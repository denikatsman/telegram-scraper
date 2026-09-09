"""Offline protocol fixtures: no account, credentials, or live Telegram needed."""
import asyncio
import base64
import copy
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_scraper.engine import AuthRequired, TelegramService, UserError, _json_value, date_bounds, friendly_error
from telegram_scraper.storage import Store


UTC = timezone.utc
DAY = datetime(2025, 3, 11, tzinfo=UTC)


class Message:
    def __init__(self, id, text="A trading lesson", date=DAY, media=False, media_id=100, payload=b"video", edit_date=None):
        self.id = id
        self.message = self.text = text
        self.date = date
        self.edit_date = edit_date
        self.payload = payload
        self.document = SimpleNamespace(id=media_id, access_hash=987, mime_type="video/mp4", size=len(payload)) if media else None
        self.photo = None
        self.media = self.document
        self.file = SimpleNamespace(ext=".mp4", size=len(payload), name="lesson.mp4", mime_type="video/mp4") if media else None

    def to_dict(self):
        return {"_": "Message", "id": self.id, "date": self.date, "message": self.message,
                "edit_date": self.edit_date,
                "media": {"_": "MessageMediaDocument", "document": {"id": self.document.id, "file_reference": b"\x00\xff"}} if self.media else None}

    def __bytes__(self):
        return f"wire-message:{self.id}:{self.message}".encode()


class WireObject(SimpleNamespace):
    def to_dict(self):
        return dict(vars(self))

    def __bytes__(self):
        return ("wire:" + str(getattr(self, "_", "entity"))).encode()


class Client:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.iter_kwargs = []
        self.handlers = []
        self.downloads = []
        self.download_error = None
        self.during_history = None
        self.block_download = None
        self.authorized = True
        self.entity_targets = []

    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, target):
        self.entity_targets.append(target)
        return WireObject(id=123, title="Fixture channel", _="Channel")

    def iter_messages(self, entity, **kwargs):
        self.iter_kwargs.append(kwargs)

        async def iterator():
            for message in sorted(self.messages, key=lambda item: item.id, reverse=True):
                if self.during_history:
                    hook, self.during_history = self.during_history, None
                    await hook()
                yield message

        return iterator()

    async def download_media(self, message, file, progress_callback):
        self.downloads.append(message.id)
        if self.download_error:
            raise self.download_error
        file.write(message.payload)
        await progress_callback(len(message.payload), len(message.payload))
        if self.block_download:
            self.block_download.set()
            await asyncio.Event().wait()
        return file

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback, event):
        self.handlers.remove((callback, event))


def service(tmp_path, client=None, records=(), report=None, **settings):
    store = Store(tmp_path)
    store.load()
    for record in records:
        store.upsert(record)
    if records:
        store.save()
    updates = []
    config = {"api_id": 1, "api_hash": "fixture", "channel": "@fixture", "legacy_channel": "@fixture",
              "download_media": True, "archive_before_sync": False, "capture_context": False, **settings}
    engine = TelegramService(tmp_path, config, store, report or updates.append)
    engine._client = client or Client()
    return engine, store, updates


def test_date_bounds_include_fractional_final_second_and_reject_reverse():
    first, upper = date_bounds("2025-03-11", "2025-03-11")
    assert first == DAY
    assert upper == DAY + timedelta(days=1)
    assert first <= DAY.replace(hour=23, minute=59, second=59, microsecond=999999) < upper
    for start, end in [("2025-03-12", "2025-03-11"), ("2025-02-30", ""), ("2025-3-1", ""), ("9999-12-31", "")]:
        with pytest.raises(UserError):
            date_bounds(start, end)


def test_range_reads_beyond_old_5000_limit_and_keeps_exact_day(tmp_path):
    async def run():
        # Most messages are older than the range. The fake stream exposes all
        # records to verify both the filter and the unbounded Telegram request.
        messages = [Message(id, date=DAY) for id in range(1, 5003)]
        messages.extend([Message(5003, date=DAY + timedelta(days=1)), Message(5004, date=DAY.replace(hour=23, minute=59, second=59, microsecond=999999))])
        client = Client(messages)
        engine, store, _ = service(tmp_path, client)
        engine.evidence = MemoryEvidence()
        # Avoid making this coverage depend on thousands of disk writes; real
        # Store writes are exercised by the focused integrity cases below.
        checkpoints = []
        async def checkpoint():
            checkpoints.append(len(store.records))
            engine._dirty = False
            engine._since_save = 0
        engine._save = checkpoint
        result = await engine.run("range", "2025-03-11", "2025-03-11")
        assert result["added"] == 5003
        assert 5003 not in store.records and 5004 in store.records
        assert client.iter_kwargs == [{"limit": None, "offset_date": DAY + timedelta(days=1)}]
        assert len(checkpoints) >= 200
    asyncio.run(run())


class MemoryEvidence:
    """Fast receipt fixture for the >5000 pagination boundary test only."""
    def prepare(self):
        pass
    def begin_run(self, metadata):
        return "fixture-run"
    def append_observation(self, *args, **kwargs):
        return 1
    def append_event(self, *args, **kwargs):
        return 1
    def update_run(self, *args, **kwargs):
        pass
    def finish_run(self, *args, **kwargs):
        pass


def test_edits_preserve_original_text_unknown_fields_and_deleted_posts(tmp_path):
    async def run():
        original = {"id": 1, "text": "original", "date": DAY.isoformat(), "type": "Message", "custom_note": {"keep": True}}
        deleted = {"id": 2, "text": "no longer on Telegram"}
        engine, store, _ = service(tmp_path, Client([Message(1, text="edited", edit_date=DAY + timedelta(hours=1))]), [original, deleted])
        result = await engine.run("sync")
        assert result["updated"] == 1
        store.load()
        assert store.records[1]["text"] == "edited"
        assert store.records[1]["custom_note"] == {"keep": True}
        assert store.records[1]["revisions"][0]["text"] == "original"
        assert store.records[2]["text"] == "no longer on Telegram"
    asyncio.run(run())


@pytest.mark.parametrize("legacy_path", [None, "telegram_data/media/missing.mp4", "telegram_data/media/empty.mp4"])
def test_missing_and_zero_byte_attachments_are_retried(tmp_path, legacy_path):
    async def run():
        original = {"id": 1, "text": "saved already", "media_id": 100}
        if legacy_path:
            original["media_file"] = legacy_path
        if legacy_path and "empty" in legacy_path:
            (tmp_path / legacy_path).parent.mkdir(parents=True)
            (tmp_path / legacy_path).touch()
        # The existing JSON must be written before orphan-media protections run.
        store = Store(tmp_path)
        store.data_dir.mkdir(exist_ok=True)
        store.messages_file.write_text(json.dumps([original]))
        client = Client([Message(1, media=True)])
        engine, store, _ = service(tmp_path, client)
        result = await engine.run("sync")
        assert result["media_downloaded"] == 1
        record = store.records[1]
        assert store.media_path(record).read_bytes() == b"video"
        assert record["raw"]["media"]["document"]["file_reference"] == {"encoding": "base64", "data": base64.b64encode(b"\x00\xff").decode()}
        assert not list(store.media_dir.glob("*.part"))
        await engine.run("sync")
        assert client.downloads == [1]
    asyncio.run(run())


def test_replaced_media_keeps_old_file_and_downloads_new_attachment(tmp_path):
    async def run():
        client = Client([Message(1, media=True, media_id=100, payload=b"old")])
        engine, store, _ = service(tmp_path, client)
        await engine.run("sync")
        old_file = store.media_path(store.records[1])
        client.messages = [Message(1, media=True, media_id=200, payload=b"new")]
        await engine.run("sync")
        assert old_file.read_bytes() == b"old"
        current = store.media_path(store.records[1])
        assert current != old_file and current.read_bytes() == b"new"
        assert store.records[1]["previous_media"][0]["media_id"] == 100
        assert client.downloads == [1, 1]
    asyncio.run(run())


def test_preexisting_filename_is_never_overwritten(tmp_path):
    async def run():
        engine, store, _ = service(tmp_path, Client([Message(1, media=True)]), [{"id": 99, "text": "previous post"}])
        store.media_dir.mkdir(parents=True)
        collision = store.media_dir / "Message-1 (2025-03-11T00-00-00).mp4"
        collision.write_bytes(b"must survive")
        await engine.run("sync")
        assert collision.read_bytes() == b"must survive"
        assert store.media_path(store.records[1]) != collision
    asyncio.run(run())


def test_attachment_failure_keeps_post_and_retry_clears_error(tmp_path):
    async def run():
        client = Client([Message(1, media=True)])
        client.download_error = RuntimeError("internal diagnostic should never appear")
        engine, store, updates = service(tmp_path, client)
        result = await engine.run("sync")
        assert result["media_failed"] == 1
        assert result["status"] == "warning"
        store.load()
        assert store.records[1]["text"] == "A trading lesson"
        assert store.records[1]["media_status"] == "failed"
        assert "internal diagnostic" not in json.dumps(updates)
        assert not list(store.media_dir.glob("*.part"))
        client.download_error = None
        await engine.run("sync")
        assert store.records[1]["media_status"] == "downloaded"
        assert not store.records[1].get("media_error")
    asyncio.run(run())


def test_cancel_during_download_cleans_partial_and_keeps_post(tmp_path):
    async def run():
        client = Client([Message(1, media=True)])
        client.block_download = asyncio.Event()
        engine, store, updates = service(tmp_path, client)
        task = asyncio.create_task(engine.run("sync"))
        await asyncio.wait_for(client.block_download.wait(), 2)
        engine.cancel()
        result = await asyncio.wait_for(task, 2)
        assert result["status"] == "cancelled"
        store.load()
        assert store.records[1]["text"] == "A trading lesson"
        assert store.records[1]["media_status"] == "pending"
        assert not list(store.media_dir.iterdir())
        assert updates[-1]["status"] == "cancelled"
    asyncio.run(run())


def test_short_download_is_not_published(tmp_path):
    async def run():
        message = Message(1, media=True)
        message.file.size = 1000
        engine, store, _ = service(tmp_path, Client([message]))
        result = await engine.run("sync")
        assert result["media_failed"] == 1
        assert not list(store.media_dir.iterdir())
        assert not store.records[1].get("media_file")
    asyncio.run(run())


def test_flood_wait_is_visible_and_cancellable(tmp_path):
    async def run():
        FloodWaitError = type("FloodWaitError", (Exception,), {"seconds": 120})
        client = Client([Message(1, media=True)])
        client.download_error = FloodWaitError()
        waiting = asyncio.Event()
        updates = []
        def report(value):
            updates.append(value)
            if value.get("phase") == "waiting":
                waiting.set()
        engine, store, _ = service(tmp_path, client, report=report)
        task = asyncio.create_task(engine.run("sync"))
        await asyncio.wait_for(waiting.wait(), 2)
        engine.cancel()
        assert (await asyncio.wait_for(task, 2))["status"] == "cancelled"
        assert any("120 seconds" in item.get("message", "") for item in updates)
        store.load()
        assert 1 in store.records
    asyncio.run(run())


def test_watch_captures_startup_new_posts_and_edits_then_removes_handlers(tmp_path):
    async def run():
        client = Client([Message(1, text="old")])
        watching = asyncio.Event()
        engine, store, _ = service(tmp_path, client, report=lambda value: watching.set() if value.get("phase") == "watching" else None)
        async def during_history():
            assert len(client.handlers) == 2
            await client.handlers[0][0](SimpleNamespace(message=Message(2, text="arrived during sync")))
            await client.handlers[1][0](SimpleNamespace(message=Message(1, text="edited during sync", edit_date=DAY)))
        client.during_history = during_history
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 2)
        assert store.records[1]["text"] == "edited during sync"
        assert store.records[2]["text"] == "arrived during sync"
        engine.cancel()
        assert (await asyncio.wait_for(task, 2))["status"] == "cancelled"
        assert not client.handlers
    asyncio.run(run())


def test_watch_overflow_rescans_history_without_losing_posts(tmp_path):
    async def run():
        client = Client([Message(1)])
        watching = asyncio.Event()
        engine, store, _ = service(tmp_path, client, report=lambda value: watching.set() if value.get("phase") == "watching" else None)
        async def during_history():
            for id in range(2, 602):
                message = Message(id)
                client.messages.append(message)
                await client.handlers[0][0](SimpleNamespace(message=message))
        client.during_history = during_history
        engine.evidence = MemoryEvidence()
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 5)
        engine.cancel()
        await asyncio.wait_for(task, 2)
        assert len(store.records) == 601
        assert len(client.iter_kwargs) == 2
    asyncio.run(run())


def test_private_invite_checks_existing_access_and_never_joins(tmp_path):
    async def run():
        class InviteClient(Client):
            async def __call__(self, request):
                assert type(request).__name__ == "CheckChatInviteRequest"
                return type("ChatInvite", (), {})()
        engine, store, _ = service(tmp_path, InviteClient(), channel="https://t.me/+fixture")
        with pytest.raises(UserError, match="Join it in Telegram first"):
            await engine.run("sync")
        assert not store.records
        assert not store.messages_file.exists()
    asyncio.run(run())


def test_numeric_channel_ids_pass_to_telethon_as_integers(tmp_path):
    async def run():
        client = Client()
        engine, _, _ = service(tmp_path, client, channel="-1001234567890")
        await engine.run("sync")
        assert client.entity_targets == [-1001234567890]
    asyncio.run(run())


def test_login_has_two_factor_flow_and_does_not_leak_error_details(tmp_path):
    async def run():
        class LoginClient(Client):
            async def send_code_request(self, phone):
                assert phone == "+41791234567"
                return SimpleNamespace(phone_code_hash="fixture-hash")
            async def sign_in(self, **kwargs):
                if "password" not in kwargs:
                    assert kwargs["phone_code_hash"] == "fixture-hash"
                    raise type("SessionPasswordNeededError", (Exception,), {})("secret diagnostic")
                assert kwargs["password"] == "fixture-password"
        engine, _, _ = service(tmp_path, LoginClient())
        assert await engine.send_code("+41 79 123 45 67") == {"step": "code"}
        assert await engine.sign_in(code="12345") == {"step": "password"}
        assert await engine.sign_in(password="fixture-password") == {"step": "connected"}
        assert not engine._phone and not engine._phone_code_hash
        assert "secret" not in friendly_error(RuntimeError("secret"))
    asyncio.run(run())


def test_first_status_does_not_create_session_or_archive(tmp_path):
    async def run():
        engine = TelegramService(tmp_path, {"api_id": 1, "api_hash": "fixture"}, Store(tmp_path), lambda _: None)
        assert await engine.status() == {"authorized": False, "configured": True}
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


def test_history_error_checkpoints_posts_before_reporting_failure(tmp_path):
    async def run():
        class BrokenHistoryClient(Client):
            def iter_messages(self, entity, **kwargs):
                async def iterator():
                    yield Message(1)
                    raise ConnectionError("private diagnostics")
                return iterator()
        engine, store, updates = service(tmp_path, BrokenHistoryClient())
        with pytest.raises(UserError, match="Check your internet"):
            await engine.run("sync")
        store.load()
        assert 1 in store.records
        assert updates[-1]["status"] == "error"
    asyncio.run(run())


def test_same_size_corruption_is_detected_and_repaired_without_overwrite(tmp_path):
    async def run():
        client = Client([Message(1, media=True, payload=b"valid")])
        engine, store, _ = service(tmp_path, client)
        await engine.run("sync")
        old = store.media_path(store.records[1])
        assert store.records[1]["media_sha256"] == hashlib.sha256(b"valid").hexdigest()
        old.write_bytes(b"wrong")
        await engine.run("sync")
        new = store.media_path(store.records[1])
        assert old.read_bytes() == b"wrong"
        assert new != old and new.read_bytes() == b"valid"
        assert client.downloads == [1, 1]
    asyncio.run(run())


def test_removed_attachment_updates_current_post_but_preserves_download(tmp_path):
    async def run():
        client = Client([Message(1, media=True)])
        engine, store, _ = service(tmp_path, client)
        await engine.run("sync")
        original = store.media_path(store.records[1])
        client.messages = [Message(1, text="attachment removed", edit_date=DAY + timedelta(hours=1))]
        await engine.run("sync")
        record = store.records[1]
        assert record["has_media"] is False
        assert record["media_file"] is None
        assert record["media_kind"] is None
        assert original.read_bytes() == b"video"
        assert record["previous_media"][0]["media_file"]
        assert record["revisions"][-1]["media_file"]
    asyncio.run(run())


def test_watch_requests_missed_updates_periodically(tmp_path):
    async def run():
        recovered = asyncio.Event()
        class CatchUpClient(Client):
            calls = 0
            async def catch_up(self):
                self.calls += 1
                if self.calls == 2:
                    await self.handlers[0][0](SimpleNamespace(message=Message(2, text="during reconnect")))
        client = CatchUpClient([Message(1)])
        engine, store, _ = service(tmp_path, client, report=lambda value: recovered.set() if value.get("phase") == "watching" and 2 in store.records else None)
        engine.CATCH_UP_INTERVAL = 0.01
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(recovered.wait(), 2)
        engine.cancel()
        await asyncio.wait_for(task, 2)
        assert client.calls >= 2
        assert store.records[2]["text"] == "during reconnect"
    asyncio.run(run())


def test_force_cancel_waits_for_disk_worker_before_job_finishes(tmp_path):
    async def run():
        engine, store, _ = service(tmp_path, Client([Message(1)]), [{"id": 2, "text": "existing"}], archive_before_sync=True)
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        def snapshot(**kwargs):
            worker_started.set()
            release_worker.wait(timeout=2)
            worker_finished.set()
        store.snapshot = snapshot
        task = asyncio.create_task(engine.run("sync"))
        await asyncio.to_thread(worker_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release_worker.set()
        result = await asyncio.wait_for(task, 2)
        assert result["status"] == "cancelled"
        assert worker_finished.is_set()
    asyncio.run(run())


def test_expired_session_is_typed_so_ui_can_clear_connected_state(tmp_path):
    async def run():
        client = Client()
        client.authorized = False
        engine, _, _ = service(tmp_path, client)
        with pytest.raises(AuthRequired):
            await engine.run("sync")
        class RevokedClient(Client):
            async def is_user_authorized(self):
                raise type("SessionRevokedError", (Exception,), {})("do not expose details")
        engine._client = RevokedClient()
        with pytest.raises(AuthRequired, match="revoked"):
            await engine.status()
        with pytest.raises(AuthRequired, match="revoked"):
            await engine.run("sync")
    asyncio.run(run())


class ProtocolClient(Client):
    """A raw Telegram pagination fixture with concrete response receipts."""
    def __init__(self, messages=(), page_size=2):
        super().__init__(messages)
        from telethon._updates import EntityCache
        self._self_id = 999
        self._mb_entity_cache = EntityCache()
        self.page_size = page_size
        self.requests = []
        self.page_failures = {}
        self.total_override = None
        self.after_anchor = None
        self.full_error = None
        self.bad_history = False

    async def __call__(self, request):
        kind = type(request).__name__
        if kind == "GetFullChannelRequest":
            if self.full_error:
                raise self.full_error
            return WireObject(_="messages.ChatFull", full_chat={"_": "ChannelFull", "id": 123, "about": "The channel description", "linked_chat_id": 456},
                              users=[WireObject(_="User", id=77, username="fixture_author")], chats=[])
        assert kind == "GetHistoryRequest", f"Unexpected network operation: {kind}"
        self.requests.append({key: getattr(request, key) for key in ("offset_id", "limit", "max_id", "offset_date")})
        if self.bad_history:
            return WireObject(_="messages.MessagesNotModified", count=5)
        failures = self.page_failures.get(request.offset_id, [])
        if request.limit != 1 and failures:
            raise failures.pop(0)
        available = sorted(self.messages, key=lambda message: message.id, reverse=True)
        if request.max_id:
            available = [message for message in available if message.id < request.max_id]
        if request.offset_id:
            available = [message for message in available if message.id < request.offset_id]
        if request.offset_date:
            available = [message for message in available if message.date is None or message.date < request.offset_date]
        result = WireObject(_="messages.ChannelMessages", count=self.total_override if self.total_override is not None else len(self.messages),
                            messages=available[:min(request.limit, self.page_size)], users=[], chats=[])
        if request.limit == 1 and self.after_anchor:
            hook, self.after_anchor = self.after_anchor, None
            await hook()
        return result


def test_raw_pages_anchor_and_capture_all_messages_without_id_gap_assumptions(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        client = ProtocolClient([Message(1), Message(4), Message(9)])
        async def later_post():
            client.messages.append(Message(10, text="arrived after anchor"))
        client.after_anchor = later_post
        engine, store, updates = service(tmp_path, client)
        result = await engine.run("sync")
        assert result["status"] == "completed"
        scan = result["coverage"]["scans"][0]
        assert scan["anchor_top_id"] == 9 and scan["telegram_total_before"] == 3
        assert scan["accessible_message_count"] == 3 and scan["count_reconciled"]
        assert result["coverage"]["history_scan_complete"]
        assert set(store.records) == {1, 4, 9}
        assert all(request["max_id"] == 10 for request in client.requests if request["limit"] != 1)
        evidence = EvidenceStore(tmp_path)
        observation = evidence.message_observations(123, 9)[0]
        assert base64.b64decode(observation["tl_bytes"]["data"]) == bytes(client.messages[2])
        assert store.records[9]["source_observation_id"] == observation["observation_id"]
        assert store.records[9]["first_observed_at"] and store.records[9]["last_observed_at"]
        report = evidence.run(result["run_id"])
        assert report["metadata"]["telegram_api_layer"] > 0
        assert report["status"] == "completed"
        assert any(update.get("run_id") == result["run_id"] and update.get("status") != "completed" for update in updates)
    asyncio.run(run())


def test_raw_history_retry_reuses_last_page_cursor_without_skipping(tmp_path):
    async def run():
        client = ProtocolClient([Message(id) for id in range(1, 7)])
        client.page_failures[5] = [ConnectionResetError("temporary")]
        engine, store, updates = service(tmp_path, client)
        engine.RETRY_DELAYS = (0, 0, 0)
        result = await engine.run("sync")
        assert result["added"] == 6
        assert set(store.records) == set(range(1, 7))
        assert [request["offset_id"] for request in client.requests].count(5) == 2
        assert any(update.get("phase") == "retrying" for update in updates)
        assert result["coverage"]["history_scan_complete"]
    asyncio.run(run())


def test_raw_history_exhausted_retries_leave_incomplete_durable_receipt(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        client = ProtocolClient([Message(id) for id in range(1, 7)])
        client.page_failures[5] = [ConnectionResetError("temporary") for _ in range(4)]
        engine, store, _ = service(tmp_path, client)
        engine.RETRY_DELAYS = (0, 0, 0)
        with pytest.raises(UserError):
            await engine.run("sync")
        store.load()
        assert set(store.records) == {5, 6}
        receipt = EvidenceStore(tmp_path).runs()[0]
        assert receipt["status"] == "error"
        assert not receipt["coverage"]["history_scan_complete"]
        assert receipt["coverage"]["scans"][0]["last_durable_id"] == 5
        assert [request["offset_id"] for request in client.requests].count(5) == 4
    asyncio.run(run())


def test_missing_history_vector_cannot_be_misreported_as_empty_complete(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        client = ProtocolClient()
        client.bad_history = True
        engine, _, _ = service(tmp_path, client)
        with pytest.raises(UserError, match="without a message list"):
            await engine.run("sync")
        receipt = EvidenceStore(tmp_path).runs()[0]
        assert receipt["status"] == "error"
        assert not receipt["coverage"]["history_scan_complete"]
        assert any(observation["kind"] == "history_anchor" for observation in EvidenceStore(tmp_path).export()["observations"])
    asyncio.run(run())


def test_server_count_difference_is_visible_and_never_complete(tmp_path):
    async def run():
        client = ProtocolClient([Message(1), Message(9)])
        client.total_override = 7
        engine, _, _ = service(tmp_path, client)
        result = await engine.run("sync")
        assert result["status"] == "warning"
        scan = result["coverage"]["scans"][0]
        assert scan["history_traversal_complete"] and not scan["coverage_complete"]
        assert scan["accessible_message_count"] == 2 and scan["telegram_total_before"] == 7
    asyncio.run(run())


def test_raw_counter_changes_are_preserved_even_without_text_edits(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        class CounterMessage(Message):
            views = 1
            def to_dict(self):
                return {**super().to_dict(), "views": self.views}
        message = CounterMessage(1)
        engine, store, _ = service(tmp_path, ProtocolClient([message]))
        await engine.run("sync")
        first_observed = store.records[1]["first_observed_at"]
        message.views = 9
        await engine.run("sync")
        observations = EvidenceStore(tmp_path).message_observations(123, 1)
        assert {observation["payload"]["views"] for observation in observations} == {1, 9}
        assert not store.records[1].get("revisions")
        assert store.records[1]["first_observed_at"] == first_observed
    asyncio.run(run())


def test_unknown_raw_types_fail_explicitly_and_preserve_available_tl(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        class Unsupported(Message):
            def to_dict(self):
                return {**super().to_dict(), "future_field": object()}
        engine, store, _ = service(tmp_path, Client([Unsupported(1)]))
        with pytest.raises(UserError, match="fully represented"):
            await engine.run("sync")
        assert not store.records
        raw = EvidenceStore(tmp_path).message_observations(123, 1)[0]
        assert raw["payload"]["capture_error"]["error_type"] == "TypeError"
        assert raw["tl_bytes"] is not None
        assert EvidenceStore(tmp_path).runs()[0]["status"] == "error"
        with pytest.raises(TypeError):
            _json_value({"nested": object()})
        with pytest.raises(TypeError):
            _json_value({1: "not silently changed into a string"})
    asyncio.run(run())


def test_raw_observation_is_durable_before_derived_processing_failure(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        engine, store, _ = service(tmp_path, ProtocolClient([Message(1)]))
        def broken_record(*args, **kwargs):
            raise RuntimeError("fixture derived failure")
        engine._record = broken_record
        with pytest.raises(UserError):
            await engine.run("sync")
        assert not store.records
        evidence = EvidenceStore(tmp_path)
        assert evidence.message_observations(123, 1)[0]["payload"]["message"] == "A trading lesson"
        assert any(event["event"].get("type") == "message_processing_failed" for event in evidence.export()["events"])
    asyncio.run(run())


def test_raw_range_count_is_selected_scope_not_channel_total(tmp_path):
    async def run():
        client = ProtocolClient([Message(1, date=DAY-timedelta(days=1)), Message(2, date=DAY), Message(3, date=DAY+timedelta(days=1))])
        engine, store, _ = service(tmp_path, client)
        result = await engine.run("range", "2025-03-11")
        scan = result["coverage"]["scans"][0]
        assert scan["accessible_message_count"] == 1 and scan["telegram_total_before"] == 3
        assert scan["count_reconciled"] is None
        assert scan["scope"] == "utc_date_range" and scan["history_traversal_complete"]
        assert set(store.records) == {2}
    asyncio.run(run())


def test_raw_persistence_failure_stops_before_derivation(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceError, EvidenceStore
        engine, store, _ = service(tmp_path, ProtocolClient([Message(1)]))
        class FailedEvidence(EvidenceStore):
            def append_observation(self, run_id, kind, *args, **kwargs):
                if kind == "message":
                    raise EvidenceError("Fixture evidence persistence failed")
                return super().append_observation(run_id, kind, *args, **kwargs)
        engine.evidence = FailedEvidence(tmp_path)
        with pytest.raises(UserError, match="evidence persistence"):
            await engine.run("sync")
        assert not store.records
        assert engine.evidence.runs()[0]["status"] == "error"
    asyncio.run(run())


def test_actual_telethon_objects_keep_exact_tl_and_initialize_download_chat(tmp_path):
    async def run():
        from telethon.tl import types
        from telethon._updates import EntityCache
        from telegram_scraper.evidence import EvidenceStore
        channel = types.Channel(id=123, title="Fixture channel", photo=types.ChatPhotoEmpty(), date=DAY, access_hash=456, broadcast=True)
        author = types.User(id=77, first_name="Fixture", access_hash=88)
        message = types.Message(id=1, peer_id=types.PeerChannel(123), from_id=types.PeerUser(77),
                                date=DAY, message="Exact source", post=True, views=10, forwards=0,
                                entities=[types.MessageEntityBold(offset=0, length=5)])
        original_bytes = bytes(message)
        class TelethonProtocolClient(ProtocolClient):
            _self_id = 999
            _mb_entity_cache = EntityCache()
            async def get_entity(self, target):
                return channel
            async def __call__(self, request):
                response = await super().__call__(request)
                if type(request).__name__ == "GetHistoryRequest":
                    response.users = [author]
                    response.chats = [channel]
                return response
        client = TelethonProtocolClient([message])
        engine, store, _ = service(tmp_path, client)
        result = await engine.run("sync")
        assert result["status"] == "completed"
        assert isinstance(message.input_chat, types.InputPeerChannel)
        assert message.sender.id == 77 and message.chat.id == 123
        evidence = EvidenceStore(tmp_path)
        observed = evidence.message_observations(123, 1)[0]
        assert base64.b64decode(observed["tl_bytes"]["data"]) == original_bytes
        assert observed["payload"]["entities"][0] == {"_": "MessageEntityBold", "offset": 0, "length": 5}
        associated = evidence.related_observations(123, 1)
        assert any(item["kind"] == "entity" and item["payload"].get("id") == 77 for item in associated)
    asyncio.run(run())


def test_empty_telegram_object_is_preserved_and_explicitly_unavailable(tmp_path):
    async def run():
        from telethon.tl import types
        from telegram_scraper.evidence import EvidenceStore
        empty = types.MessageEmpty(id=2, peer_id=types.PeerChannel(123))
        engine, store, _ = service(tmp_path, ProtocolClient([Message(1), empty]))
        result = await engine.run("sync")
        assert result["status"] == "warning"
        assert store.records[2]["source_availability"] == "empty_telegram_object"
        assert result["coverage"]["scans"][0]["empty_object_count"] == 1
        assert EvidenceStore(tmp_path).message_observations(123, 2)[0]["payload"]["_"] == "MessageEmpty"
    asyncio.run(run())


def test_evidence_only_crash_data_is_snapshotted_and_checkpoint_is_not_resume_claim(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        evidence = EvidenceStore(tmp_path)
        old = evidence.begin_run({"mode": "sync", "channel_id": 123})
        evidence.append_observation(old, "message", 9, {"_": "Message", "id": 9, "message": "survived crash"})
        engine, store, _ = service(tmp_path, ProtocolClient([Message(1)]), archive_before_sync=True)
        result = await engine.run("sync")
        assert list(store.archives_dir.glob("*.zip"))
        receipt = evidence.run(result["run_id"])
        assert receipt["checkpoint"]["last_derived_checkpoint_at"]
        assert "not an automatic resume cursor" in receipt["checkpoint"]["recovery_policy"]
        assert evidence.message_observations(123, 9)
    asyncio.run(run())


def test_watch_preserves_each_intermediate_raw_edit_before_coalescing(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        client = Client([Message(1, text="original")])
        watching = asyncio.Event()
        engine, store, _ = service(tmp_path, client, report=lambda update: watching.set() if update.get("phase") == "watching" else None)
        async def edits():
            for text in ("first edit", "second edit", "third edit"):
                await client.handlers[1][0](SimpleNamespace(message=Message(1, text=text)))
        client.during_history = edits
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 3)
        engine.cancel()
        await asyncio.wait_for(task, 3)
        assert store.records[1]["text"] == "third edit"
        texts = {observation["payload"]["message"] for observation in EvidenceStore(tmp_path).message_observations(123, 1)}
        assert texts == {"original", "first edit", "second edit", "third edit"}
    asyncio.run(run())


def test_watch_stop_drains_inflight_raw_receipt_before_closing_run(tmp_path):
    async def run():
        from telegram_scraper.evidence import EvidenceStore
        entered, release = threading.Event(), threading.Event()
        class SlowEvidence(EvidenceStore):
            def append_observation(self, *args, **kwargs):
                if (kwargs.get("context") or {}).get("source") == "watch":
                    entered.set()
                    release.wait(timeout=3)
                return super().append_observation(*args, **kwargs)
        watching = asyncio.Event()
        client = Client([Message(1)])
        engine, _, _ = service(tmp_path, client, report=lambda update: watching.set() if update.get("phase") == "watching" else None)
        engine.evidence = SlowEvidence(tmp_path)
        job = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 3)
        receiver = asyncio.create_task(client.handlers[0][0](SimpleNamespace(message=Message(2))))
        await asyncio.to_thread(entered.wait, 3)
        engine.cancel()
        await asyncio.sleep(0.02)
        assert not job.done()
        release.set()
        await asyncio.wait_for(receiver, 3)
        result = await asyncio.wait_for(job, 3)
        assert result["status"] == "cancelled"
        assert engine.evidence.message_observations(123, 2)
        assert engine.evidence.run(result["run_id"])["status"] == "cancelled"
    asyncio.run(run())
