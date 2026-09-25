import asyncio
import json
import threading
from http.client import HTTPConnection
import pytest
from telegram_scraper.config import Settings, ConfigError
from telegram_scraper.engine import TelegramService
from telegram_scraper.server import Runtime
from test_engine import service, Client
from test_app import application, request


def test_credential_cleanup_failure_retry_and_preference_only_save(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.settings.update({"api_id": 1, "api_hash": "a" * 32})
    class FailingClient(Client):
        calls = 0
        async def disconnect(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("session close failed")
    client = FailingClient()
    engine = runtime.get_service()
    engine._client = client
    engine._client_identity = ("1", "a" * 32)
    runtime.connection = {"authorized": True, "step": "connected"}
    try:
        runtime.call(runtime.update_settings({"download_media": False}))
        assert client.calls == 0
        with pytest.raises(OSError):
            runtime.call(runtime.update_settings({"api_id": 2}))
        assert Settings(tmp_path).values["api_id"] == 1
        assert not runtime.connection["authorized"] and engine._client_unavailable
        runtime.call(runtime.update_settings({"api_id": 2}))
        assert client.calls == 2 and engine._client is None
        assert Settings(tmp_path).values["api_id"] == 2 and runtime.service is None
    finally:
        runtime.close()


def test_settings_publication_failure_leaves_old_identity_disconnected(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.settings.update({"api_id": 1, "api_hash": "a" * 32})
    runtime.get_service()._client = Client()
    runtime.connection["authorized"] = True
    def fail(values):
        raise ConfigError("synthetic publish failure")
    monkeypatch.setattr(runtime.settings, "publish", fail)
    try:
        with pytest.raises(ConfigError):
            runtime.call(runtime.update_settings({"api_id": 2}))
        assert Settings(tmp_path).values["api_id"] == 1
        assert not runtime.connection["authorized"] and runtime.service is None
    finally:
        runtime.close()


def test_connect_cancellation_drains_repeatedly_cancelled_cleanup(tmp_path):
    async def run():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Partial(Client):
            def is_connected(self):
                return False
            async def connect(self):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                cleanup.set()
                await release.wait()
        engine, _, _ = service(tmp_path, Partial())
        task = asyncio.create_task(engine._connected_client())
        await entered.wait()
        task.cancel()
        await cleanup.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and engine._client_unavailable
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine._client is None and engine._cleanup_task.done()
    asyncio.run(run())


def test_late_http_body_cannot_admit_mutation_during_shutdown(application):
    runtime, server = application
    entered, release = threading.Event(), threading.Event()
    class HeldClient(Client):
        async def disconnect(self):
            entered.set()
            await asyncio.to_thread(release.wait)
    runtime.get_service()._client = HeldClient()
    connections = []
    for path, body in [("/api/jobs", {"mode": "verify"}), ("/api/settings", {"download_media": False}),
                       ("/api/login/code", {"phone": "+41791234567"}), ("/api/channels", {"channel": "@fixture"})]:
        c = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        data = json.dumps(body).encode()
        c.putrequest("POST", path)
        for k, v in {"Cookie": f"{server.cookie_name}={server.token}", "X-CSRF-Token": server.token,
                     "Content-Type": "application/json", "Content-Length": str(len(data))}.items():
            c.putheader(k, v)
        c.endheaders()
        connections.append((c, data))
    closed = asyncio.run_coroutine_threadsafe(runtime._close(), runtime.loop)
    assert entered.wait(5)
    try:
        for c, data in connections:
            c.send(data)
            response = c.getresponse()
            assert response.status == 400
            assert "closing" in response.read().decode()
            c.close()
        assert runtime.task is None and not runtime._mutations
    finally:
        release.set()
        closed.result(5)


def test_real_telethon_handshake_timeouts_release_loopback_transports(tmp_path, monkeypatch):
    from telethon import TelegramClient
    from telethon.network.connection.tcpfull import ConnectionTcpFull
    async def run():
        peers, ended = set(), []
        async def stall(reader, writer):
            peers.add(writer)
            try:
                await reader.read()
            finally:
                peers.discard(writer)
                writer.close()
                await writer.wait_closed()
                ended.append(True)
        server = await asyncio.start_server(stall, "127.0.0.1", 0)
        real_init = ConnectionTcpFull.__init__
        def local(self, ip, port, dc_id, **kwargs):
            real_init(self, "127.0.0.1", server.sockets[0].getsockname()[1], dc_id, **kwargs)
        monkeypatch.setattr(ConnectionTcpFull, "__init__", local)
        engine, _, _ = service(tmp_path)
        engine.settings.update(api_id=1, api_hash="a" * 32)
        engine._client = None
        try:
            for _ in range(3):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(engine._connected_client(), .1)
                assert engine._client is None
            async with asyncio.timeout(5):
                while len(ended) < 3:
                    await asyncio.sleep(.01)
            assert not peers
            assert not [task for task in asyncio.all_tasks() if "Connection._" in repr(task) and not task.done()]
        finally:
            await engine.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_combined_real_handshake_timeout_held_cleanup_and_shutdown(tmp_path, monkeypatch):
    from telethon import TelegramClient
    from telethon.network.connection.tcpfull import ConnectionTcpFull
    async def run():
        peers = set()
        async def stall(reader, writer):
            peers.add(writer)
            try: await reader.read()
            finally:
                peers.discard(writer)
                writer.close()
                await writer.wait_closed()
        listener = await asyncio.start_server(stall, "127.0.0.1", 0)
        init = ConnectionTcpFull.__init__
        monkeypatch.setattr(ConnectionTcpFull, "__init__", lambda self, ip, port, dc, **kw: init(self, "127.0.0.1", listener.sockets[0].getsockname()[1], dc, **kw))
        cleanup, release = threading.Event(), threading.Event()
        disconnect = TelegramClient._disconnect_coro
        async def held(self):
            cleanup.set()
            await asyncio.to_thread(release.wait)
            await disconnect(self)
        monkeypatch.setattr(TelegramClient, "_disconnect_coro", held)
        runtime = Runtime(tmp_path)
        runtime.settings.update({"api_id": 1, "api_hash": "a" * 32})
        try:
            with pytest.raises(ValueError, match="too long"):
                await asyncio.to_thread(runtime.call, runtime.authenticate("code", {"phone": "+41791234567"}), .15)
            assert await asyncio.to_thread(cleanup.wait, 5)
            closing = asyncio.run_coroutine_threadsafe(runtime._close(), runtime.loop)
            await asyncio.to_thread(runtime.call, asyncio.sleep(.02))
            assert runtime.auth_busy and runtime.loop.is_running() and not closing.done()
            for operation in (runtime.authenticate("connect", {}), runtime.start_job({"mode": "verify"})):
                with pytest.raises(ValueError, match="closing"):
                    await asyncio.to_thread(runtime.call, operation)
            release.set()
            await asyncio.wrap_future(closing)
            async with asyncio.timeout(5):
                while peers: await asyncio.sleep(.01)
            assert not runtime.auth_busy and not runtime._mutations and runtime.service._client is None
            assert runtime.task is None and not runtime.evidence.path.exists()
        finally:
            release.set()
            await asyncio.to_thread(runtime.close)
            listener.close()
            await listener.wait_closed()
    asyncio.run(run())
