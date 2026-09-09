import json
import hashlib
import asyncio
from http.client import HTTPConnection
import sqlite3
import threading
import zipfile
import pytest

from telegram_scraper.server import Runtime, LocalServer
from telegram_scraper.evidence import EvidenceStore
from telegram_scraper.storage import Store


def saved_library(root):
    store = Store(root)
    store.load()
    store.bind_channel(123, "Fixture channel", "@fixture")
    store.upsert({"id": 1, "date": "2025-01-01T00:00:00+00:00", "text": "A legacy post"})
    store.upsert({"id": 2, "date": "2025-01-02T00:00:00+00:00", "text": "Full source post", "source_channel_id": 123,
                  "raw": {"_": "Message", "id": 2, "message": "Full source post", "views": 17}, "last_observed_at": "2026-09-08T00:00:00+00:00"})
    store.save()
    evidence = EvidenceStore(root)
    run_id = evidence.begin_run({"mode": "sync", "channel_id": 123})
    evidence.append_observation(run_id, "message", 2, {"_": "Message", "id": 2, "views": 17}, tl_bytes=b"\x01\x02")
    evidence.append_observation(run_id, "comment_page", "123:2", {"messages": [{"id": 100, "message": "Comment"}]},
                                context={"parent_channel_id": 123, "parent_message_id": 2})
    evidence.finish_run(run_id, coverage={"history_scan_complete": True}, counts={"processed": 2})
    return run_id


def test_health_distinguishes_legacy_from_actual_raw_capture(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        health = runtime.call(runtime.health())
        assert health["legacy_posts"] == 1
        assert health["posts_with_raw_metadata"] == 1
        assert health["media_with_recorded_checksums"] == 0
        assert health["runs"][0]["coverage"]["history_scan_complete"]
        assert health["evidence"]["message_observations"] == 1
    finally:
        runtime.close()


def test_source_detail_contains_returned_fields_and_related_comments(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        post = runtime.call(runtime.post(2))
        assert post["structured_content"]["views"] == 17
        assert post["source"]["has_raw"]
        source = runtime.call(runtime.source_post(2))
        assert source["record"]["raw"]["views"] == 17
        assert len(source["observations"]) == 1
        assert any(value["kind"] == "comment_page" for value in source["related_observations"])
        assert source["complete_export_url"] == "/api/evidence/export"
    finally:
        runtime.close()


def test_capture_receipt_survives_runtime_restart(tmp_path):
    run_id = saved_library(tmp_path)
    first = Runtime(tmp_path)
    first.close()
    second = Runtime(tmp_path)
    try:
        assert second.call(second.health())["runs"][0]["run_id"] == run_id
    finally:
        second.close()


def test_unfinished_run_is_not_presented_as_running_after_restart(tmp_path):
    evidence = EvidenceStore(tmp_path)
    evidence.begin_run({"mode": "sync", "channel_id": 123})
    runtime = Runtime(tmp_path)
    try:
        assert runtime.call(runtime.health())["runs"][0]["display_status"] == "interrupted"
    finally:
        runtime.close()


def test_corrupt_evidence_blocks_new_captures_without_hiding_legacy_posts(tmp_path):
    saved_library(tmp_path)
    file = tmp_path / "telegram_data" / "evidence.sqlite3"
    file.write_bytes(b"broken evidence")
    runtime = Runtime(tmp_path)
    try:
        assert runtime.call(runtime.posts({}))["total"] == 2
        assert runtime.call(runtime.health())["error"]
        with pytest.raises(ValueError):
            runtime.call(runtime.start_job({"mode": "sync"}))
        assert file.read_bytes() == b"broken evidence"
    finally:
        runtime.close()


def test_poll_stays_structured_without_executing_content(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        runtime.store.records[2]["raw"]["media"] = {"poll": {"question": {"text": "<script>Poll?</script>"}, "answers": [{"text": {"text": "A"}, "option": {"encoding": "base64", "data": "AA=="}}]}, "results": {"total_voters": 7}}
        post = runtime.call(runtime.post(2))
        assert post["structured_content"]["poll"]["question"]["text"] == "<script>Poll?</script>"
        assert post["structured_content"]["poll_results"]["total_voters"] == 7
    finally:
        runtime.close()


def test_health_does_not_send_every_seen_id_on_each_poll():
    result = Runtime.compact_run({"run_id": "run", "metadata": {"mode": "sync", "channel": "private"},
                                  "coverage": {"seen_ids": list(range(10000))}, "message_ids": list(range(10000))})
    assert len(json.dumps(result)) < 500
    assert "private" not in json.dumps(result)


@pytest.fixture
def source_http(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    server = LocalServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def get(path, authenticated=True):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        headers = {"Cookie": f"{server.cookie_name}={server.token}"} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result
    yield runtime, get
    server.shutdown()
    server.server_close()
    thread.join()
    runtime.close()


def test_http_source_and_complete_export_keep_large_identifiers_exact(source_http):
    runtime, get = source_http
    large = 9223372036854775807
    runtime.store.records[2]["raw"]["grouped_id"] = large
    code, _, body = get("/api/posts/2/source")
    assert code == 200
    assert str(large).encode() in body
    assert json.loads(body)["record"]["raw"]["grouped_id"] == large
    code, headers, body = get("/api/evidence/export")
    assert code == 200 and "attachment" in headers["Content-Disposition"]
    exported = json.loads(body)
    assert exported and b"Comment" in body
    assert get("/api/evidence/export", authenticated=False)[0] == 403
    run_id = runtime.evidence.runs()[0]["run_id"]
    assert get(f"/api/evidence/export?run={run_id}")[0] == 200
    assert get("/api/evidence/export?run=unknown")[0] in {400, 404}


def test_variant_http_cannot_escape_library(source_http, tmp_path):
    runtime, get = source_http
    folder = tmp_path / "telegram_data" / "media" / "variants"
    folder.mkdir(parents=True)
    data = b"a saved thumbnail"
    filename = hashlib.sha256(data).hexdigest() + ".bin"
    (folder / filename).write_bytes(data)
    (folder / "link.bin").symlink_to(tmp_path / ".telegram-scraper.json")
    runtime.store.records[2]["context_capture"] = {"media_variants": [
        {"media_file": f"telegram_data/media/variants/{filename}", "state": "downloaded"},
        {"media_file": "../../.telegram-scraper.json", "state": "downloaded"},
        {"media_file": "telegram_data/media/variants/link.bin", "state": "downloaded"}]}
    assert get("/api/posts/2/variants/0")[2] == data
    assert get("/api/posts/2/variants/1")[0] == 404
    assert get("/api/posts/2/variants/2")[0] == 404
    assert get("/api/posts/2/variants/900")[0] == 404


def test_raw_only_crash_evidence_qualifies_for_a_complete_backup(tmp_path):
    evidence = EvidenceStore(tmp_path)
    run_id = evidence.begin_run({"mode": "sync", "channel_id": 123})
    evidence.append_observation(run_id, "history_page", "page1", {"messages": [{"id": 1, "message": "Saved before processing"}]})
    store = Store(tmp_path)
    store.load()
    assert not store.messages_file.exists()
    assert store.has_snapshot_data()
    snapshot = store.snapshot()
    with zipfile.ZipFile(snapshot) as archive:
        assert archive.read("evidence.sqlite3") == evidence.path.read_bytes()
    assert not store.messages_file.exists()


def test_recovered_evidence_error_clears_after_a_successful_read(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    try:
        runtime.evidence_error = "An earlier check failed"
        with runtime.store.lock():
            runtime.recover_evidence()
        assert runtime.call(runtime.health())["error"] is None
    finally:
        runtime.close()


def test_health_does_not_clear_real_payload_corruption_after_shallow_reads(tmp_path):
    saved_library(tmp_path)
    database = tmp_path / "telegram_data" / "evidence.sqlite3"
    # Simulate on-disk corruption while retaining the precise supported schema.
    # This is a disposable fixture; production observations remain append-only.
    with sqlite3.connect(database) as connection:
        triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='observations'").fetchall()
        for name, _ in triggers:
            connection.execute('DROP TRIGGER "' + name + '"')
        connection.execute("UPDATE observations SET payload_json=? WHERE kind='message'", ('{"message":"altered fixture"}',))
        for _, sql in triggers:
            connection.execute(sql)
    runtime = Runtime(tmp_path)
    try:
        assert runtime.evidence_error and "checksum" in runtime.evidence_error
        first_error = runtime.evidence_error
        for _ in range(2):
            health = runtime.call(runtime.health())
            assert health["error"] == first_error
            assert runtime.evidence_error == first_error
        runtime.connection["authorized"] = True
        with pytest.raises(ValueError, match="checksum"):
            runtime.call(runtime.start_job({"mode": "sync"}))
        assert runtime.call(runtime.posts({}))["total"] == 2
    finally:
        runtime.close()


def test_slow_media_health_read_does_not_block_stop_or_event_loop(tmp_path, monkeypatch):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = runtime.store.media_path
    runtime.store.records[2]["has_media"] = True
    def slow_media_path(record):
        entered.set()
        assert release.wait(5), "Fixture media read was not released"
        return original(record)
    monkeypatch.setattr(runtime.store, "media_path", slow_media_path)
    future = asyncio.run_coroutine_threadsafe(runtime.health(), runtime.loop)
    try:
        assert entered.wait(3), "Health never reached the fixture disk read"
        # Stop must remain usable even while a filesystem request is blocked.
        result = runtime.call(runtime.stop_job(), timeout=1)
        assert result["job"]["running"] is False
        assert not future.done()
    finally:
        release.set()
        future.result(timeout=5)
        runtime.close()


def test_display_metadata_preserves_64_bit_album_and_run_identifiers(tmp_path):
    saved_library(tmp_path)
    runtime = Runtime(tmp_path)
    large = 9223372036854775807
    try:
        runtime.store.records[2]["raw"]["grouped_id"] = large
        post = runtime.call(runtime.post(2))
        assert post["structured_content"]["album_id"] == str(large)
        compact = Runtime.compact_run({"run_id": "fixture", "metadata": {"mode": "sync", "channel_id": large},
                                       "coverage": {"peer_id": large}})
        assert compact["metadata"]["channel_id"] == str(large)
        assert compact["coverage"]["peer_id"] == str(large)
    finally:
        runtime.close()
