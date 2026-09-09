import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from telegram_scraper.config import ConfigError, Settings, default_root
from telegram_scraper.server import LocalServer, Runtime


@pytest.fixture
def application(tmp_path):
    data = tmp_path / "telegram_data"
    (data / "media").mkdir(parents=True)
    (data / "media" / "lesson.mp4").write_bytes(b"0123456789abcdef")
    (data / "media" / "chart.png").write_bytes(b"image")
    records = [
        {"id": 1, "date": "2025-01-01T20:00:00+00:00", "text": "Market structure", "media_file": "telegram_data/media/lesson.mp4"},
        {"id": 2, "date": "2025-01-02T00:30:00+02:00", "text": "<script>alert(1)</script> gap", "media_file": "media/chart.png"},
        {"id": 3, "date": "2025-01-02T12:00:00+00:00", "text": "Liquidity lesson"},
        {"id": 4, "date": "2025-01-03T00:00:00+00:00", "text": "Missing lesson", "media_file": "media/missing.mp4"},
        {"id": 5, "date": None, "text": "Unsafe media", "media_file": "../../.telegram-scraper.json"},
    ]
    (data / "messages_all.json").write_text(json.dumps(records))
    runtime = Runtime(tmp_path)
    server = LocalServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield runtime, server
    server.shutdown()
    server.server_close()
    runtime.close()
    thread.join(timeout=2)


def request(application, path, method="GET", body=None, headers=None, authenticated=True):
    _, server = application
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    supplied = {"Cookie": f"{server.cookie_name}={server.token}"} if authenticated else {}
    if method == "POST":
        supplied.update({"Content-Type": "application/json", "X-CSRF-Token": server.token})
    supplied.update(headers or {})
    connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=supplied)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def payload(application, path):
    status, _, body = request(application, path)
    assert status == 200
    return json.loads(body)


def test_browse_offline_no_connection_no_mutation(application):
    runtime, _ = application
    before = runtime.store.messages_file.read_bytes()
    state = payload(application, "/api/state")
    assert state["library"]["total"] == 5
    assert state["library"]["videos"] == 2
    assert not state["connection"]["authorized"]
    assert runtime.service is None
    posts = payload(application, "/api/posts")
    assert [post["id"] for post in posts["posts"]] == [5, 4, 3, 2, 1]
    assert runtime.store.messages_file.read_bytes() == before


def test_search_media_and_pagination(application):
    result = payload(application, "/api/posts?q=LESSON&offset=1&limit=1")
    assert result["total"] == 2
    assert [post["id"] for post in result["posts"]] == [3]
    assert payload(application, "/api/posts?kind=video")["total"] == 2
    assert payload(application, "/api/posts?kind=photo")["total"] == 1
    assert payload(application, "/api/posts?q=999")["total"] == 0


def test_dates_are_inclusive_utc_and_handle_timezone_offsets(application):
    result = payload(application, "/api/posts?start=2025-01-01&end=2025-01-01")
    assert [post["id"] for post in result["posts"]] == [2, 1]
    assert request(application, "/api/posts?start=2025-01-03&end=2025-01-01")[0] == 400
    assert request(application, "/api/posts?start=bogus")[0] == 400


def test_private_settings_not_exposed_and_persist(tmp_path):
    settings = Settings(tmp_path)
    secret = "a" * 32
    settings.update({"api_id": 12345, "api_hash": secret, "channel": "@channelname", "download_media": False})
    reopened = Settings(tmp_path)
    assert reopened.values["api_hash"] == secret
    assert not reopened.public()["download_media"]
    assert secret not in json.dumps(reopened.public())
    reopened.update({"api_hash": "", "archive_before_sync": False})
    assert Settings(tmp_path).values["api_hash"] == secret
    assert settings.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("data", [{"api_id": -1}, {"api_hash": "bad"}, {"channel": "https://evil.test/private"}, {"download_media": "false"}, {"legacy_channel": "@other"}])
def test_bad_settings_not_written(tmp_path, data):
    settings = Settings(tmp_path)
    with pytest.raises(ConfigError):
        settings.update(data)
    assert not settings.path.exists()


def test_future_settings_fail_closed(tmp_path):
    path = tmp_path / ".telegram-scraper.json"
    original = '{"schema_version":999,"important":"keep"}'
    path.write_text(original)
    with pytest.raises(ConfigError):
        Settings(tmp_path)
    assert path.read_text() == original


def test_localhost_access_and_csrf_required(application):
    assert request(application, "/api/state", authenticated=False)[0] == 403
    assert request(application, "/api/state", headers={"Host": "evil.test"})[0] == 403
    assert request(application, "/api/jobs", "POST", {"mode": "verify"}, headers={"X-CSRF-Token": "wrong"})[0] == 403
    assert request(application, "/api/jobs", "POST", {"mode": "verify"}, headers={"Origin": "https://evil.test"})[0] == 403
    assert request(application, "/api/jobs", "POST", [], headers={})[0] == 400


def test_host_must_be_loopback(application):
    runtime, _ = application
    with pytest.raises(ValueError):
        LocalServer(("0.0.0.0", 0), runtime)


def test_media_streaming_ranges_and_head(application):
    code, headers, body = request(application, "/api/media/1", headers={"Range": "bytes=2-5"})
    assert (code, body) == (206, b"2345")
    assert headers["Content-Range"] == "bytes 2-5/16"
    assert headers["Accept-Ranges"] == "bytes"
    assert request(application, "/api/media/1", headers={"Range": "bytes=-3"})[2] == b"def"
    assert request(application, "/api/media/1", headers={"Range": "bytes=15-99"})[2] == b"f"
    assert request(application, "/api/media/1", headers={"Range": "bytes=99-"})[0] == 416
    assert request(application, "/api/media/1", headers={"Range": "bytes=-0"})[0] == 416
    assert request(application, "/api/media/1", "HEAD")[2] == b""


def test_missing_or_unsafe_media_never_served(application):
    assert request(application, "/api/media/4")[0] == 404
    assert request(application, "/api/media/5")[0] == 404
    assert payload(application, "/api/posts/4")["media_missing"]
    assert payload(application, "/api/posts/5")["media_url"] is None
    assert request(application, "/../.telegram-scraper.json")[0] == 404


def test_export_is_download_and_exact_original(application):
    runtime, _ = application
    code, headers, body = request(application, "/api/export")
    assert code == 200
    assert "attachment" in headers["Content-Disposition"]
    assert body == runtime.store.messages_file.read_bytes()


def test_scrape_requires_explicit_connection(application):
    response = request(application, "/api/jobs", "POST", {"mode": "sync"})
    assert response[0] == 400
    assert "Connect" in json.loads(response[2])["error"]
    assert application[0].service is None


def test_existing_library_channel_cannot_be_switched(application):
    response = request(application, "/api/settings", "POST", {"channel": "@different"})
    assert response[0] == 400
    assert "another channel" in json.loads(response[2])["error"]


def test_corrupt_library_stays_visible_as_error_and_unchanged(tmp_path):
    folder = tmp_path / "telegram_data"
    folder.mkdir()
    file = folder / "messages_all.json"
    file.write_bytes(b"[{broken")
    app = Runtime(tmp_path)
    try:
        assert app.call(app.state())["library"]["error"]
        with pytest.raises(ValueError):
            app.call(app.posts({}))
        assert file.read_bytes() == b"[{broken"
    finally:
        app.close()


def test_default_root_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_HOME", str(tmp_path))
    assert default_root() == tmp_path


def test_cookie_isolation_for_two_library_ports(application, tmp_path):
    runtime = Runtime(tmp_path / "second")
    second = LocalServer(("127.0.0.1", 0), runtime)
    try:
        first = application[1]
        assert first.cookie_name != second.cookie_name
        cookies = f"{first.cookie_name}={first.token}; {second.cookie_name}={second.token}"
        assert request(application, "/api/state", headers={"Cookie": cookies})[0] == 200
    finally:
        second.server_close()
        runtime.close()


def test_export_rejects_replaced_symlink(application, tmp_path):
    app = application[0]
    external = tmp_path / "private.json"
    external.write_text('{"private":true}')
    app.store.messages_file.unlink()
    app.store.messages_file.symlink_to(external)
    status, _, body = request(application, "/api/export")
    assert status == 400
    assert b'"private"' not in body


@pytest.mark.parametrize("value", [None, 123])
def test_channel_type_validation_before_existing_library_guard(application, value):
    assert request(application, "/api/settings", "POST", {"channel": value})[0] == 400


def test_invalid_saved_switch_cannot_enable_downloads(tmp_path):
    file = tmp_path / ".telegram-scraper.json"
    file.write_text('{"download_media":"false"}')
    with pytest.raises(ConfigError):
        Settings(tmp_path)
    assert file.read_text() == '{"download_media":"false"}'


def test_stop_before_job_starts_cannot_be_lost(application):
    app = application[0]
    app.connection = {"authorized": True, "step": "connected"}
    app.settings.values["channel"] = "@example"

    async def stop_immediately():
        await app.start_job({"mode": "sync"})
        await app.stop_job()
        await app.task

    app.call(stop_immediately())
    assert app.service is None
    assert app.job["status"] == "cancelled"
    assert not app.job["running"]


def test_preference_save_keeps_connection(application):
    app = application[0]
    class Service:
        settings = {}
        async def close(self):
            raise AssertionError("Preference save must keep the session connected")
    service = Service()
    app.service = service
    app.connection = {"authorized": True, "step": "connected"}
    try:
        app.call(app.update_settings({"download_media": False, "archive_before_sync": False}))
        assert app.service is service
        assert app.connection["authorized"]
        assert service.settings["download_media"] is False
    finally:
        app.service = None


def test_revoked_session_clears_connected_badge(application):
    from telegram_scraper.engine import AuthRequired
    app = application[0]
    class Service:
        async def run(self, *args, **kwargs):
            raise AuthRequired("Your session expired. Sign in again.")
    app.service = Service()
    app.connection = {"authorized": True, "step": "connected"}
    try:
        app.call(app._run_job("sync", "", ""))
        assert not app.connection["authorized"]
        assert app.connection["step"] == "phone"
        assert app.job["status"] == "error"
    finally:
        app.service = None
