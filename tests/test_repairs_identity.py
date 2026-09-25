import asyncio
import json
import sqlite3
import threading
from pathlib import Path
import pytest
from telethon import types, utils
from telegram_scraper.server import Runtime
from telegram_scraper.storage import Store
from telegram_scraper.config import Settings, ConfigError
from telegram_scraper.engine import UserError
from telegram_scraper.evidence import EvidenceStore, EvidenceError
from test_engine import Client, ProtocolClient, WireObject, Message, service, DAY


def test_legacy_peer_kind_inference_requires_intact_saved_source(tmp_path):
    store = Store(tmp_path)
    store.load()
    store.save()
    store.bind_channel(123, "original", "@original")
    evidence = EvidenceStore(tmp_path)
    run = evidence.begin_run({"mode": "sync", "channel_id": 123})
    evidence.append_observation(run, "channel", 123, {"_": "Channel", "id": 123})
    assert store.channel_identity() == {"kind": "channel", "id": 123}
    connection = sqlite3.connect(evidence.path)
    try:
        with connection:
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='observations_no_update'").fetchone()[0]
            connection.execute("DROP TRIGGER observations_no_update")
            connection.execute("UPDATE observations SET payload_json=?", ('{"_":"Chat","id":123}',))
            connection.execute(trigger)
    finally:
        connection.close()
    with pytest.raises(EvidenceError, match="checksum"):
        store.channel_identity()


def runtime_with_client(root, *, legacy=False, configured=True, client=None):
    store = Store(root)
    store.load()
    if legacy:
        store.upsert({"id": 1, "text": "unchanged legacy", "custom": {"preserve": True}})
        store.save()
    else:
        store.bind_channel(123, "Original", "@original", peer_kind="channel", confirmed=True)
    settings = Settings(root)
    settings.update({"api_id": 1, "api_hash": "a" * 32, "channel": "@original" if configured else "", "capture_context": False, "archive_before_sync": False})
    runtime = Runtime(root)
    runtime.get_service()._client = client or ProtocolClient([])
    runtime.connection = {"authorized": True, "step": "connected"}
    return runtime


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("manual_metadata", [False, True])
def test_explicit_legacy_binding_preserves_index_and_unknown_metadata(tmp_path, configured, manual_metadata):
    runtime = runtime_with_client(tmp_path, legacy=True, configured=configured)
    before = runtime.store.messages_file.read_bytes()
    if manual_metadata:
        (runtime.store.data_dir / "channel.json").write_text(json.dumps({"id": 123, "title": "manual", "extra": "keep"}))
    try:
        result = runtime.call(runtime.resolve_identity({"channel": "@current"}))
        assert result["peer"]["kind"] == "channel"
        assert runtime.store.messages_file.read_bytes() == before
        runtime.call(runtime.confirm_identity({"token": result["token"], "original_channel": True}))
        assert runtime.store.channel_identity() == {"kind": "channel", "id": 123}
        if manual_metadata:
            assert runtime.store.channel_info()["extra"] == "keep"
        assert runtime.store.messages_file.read_bytes() == before
        assert runtime.channel_settings()["channel"] == "@current"
    finally:
        runtime.close()


def test_stale_confirmation_and_retry_after_locator_write_failure(tmp_path, monkeypatch):
    runtime = runtime_with_client(tmp_path, legacy=True)
    try:
        old = runtime.call(runtime.resolve_identity({"channel": "@current"}))
        runtime.call(runtime.update_settings({"download_media": False}))
        with pytest.raises(ValueError, match="stale"):
            runtime.call(runtime.confirm_identity({"token": old["token"], "original_channel": True}))
        value = runtime.call(runtime.resolve_identity({"channel": "@current"}))
        publish = runtime.settings.publish
        monkeypatch.setattr(runtime.settings, "publish", lambda values: (_ for _ in ()).throw(ConfigError("disk full")))
        with pytest.raises(ConfigError):
            runtime.call(runtime.confirm_identity({"token": value["token"], "original_channel": True}))
        assert runtime.store.channel_identity() == {"kind": "channel", "id": 123}
        assert Settings(tmp_path).values["channel"] == "@original"
        monkeypatch.setattr(runtime.settings, "publish", publish)
        runtime.call(runtime.confirm_identity({"token": value["token"], "original_channel": True}))
        assert Settings(tmp_path).values["channel"] == "@current"
    finally:
        runtime.close()


@pytest.mark.parametrize("wrong", [False, True])
def test_obsolete_locator_falls_back_to_saved_typed_peer_but_rejects_wrong_peer(tmp_path, wrong):
    class Renamed(ProtocolClient):
        async def get_entity(self, target):
            if isinstance(target, str):
                if wrong:
                    return WireObject(_="Chat", id=123, title="different basic group")
                raise ValueError("username gone")
            assert isinstance(target, types.PeerChannel) and target.channel_id == 123
            return WireObject(_="Channel", id=123, title="renamed")
    runtime = runtime_with_client(tmp_path, client=Renamed([]))
    try:
        if wrong:
            with pytest.raises(UserError, match="different"):
                runtime.call(runtime.start_job({"mode": "sync"}))
        else:
            assert runtime.call(runtime.start_job({"mode": "sync"}))["capture_started"]
            runtime.call(wait_job(runtime))
            assert runtime.job["status"] == "completed"
        assert runtime.store.channel_identity() == {"kind": "channel", "id": 123}
    finally:
        runtime.close()


async def wait_job(runtime):
    await runtime.task


@pytest.mark.parametrize("multiple", [False, True])
def test_duplicate_preflight_no_capture_mutation_then_explicit_open(tmp_path, multiple):
    runtime = runtime_with_client(tmp_path)
    try:
        runtime.connection["authorized"] = False
        candidate = runtime.call(runtime.add_channel({"channel": "https://t.me/c/123/456"}))["active_channel"]
        if multiple:
            other, _ = runtime.channels.add("@another", "@original")
            Store(runtime.channels.folder(other)).bind_channel(123, "copy", "@another", peer_kind="channel", confirmed=True)
        root = runtime.store.root
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        job, revision = dict(runtime.job), runtime.context_revision
        catalogue = runtime.channels.path.read_bytes()
        runtime.connection["authorized"] = True
        outcome = runtime.call(runtime.start_job({"mode": "sync"}))
        assert outcome["capture_started"] is False
        assert len(outcome["existing_archive_ids"]) == (2 if multiple else 1)
        assert runtime.channels.active == candidate and runtime.context_revision == revision
        assert runtime.job == job and runtime.task is None
        assert runtime.channels.path.read_bytes() == catalogue
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert not runtime.evidence.path.exists() and not runtime.store.messages_file.exists()
        runtime.call(runtime.select_channel({"id": "main"}))
        assert runtime.context_revision == revision + 1 and runtime.task is None
        if not multiple:
            assert runtime.call(runtime.start_job({"mode": "sync"}))["capture_started"]
            runtime.call(wait_job(runtime))
            assert runtime.job["status"] == "completed"
    finally:
        runtime.close()


def test_connected_add_opens_existing_identity_without_new_folder(tmp_path):
    runtime = runtime_with_client(tmp_path)
    try:
        value = runtime.call(runtime.add_channel({"channel": "https://t.me/c/123/456"}))
        assert value == {"active_channel": "main", "added": False}
        assert not (tmp_path / "channels").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("ending", ["stop", "shutdown", "timeout"])
def test_exclusive_preflight_cancellation_cannot_handoff_late_run(tmp_path, ending):
    entered, released = threading.Event(), threading.Event()
    class Held(ProtocolClient):
        async def get_entity(self, target):
            entered.set()
            try:
                await asyncio.to_thread(released.wait)
            except asyncio.CancelledError:
                # A misbehaving resolution adapter may return after cancellation.
                await asyncio.to_thread(released.wait)
            return await super().get_entity(target)
    runtime = runtime_with_client(tmp_path, client=Held([]))
    try:
        operation = asyncio.run_coroutine_threadsafe(runtime.start_job({"mode": "sync"}), runtime.loop)
        assert entered.wait(5)
        for coro in (runtime.update_settings({"download_media": False}), runtime.select_channel({"id": "main"}), runtime.authenticate("connect", {})):
            with pytest.raises(ValueError):
                runtime.call(coro)
        if ending == "shutdown":
            closing = asyncio.run_coroutine_threadsafe(runtime._close(), runtime.loop)
        elif ending == "stop":
            runtime.call(runtime.stop_job())
        else:
            operation.cancel()
        released.set()
        try:
            operation.result(5)
        except Exception:
            pass
        if ending == "shutdown":
            closing.result(5)
        runtime.call(asyncio.sleep(.02))
        assert runtime.task is None and not runtime.evidence.path.exists()
        assert runtime.channels.active == "main" and not runtime.library_busy
    finally:
        released.set()
        runtime.close()


@pytest.mark.parametrize("kind", ["chat", "channel"])
def test_actual_telethon_full_request_resolution_for_peer_kind(tmp_path, kind):
    async def run():
        entity = (types.Chat(id=123, title="group", photo=types.ChatPhotoEmpty(), participants_count=1, date=DAY, version=1)
                  if kind == "chat" else types.Channel(id=123, title="channel", photo=types.ChatPhotoEmpty(), date=DAY, access_hash=456))
        class PeerClient(ProtocolClient):
            async def get_entity(self, target):
                return entity
            async def get_input_entity(self, target):
                return utils.get_input_peer(target)
            async def __call__(self, request):
                if type(request).__name__.startswith("GetFull"):
                    await request.resolve(self, utils)
                    assert type(request).__name__ == ("GetFullChatRequest" if kind == "chat" else "GetFullChannelRequest")
                    return WireObject(_="messages.ChatFull", full_chat=WireObject(id=123, chat_photo=types.ChatPhotoEmpty()), chats=[], users=[])
                return await super().__call__(request)
        engine, _, _ = service(tmp_path, PeerClient([]), capture_context=True)
        result = await engine.run("sync")
        assert result["status"] == "completed"
        assert engine._full_channel is not None
        assert not any(i["stage"] == "full_channel" for i in engine._issues)
    asyncio.run(run())


def test_cli_duplicate_result_does_not_wait_on_an_old_job(tmp_path, monkeypatch, capsys):
    from telegram_scraper import cli, server
    from contextlib import nullcontext
    class FakeRuntime:
        def __init__(self, root): pass
        def lock(self): return nullcontext()
        def recover_evidence(self): pass
        def call(self, coroutine): return asyncio.run(coroutine)
        async def authenticate(self, *args): return {"authorized": True}
        async def start_job(self, payload): return {"capture_started": False, "existing_archive_ids": ["main"]}
        async def state(self): pytest.fail("No polling after a no-capture result")
        def close(self): pass
    monkeypatch.setattr(server, "Runtime", FakeRuntime)
    assert cli.main(["--data-dir", str(tmp_path), "sync"]) == 1
    assert "Capture did not start" in capsys.readouterr().out
