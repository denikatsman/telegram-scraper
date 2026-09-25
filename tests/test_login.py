"""Exercise real Telethon login and update handling over an offline transport."""
import asyncio
from datetime import datetime, timezone
import json

import pytest
from telethon import errors, functions, types
from telethon.client import telegrambaseclient
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from test_app import application, payload, request


class LoginTransport:
    """Only the network is simulated; Telethon owns auth, updates and session IO."""

    def __init__(self, auth_key, **kwargs):
        self.auth_key = auth_key or AuthKey(b"a" * 256)
        self.connected = False
        self.authorized = False
        self.pending = set()
        self.sent_codes = 0
        self.unauthorized_differences = 0
        self._disconnected = asyncio.get_running_loop().create_future()
        self.user = types.User(id=42, access_hash=123, first_name="Fixture", is_self=True)
        self.state = types.updates.State(pts=10, qts=0, date=datetime.now(timezone.utc), seq=1, unread_count=0)

    @property
    def disconnected(self):
        return asyncio.shield(self._disconnected)

    def is_connected(self):
        return self.connected

    async def connect(self, connection):
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False
        for future in list(self.pending):
            future.cancel()
        if not self._disconnected.done():
            self._disconnected.set_result(None)

    def send(self, operation, ordered=False):
        while isinstance(operation, (functions.InvokeWithLayerRequest,
                                     functions.InitConnectionRequest,
                                     functions.InvokeWithoutUpdatesRequest)):
            operation = operation.query
        future = asyncio.get_running_loop().create_future()
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        if isinstance(operation, functions.help.GetConfigRequest):
            future.set_result(True)
        elif isinstance(operation, functions.auth.SendCodeRequest):
            self.sent_codes += 1
            # Telegram accepts the request before the client receives its reply.
            # Old background catch-up can disconnect in this exact interval.
            def deliver():
                if not future.done():
                    future.set_result(types.auth.SentCode(
                        type=types.auth.SentCodeTypeApp(length=5), phone_code_hash="fixture-hash"))
            asyncio.get_running_loop().call_later(0.02, deliver)
        elif isinstance(operation, functions.auth.SignInRequest):
            assert operation.phone_code_hash == "fixture-hash"
            if operation.phone_code != "12345":
                future.set_exception(errors.PhoneCodeInvalidError(operation))
            else:
                self.authorized = True
                future.set_result(types.auth.Authorization(user=self.user))
        elif isinstance(operation, (functions.users.GetUsersRequest,
                                    functions.updates.GetStateRequest,
                                    functions.updates.GetDifferenceRequest)):
            if not self.authorized:
                if isinstance(operation, functions.updates.GetDifferenceRequest):
                    self.unauthorized_differences += 1
                future.set_exception(errors.AuthKeyUnregisteredError(operation))
            elif isinstance(operation, functions.users.GetUsersRequest):
                future.set_result([self.user])
            elif isinstance(operation, functions.updates.GetStateRequest):
                future.set_result(self.state)
            else:
                future.set_result(types.updates.DifferenceEmpty(date=self.state.date, seq=1))
        else:
            raise AssertionError(f"Unexpected offline request: {type(operation).__name__}")
        return future


@pytest.fixture
def login_transport(application, monkeypatch):
    runtime, _ = application
    runtime.settings.update({"api_id": 12345, "api_hash": "a" * 32})
    transports = []

    def create(*args, **kwargs):
        transport = LoginTransport(*args, **kwargs)
        transports.append(transport)
        return transport

    monkeypatch.setattr(telegrambaseclient, "MTProtoSender", create)
    return transports


@pytest.mark.parametrize("old_update_state", [False, True])
def test_http_login_with_real_telethon_and_expired_session(application, login_transport, old_update_state):
    runtime, _ = application
    if old_update_state:
        session = SQLiteSession(str(runtime.root / "telegram-scraper"))
        session.set_update_state(0, types.updates.State(
            pts=5, qts=0, date=datetime(2025, 1, 1, tzinfo=timezone.utc), seq=1, unread_count=0))
        session.save()
        session.close()
    archive_before = runtime.store.messages_file.read_bytes()

    status, _, body = request(application, "/api/login/code", "POST", {"phone": "+41 79 123 45 67"})
    assert status == 200, body.decode()
    assert json.loads(body) == {"ok": True, "step": "code"}
    assert payload(application, "/api/state")["connection"] == {"authorized": False, "step": "code"}
    assert login_transport[0].sent_codes == 1
    assert login_transport[0].unauthorized_differences == 0

    status, _, body = request(application, "/api/login/verify", "POST", {"code": "wrong"})
    assert status == 400
    assert "incorrect" in json.loads(body)["error"]
    assert payload(application, "/api/state")["connection"]["step"] == "code"

    status, _, body = request(application, "/api/login/verify", "POST", {"code": "12345"})
    assert status == 200, body.decode()
    assert json.loads(body)["step"] == "connected"
    assert payload(application, "/api/state")["connection"]["authorized"]
    assert not runtime.service._phone and not runtime.service._phone_code_hash
    assert runtime.store.messages_file.read_bytes() == archive_before


def test_interrupted_login_gives_a_retryable_error_and_releases_auth(application):
    runtime, _ = application

    class InterruptedService:
        async def send_code(self, phone):
            raise asyncio.CancelledError()

    runtime.service = InterruptedService()
    try:
        status, _, body = request(application, "/api/login/code", "POST", {"phone": "+41791234567"})
        assert status == 400
        assert "interrupted" in json.loads(body)["error"]
        assert not runtime.auth_busy
        assert not runtime.connection["authorized"]
    finally:
        runtime.service = None
