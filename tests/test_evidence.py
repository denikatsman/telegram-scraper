"""Durability and provenance tests use disposable libraries only."""

import base64
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from telegram_scraper.evidence import EvidenceError, EvidenceStore, _digest
from telegram_scraper.storage import Store


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = EvidenceStore(self.root)

    def begin(self, channel_id=99, **metadata):
        return self.evidence.begin_run({"mode": "sync", "channel_id": channel_id, **metadata})

    def test_constructor_reads_and_exports_do_not_create_a_database(self):
        self.assertFalse(self.evidence.validate()["exists"])
        self.assertEqual(self.evidence.stats()["observations"], 0)
        self.assertEqual(self.evidence.runs(), [])
        self.assertEqual(self.evidence.message_observations(99, 1), [])
        self.assertEqual(self.evidence.related_observations(99, 1), [])
        self.assertEqual(self.evidence.export()["observations"], [])
        output = io.BytesIO()
        self.evidence.write_export(output)
        self.assertEqual(json.loads(output.getvalue()), self.evidence.export())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_every_raw_change_is_preserved_including_volatile_fields_and_tl_bytes(self):
        run = self.begin(client_version="fixture-1", channel="@fixture")
        raw = {"id": 1, "message": "same", "views": 10, "file_reference": {"base64": "AA=="}, "unknown_future_field": {"keep": [1, None, False]}}
        first = self.evidence.append_observation(run, "message", 1, raw, b"\x01original")
        second = self.evidence.append_observation(run, "message", 1, {**raw, "views": 11}, b"\x01original")
        third = self.evidence.append_observation(run, "message", 1, {**raw, "views": 11}, b"\x02changed")
        fourth = self.evidence.append_observation(run, "message", 1, {**raw, "file_reference": {"base64": "AQ=="}}, b"\x01original")
        self.assertEqual(len({first, second, third, fourth}), 4)
        items = self.evidence.message_observations(99, 1)
        self.assertEqual(items[0]["payload"], raw)
        self.assertEqual(base64.b64decode(items[2]["tl_bytes"]["data"]), b"\x02changed")
        self.assertTrue(self.evidence.validate()["ok"])

    def test_equal_payloads_are_deduplicated_without_losing_repeat_or_run_associations(self):
        first_run, second_run = self.begin(), self.begin()
        first = self.evidence.append_observation(first_run, "message", 1, {"b": 2, "a": 1}, observed_at="2025-01-01T12:00:00+00:00", context={"scan_id": "first"})
        second = self.evidence.append_observation(first_run, "message", 1, {"a": 1, "b": 2}, observed_at="2025-01-01T12:01:00+00:00", context={"scan_id": "repeat"})
        third = self.evidence.append_observation(second_run, "message", 1, {"a": 1, "b": 2}, context={"scan_id": "second_run"})
        self.assertEqual((first, second, third), (first, first, first))
        items = self.evidence.message_observations(99, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["occurrences"]), 3)
        self.assertEqual(items[0]["occurrences"][1]["context"]["scan_id"], "repeat")
        self.assertEqual(self.evidence.run(first_run)["observed_messages"], 1)
        self.assertEqual(self.evidence.run(first_run)["occurrences"], 2)
        self.assertEqual(len(self.evidence.export(second_run)["occurrences"]), 1)

    def test_same_message_id_in_another_channel_is_independent(self):
        first, other = self.begin(99), self.begin(100)
        a = self.evidence.append_observation(first, "message", 1, {"id": 1})
        b = self.evidence.append_observation(other, "message", 1, {"id": 1})
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.evidence.message_observations(99, 1)), 1)
        self.assertEqual(self.evidence.message_observations(100, 1)[0]["observation_id"], b)

    def test_none_and_empty_tl_bytes_remain_distinct(self):
        run = self.begin()
        a = self.evidence.append_observation(run, "message", 1, {}, None)
        b = self.evidence.append_observation(run, "message", 1, {}, b"")
        self.assertNotEqual(a, b)
        items = self.evidence.message_observations(99, 1)
        self.assertIsNone(items[0]["tl_bytes"])
        self.assertEqual(items[1]["tl_bytes"], {"encoding": "base64", "data": ""})

    def test_json_shapes_unicode_and_large_integers_are_lossless(self):
        run = self.begin()
        raw = {"empty": {}, "list": [], "null": None, "flag": False, "zero": 0,
               "large": 2**100, "text": "你好 😀", "isolated_surrogate": "\ud800"}
        self.evidence.append_observation(run, "message", 1, raw)
        self.assertEqual(self.evidence.message_observations(99, 1)[0]["payload"], raw)
        self.assertTrue(self.evidence.validate()["ok"])

    def test_unsupported_values_fail_before_publishing_observations(self):
        run = self.begin()
        for value in ({1: "ambiguous key"}, {"bad": object()}, {"nan": float("nan")}, {"tuple": (1, 2)}, {"bytes": b"tag me"}):
            with self.subTest(value=type(value)):
                with self.assertRaises(EvidenceError):
                    self.evidence.append_observation(run, "message", 1, value)
        self.assertEqual(self.evidence.stats()["observations"], 0)

    def test_observation_and_occurrence_are_one_atomic_transaction(self):
        run = self.begin()
        original_connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                if sql.startswith("INSERT INTO occurrences"):
                    raise sqlite3.OperationalError("injected disk failure")
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=FailingConnection)

        with patch("telegram_scraper.evidence.sqlite3.connect", side_effect=connect):
            with self.assertRaises(EvidenceError):
                self.evidence.append_observation(run, "message", 1, {"id": 1})
        self.assertEqual(self.evidence.stats()["observations"], 0)
        self.assertEqual(self.evidence.stats()["occurrences"], 0)
        self.assertTrue(self.evidence.validate()["ok"])

    def test_committed_observation_survives_a_fresh_process(self):
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {"id": 1, "text": "durable"}, b"exact")
        script = "from pathlib import Path; from telegram_scraper.evidence import EvidenceStore; import json,sys; e=EvidenceStore(Path(sys.argv[1])); print(json.dumps(e.export()))"
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["observations"][0]["payload"]["text"], "durable")
        self.assertEqual(value["runs"][0]["status"], "running")
        self.assertIsNone(value["runs"][0]["ended_at"])

    def test_crashed_transaction_recovers_without_losing_committed_observations(self):
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {"id": 1, "text": "committed"})
        script = """from pathlib import Path
from telegram_scraper.evidence import EvidenceStore
import os,sqlite3,sys
path=EvidenceStore(Path(sys.argv[1])).path
db=sqlite3.connect(path,isolation_level=None)
db.execute('PRAGMA cache_size=1')
db.execute('PRAGMA synchronous=FULL')
db.execute('BEGIN IMMEDIATE')
for n in range(100):
    db.execute('INSERT INTO run_events(run_id,occurred_at,event_json) VALUES (?,?,?)',(sys.argv[2],'uncommitted','x'*10000))
os._exit(12)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), run], timeout=5)
        self.assertEqual(result.returncode, 12)
        self.assertTrue(Path(str(self.evidence.path) + "-journal").exists())
        reopened = EvidenceStore(self.root)
        self.assertTrue(reopened.prepare()["ok"])
        self.assertEqual(len(reopened.export()["events"]), 1)
        self.assertEqual(reopened.message_observations(99, 1)[0]["payload"]["text"], "committed")
        self.assertEqual(reopened.run(run)["status"], "running")
        self.assertFalse(Path(str(self.evidence.path) + "-journal").exists())

    def test_runs_keep_requested_range_checkpoints_coverage_issues_and_update_history(self):
        run = self.begin(mode="range", start="2025-01-01", end="2025-01-02", api_layer=200)
        self.evidence.update_run(run, counts={"processed": 4}, checkpoint={"last_durable_id": 42})
        self.evidence.append_event(run, {"type": "message_processed", "message_id": 42, "scan_id": "a"})
        self.evidence.update_run(run, counts={"processed": 5}, checkpoint={"last_durable_id": 41})
        self.evidence.finish_run(run, "warning", coverage={"history_exhausted": True, "accessible_message_count": 5, "attachments_complete": False}, issues=[{"type": "download", "message_id": 41}])
        result = self.evidence.run(run)
        self.assertEqual(result["metadata"]["end"], "2025-01-02")
        self.assertEqual(result["checkpoint"]["last_durable_id"], 41)
        self.assertEqual(result["counts"]["processed"], 5)
        self.assertEqual(result["status"], "warning")
        self.assertIsNotNone(result["ended_at"])
        self.assertFalse(result["coverage"]["attachments_complete"])
        events = self.evidence.export(run)["events"]
        self.assertEqual(events[1]["event"]["fields"]["checkpoint"]["last_durable_id"], 42)
        for action in (lambda: self.evidence.append_event(run, {}), lambda: self.evidence.append_observation(run, "message", 1, {}),
                       lambda: self.evidence.update_run(run, checkpoint={"changed": True}), lambda: self.evidence.finish_run(run)):
            with self.assertRaises(EvidenceError):
                action()

    def test_channel_binding_is_required_and_cannot_change_midrun(self):
        run = self.begin(None)
        self.evidence.append_observation(run, "session_context", "capture", {"protocol": "fixture"})
        with self.assertRaises(EvidenceError):
            self.evidence.append_observation(run, "message", 1, {})
        self.evidence.update_run(run, channel_id=99)
        self.evidence.append_observation(run, "message", 1, {})
        with self.assertRaises(EvidenceError):
            self.evidence.update_run(run, channel_id=100)
        self.assertEqual(self.evidence.run(run)["channel_id"], 99)

    def test_unknown_schema_unrelated_and_corrupt_databases_fail_closed(self):
        self.begin()
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            connection.execute("PRAGMA user_version=999")
        before = self.evidence.path.read_bytes()
        for action in (self.evidence.validate, self.evidence.prepare, lambda: self.begin(), self.evidence.stats):
            with self.assertRaises(EvidenceError):
                action()
            self.assertEqual(self.evidence.path.read_bytes(), before)
        self.evidence.path.write_bytes(b"not a SQLite database")
        with self.assertRaises(EvidenceError):
            self.begin()
        self.assertEqual(self.evidence.path.read_bytes(), b"not a SQLite database")

    def test_missing_schema_object_and_wrong_raw_checksum_block_writes(self):
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {"text": "original"})
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name='observations_no_update'").fetchone()[0]
            connection.execute("DROP TRIGGER observations_no_update")
        with self.assertRaises(EvidenceError):
            self.evidence.append_observation(run, "message", 2, {})
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            connection.execute("UPDATE observations SET payload_json='{}'")
            connection.execute(trigger)
        with self.assertRaisesRegex(EvidenceError, "checksum"):
            self.evidence.validate()
        with self.assertRaises(EvidenceError):
            self.evidence.append_observation(run, "message", 2, {})

    def test_append_only_tables_reject_update_and_delete(self):
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {})
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            for statement in ("DELETE FROM observations", "UPDATE observations SET payload_json='{}'", "DELETE FROM occurrences", "UPDATE occurrences SET observed_at='changed'", "DELETE FROM run_events", "UPDATE run_events SET event_json='{}'", "DELETE FROM runs", "UPDATE runs SET metadata_json='{}'"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement)
        self.assertTrue(self.evidence.validate()["ok"])

    def test_replaced_immutability_trigger_is_detected_even_with_same_name(self):
        self.begin()
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            connection.execute("DROP TRIGGER observations_no_update")
            connection.execute("CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations BEGIN SELECT 1; END")
        with self.assertRaises(EvidenceError):
            self.evidence.validate()
        with self.assertRaises(EvidenceError):
            self.begin()

    def test_invalid_run_provenance_shape_is_not_treated_as_valid_evidence(self):
        run = self.begin()
        with closing(sqlite3.connect(self.evidence.path)) as connection, connection:
            connection.execute("UPDATE runs SET coverage_json='[]' WHERE run_id=?", (run,))
        with self.assertRaises(EvidenceError):
            self.evidence.validate()
        with self.assertRaises(EvidenceError):
            self.evidence.append_observation(run, "message", 1, {})

    def test_safe_database_path_and_missing_database_journal(self):
        self.evidence.data_dir.mkdir()
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        self.evidence.path.symlink_to(outside)
        with self.assertRaises(EvidenceError):
            self.begin()
        self.assertEqual(outside.read_bytes(), b"keep")
        self.evidence.path.unlink()
        journal = Path(str(self.evidence.path) + "-journal")
        journal.write_bytes(b"recovery")
        with self.assertRaises(EvidenceError):
            self.begin()
        self.assertEqual(journal.read_bytes(), b"recovery")
        self.assertFalse(self.evidence.path.exists())

    def test_concurrent_observations_deduplicate_payload_and_keep_all_occurrences(self):
        run = self.begin()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda n: self.evidence.append_observation(run, "message", 1, {"same": True}, context={"attempt": n}), range(20)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.evidence.stats()["occurrences"], 20)
        self.assertTrue(self.evidence.validate()["ok"])

    def test_complete_closed_database_is_included_in_regular_pre_sync_backup(self):
        store = Store(self.root)
        store.load()
        store.upsert({"id": 1, "text": "derived"})
        store.save()
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {"raw": "complete"}, b"wire")
        self.evidence.finish_run(run)
        self.evidence.prepare()
        archive = store.snapshot()
        with zipfile.ZipFile(archive) as zipped:
            self.assertIn("evidence.sqlite3", zipped.namelist())
            self.assertFalse(any(name.endswith(("-journal", "-wal", "-shm")) for name in zipped.namelist()))
            with tempfile.TemporaryDirectory() as restored:
                restored_evidence = EvidenceStore(Path(restored))
                restored_evidence.data_dir.mkdir()
                restored_evidence.path.write_bytes(zipped.read("evidence.sqlite3"))
                self.assertTrue(restored_evidence.validate()["ok"])
                self.assertEqual(restored_evidence.export(), self.evidence.export())

    def test_related_context_is_separate_and_indexed_by_parent_message(self):
        run = self.begin()
        primary = self.evidence.append_observation(run, "message", 1, {"id": 1})
        comment = self.evidence.append_observation(run, "discussion_message", 250, {"id": 250, "message": "Comment"}, context={"parent_channel_id": 99, "parent_message_id": 1})
        self.evidence.append_observation(run, "discussion_message", 300, {}, context={"parent_channel_id": 99, "parent_message_id": 2})
        related = self.evidence.related_observations(99, 1)
        self.assertEqual([item["observation_id"] for item in related], [primary, comment])
        self.assertEqual(len(self.evidence.related_observations(99, 1, limit=1)), 1)
        self.assertEqual(self.evidence.related_observations(99, 1, limit=1, offset=1)[0]["observation_id"], comment)

    def test_streaming_export_matches_complete_export_and_is_read_only(self):
        run, other = self.begin(), self.begin(100)
        self.evidence.append_observation(run, "message", 1, {"text": "hello"}, b"raw")
        self.evidence.append_observation(other, "channel", 100, {"title": "other"})
        before = self.evidence.path.read_bytes()
        for selected in (None, run):
            for output in (io.BytesIO(), io.StringIO()):
                self.evidence.write_export(output, selected)
                self.assertEqual(json.loads(output.getvalue()), self.evidence.export(selected))
        self.assertEqual(self.evidence.path.read_bytes(), before)

    def test_post_preview_bounds_observations_and_nested_occurrences_with_full_counts(self):
        run = self.begin()
        for version in range(8):
            for occurrence in range(5):
                self.evidence.append_observation(run, "message", 1, {"version": version}, context={"attempt": occurrence})
                self.evidence.append_observation(run, "comment_page", str(version), {"version": version},
                    context={"parent_channel_id": 99, "parent_message_id": 1, "attempt": occurrence})
        result = self.evidence.post_preview(99, 1, limit=2, related_limit=3, occurrence_limit=2)
        self.assertEqual(result["total_message_observations"], 8)
        self.assertEqual(result["total_related_observations"], 8)
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(len(result["related_observations"]), 3)
        self.assertEqual(result["observations"][0]["payload"]["version"], 7)
        for observation in result["observations"] + result["related_observations"]:
            self.assertEqual(len(observation["occurrences"]), 2)
            self.assertEqual(observation["occurrence_count"], 5)
            self.assertTrue(observation["occurrences_truncated"])
        self.assertEqual(len(self.evidence.export()["occurrences"]), 80)

    def test_slow_export_reader_does_not_block_capture_and_receives_consistent_snapshot(self):
        run = self.begin()
        self.evidence.append_observation(run, "message", 1, {"text": "first"})
        exporting, release = threading.Event(), threading.Event()

        class SlowOutput(io.BytesIO):
            def write(self, data):
                if not exporting.is_set():
                    exporting.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("test reader timed out")
                return super().write(data)

        output = SlowOutput()
        with ThreadPoolExecutor(max_workers=2) as pool:
            export = pool.submit(self.evidence.write_export, output)
            self.assertTrue(exporting.wait(timeout=3))
            try:
                capture = pool.submit(self.evidence.append_observation, run, "message", 2, {"text": "second"})
                capture.result(timeout=3)
            finally:
                release.set()
            export.result(timeout=3)
        self.assertEqual(len(json.loads(output.getvalue())["observations"]), 1)
        self.assertEqual(self.evidence.stats()["observations"], 2)


if __name__ == "__main__":
    unittest.main()
