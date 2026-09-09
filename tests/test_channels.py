"""Channel isolation and link resolution, using only temporary libraries."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from telegram_scraper.channels import Channels
from telegram_scraper.config import Settings, normalize_channel, ConfigError
from telegram_scraper.engine import TelegramService, UserError
from telegram_scraper.server import Runtime
from telegram_scraper.storage import Store, StoreError
from test_app import application, request


@pytest.mark.parametrize("value, expected", [
    ("  @ExampleChannel  ", "@examplechannel"),
    ("ExampleChannel", "@examplechannel"),
    ("https://t.me/ExampleChannel", "@examplechannel"),
    ("t.me/ExampleChannel/123", "@examplechannel"),
    ("https://telegram.me/ExampleChannel/123?single&comment=77", "@examplechannel"),
    ("http://www.t.me/ExampleChannel/123/?t=10", "@examplechannel"),
    ("t.me/ExampleChannel/12/123?t=1:30", "@examplechannel"),
    ("https://t.me/s/ExampleChannel/123", "@examplechannel"),
    ("https://t.me/s/ExampleChannel", "@examplechannel"),
    ("https://t.me/c/123/42", "-1000000000123"),
    ("https://t.me/c/1234567890/7/42?single", "-1001234567890"),
    ("tg://resolve?domain=ExampleChannel&post=123&thread=12", "@examplechannel"),
    ("tg://privatepost?channel=123&post=42", "-1000000000123"),
    ("https://t.me/+Example_Invite", "https://t.me/+Example_Invite"),
    ("https://t.me/joinchat/Example_Invite", "https://t.me/+Example_Invite"),
    ("tg://join?invite=Example_Invite", "https://t.me/+Example_Invite"),
    ("-1001234567890", "-1001234567890"),
])
def test_channel_and_message_links(value, expected):
    assert normalize_channel(value) == expected


@pytest.mark.parametrize("value", [
    "https://t.me.evil.test/example", "https://t.me@example.test/example",
    "https://example.test/t.me/example", "https://t.me:443/example",
    "https://t.me/example/../another", "https://t.me/example//123",
    "https://t.me/c/0/123", "https://t.me/c/123/0", "https://t.me/c/123/99999999999",
    "https://t.me/c/123", "https://t.me/example/nope", "https://t.me/example/s/123",
    "https://t.me/addstickers/example", "https://t.me/share/url?url=https://t.me/example",
    "https://t.me/+41791234567", "tg://proxy?server=host&port=80",
    "tg://resolve?domain=example&domain=other", "tg://resolve?domain=example&post=abc",
    "tg://resolve?domain=example&startapp=anything", "file:///example", "0",
    "https://t.me/example\n/123", "https://t.me\\@example.test/channel", None,
])
def test_non_channel_links_are_rejected(value):
    with pytest.raises(ConfigError):
        normalize_channel(value)


def primary_library(root):
    Settings(root).update({"channel": "@original", "api_id": 12345, "api_hash": "a" * 32})
    store = Store(root)
    store.load()
    store.upsert({"id": 1, "date": "2025-01-01T00:00:00+00:00", "text": "Original channel"})
    store.save()
    return store.messages_file.read_bytes()


def test_switching_preserves_each_channel_and_one_account(tmp_path):
    original = primary_library(tmp_path)
    runtime = Runtime(tmp_path)
    service = runtime.get_service()
    sentinel = SimpleNamespace(disconnect=lambda: asyncio.sleep(0))
    service._client = sentinel
    service._phone = "+41790000000"
    service._phone_code_hash = "pending-challenge"
    runtime.connection = {"authorized": True, "step": "connected"}
    try:
        result = runtime.call(runtime.add_channel({"channel": "https://t.me/secondchannel/123"}))
        key = result["active_channel"]
        assert result["added"]
        assert runtime.store.root == tmp_path / "channels" / key
        assert runtime.store.records == {}
        runtime.store.upsert({"id": 1, "text": "Second channel"})
        runtime.store.save()
        assert service._client is sentinel
        assert service._phone_code_hash == "pending-challenge"
        assert service.account_root == tmp_path
        assert service.root == runtime.store.root
        assert runtime.call(runtime.state())["settings"]["channel"] == "@secondchannel"
        runtime.call(runtime.update_settings({"channel": "@secondchannel", "download_media": False}))
        assert Settings(tmp_path).values["channel"] == "@original"
        runtime.call(runtime.select_channel({"id": "main"}))
        assert runtime.store.records[1]["text"] == "Original channel"
        assert runtime.connection["authorized"]
        again = runtime.call(runtime.add_channel({"channel": "@SECONDCHANNEL"}))
        assert not again["added"] and again["active_channel"] == key
        assert runtime.store.records[1]["text"] == "Second channel"
        assert not list((tmp_path / "channels").rglob("*.session"))
    finally:
        runtime.close()
    assert (tmp_path / "telegram_data/messages_all.json").read_bytes() == original
    reopened = Runtime(tmp_path)
    try:
        assert reopened.channels.active == key
        assert reopened.store.records[1]["text"] == "Second channel"
        assert reopened.channel_settings()["api_hash"] == "a" * 32
    finally:
        reopened.close()


def test_switch_does_not_reset_an_active_job_or_authentication(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        for field in ("job", "auth_busy", "library_busy"):
            if field == "job": runtime.job["running"] = True
            else: setattr(runtime, field, True)
            with pytest.raises(ValueError, match="operation"):
                runtime.call(runtime.add_channel({"channel": "@secondchannel"}))
            if field == "job": runtime.job["running"] = False
            else: setattr(runtime, field, False)
        assert not runtime.channels.path.exists()
    finally:
        runtime.close()


def test_failed_switch_retains_selection_and_corrupt_data(tmp_path):
    primary_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        key = runtime.call(runtime.add_channel({"channel": "@secondchannel"}))["active_channel"]
        runtime.call(runtime.select_channel({"id": "main"}))
        folder = runtime.channels.folder(key)
        (folder / "telegram_data").mkdir(exist_ok=True)
        index = folder / "telegram_data/messages_all.json"
        index.write_text("broken JSON")
        before = runtime.channels.path.read_bytes()
        with pytest.raises(StoreError):
            runtime.call(runtime.select_channel({"id": key}))
        assert runtime.channels.active == "main"
        assert runtime.channels.path.read_bytes() == before
        assert index.read_text() == "broken JSON"
    finally:
        runtime.close()


def test_missing_and_symlinked_channel_folders_are_not_recreated(tmp_path):
    catalogue = Channels(tmp_path)
    key, _ = catalogue.add("@example", "")
    folder = catalogue.folder(key)
    folder.rmdir()
    with pytest.raises(StoreError, match="missing"):
        catalogue.select(key)
    assert not folder.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StoreError, match="ordinary folders"):
        catalogue.select(key)
    with pytest.raises(StoreError):
        catalogue.folder("../../outside")


def test_channel_catalogue_write_failure_preserves_previous_state(tmp_path, monkeypatch):
    catalogue = Channels(tmp_path)
    def fail(*args):
        raise StoreError("disk full")
    monkeypatch.setattr("telegram_scraper.channels._atomic_json", fail)
    with pytest.raises(StoreError, match="disk full"):
        catalogue.add("@example", "")
    assert catalogue.values == {"version": 1, "active": "main", "channels": {}}
    assert list((tmp_path / "channels").iterdir()) == []


def test_project_and_selected_channel_are_both_locked(tmp_path):
    primary_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        with runtime.lock():
            runtime.call(runtime.add_channel({"channel": "@example"}))
            for root in (tmp_path, runtime.store.root):
                with pytest.raises(StoreError, match="already open"):
                    with Store(root).lock():
                        pytest.fail("a competing writer acquired the lock")
    finally:
        runtime.close()


def test_first_channel_uses_the_existing_project_archive(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        result = runtime.call(runtime.add_channel({"channel": "t.me/example/42"}))
        assert result == {"active_channel": "main", "added": True}
        assert runtime.store.root == tmp_path
        assert runtime.channel_settings()["channel"] == "@example"
        assert not (tmp_path / "channels").exists()
        assert len(runtime.call(runtime.state())["channels"]) == 1
    finally:
        runtime.close()


def test_stale_browser_cannot_read_or_modify_another_channel(application):
    runtime, _ = application
    runtime.settings.values["channel"] = "@original"
    original = runtime.store.messages_file.read_bytes()
    status, _, body = request(application, "/api/channels", "POST", {"channel": "t.me/secondchannel/123"}, headers={"X-Archive-Library": "main"})
    assert status == 200
    key = json.loads(body)["active_channel"]
    assert request(application, "/api/posts")[0] == 400
    assert request(application, "/api/media/1?library=main")[0] == 400
    assert request(application, "/api/export?library=main")[0] == 400
    assert request(application, "/api/evidence/export?library=main")[0] == 400
    assert request(application, "/api/settings", "POST", {"download_media": False}, headers={"X-Archive-Library": "main"})[0] == 400
    assert request(application, "/api/posts", headers={"X-Archive-Library": key})[0] == 200
    assert request(application, "/api/channels/select", "POST", {"id": "main"}, headers={"X-Archive-Library": key})[0] == 200
    assert request(application, "/api/media/1?library=main", headers={"Range": "bytes=0-3"})[2] == b"0123"
    assert runtime.store.messages_file.read_bytes() == original


def test_message_link_resolves_private_channel_from_existing_dialogs(tmp_path):
    entity = SimpleNamespace(id=123, title="Accessible private channel")
    class Client:
        async def get_entity(self, target):
            assert target == -1000000000123
            raise ValueError("not cached")
        async def iter_dialogs(self):
            yield SimpleNamespace(id=-1000000000123, entity=entity)
    service = TelegramService(tmp_path, {"channel": "https://t.me/c/123/42"}, Store(tmp_path), lambda _: None)
    assert asyncio.run(service._entity(Client())) is entity


def test_person_link_is_not_accepted_as_a_channel(tmp_path):
    class Client:
        async def get_entity(self, target):
            return SimpleNamespace(id=123, first_name="A person")
    service = TelegramService(tmp_path, {"channel": "@someone"}, Store(tmp_path), lambda _: None)
    with pytest.raises(UserError, match="person or bot"):
        asyncio.run(service._entity(Client()))


def test_cancelled_switch_keeps_lock_until_disk_worker_finishes(tmp_path, monkeypatch):
    primary_library(tmp_path)
    runtime = Runtime(tmp_path)
    key, _ = runtime.channels.add("@secondchannel", "@original")
    folder = runtime.channels.folder(key)
    entered, release = threading.Event(), threading.Event()
    original = Store.load
    def delayed_load(store):
        if store.root == folder:
            entered.set()
            assert release.wait(5)
        return original(store)
    monkeypatch.setattr(Store, "load", delayed_load)
    operation = asyncio.run_coroutine_threadsafe(runtime.select_channel({"id": key}), runtime.loop)
    try:
        assert entered.wait(3)
        operation.cancel()
        async def check():
            await asyncio.sleep(0)
            return runtime.library_busy
        assert runtime.call(check())
        with pytest.raises(StoreError, match="already open"):
            with Store(folder).lock():
                pytest.fail("cancelled work released its writer lock early")
    finally:
        release.set()
        runtime.close()
    assert Channels(tmp_path).active == "main"


def test_restart_does_not_replace_a_missing_selected_archive(tmp_path):
    primary_library(tmp_path)
    catalogue = Channels(tmp_path)
    key, _ = catalogue.add("@secondchannel", "@original")
    catalogue.select(key)
    folder = catalogue.folder(key)
    folder.rmdir()
    with pytest.raises(StoreError, match="missing"):
        Runtime(tmp_path)
    assert not folder.exists()


def test_legacy_channel_alias_is_recognized_without_changing_posts(tmp_path):
    original = primary_library(tmp_path)
    store = Store(tmp_path)
    store.bind_channel(123, "Original", "https://t.me/original/42", "@original")
    assert store.channel_info()["id"] == 123
    assert store.messages_file.read_bytes() == original
