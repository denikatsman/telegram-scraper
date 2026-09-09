"""Storage regression tests use temporary libraries; no personal archive access."""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from telegram_scraper.storage import OperationCancelled, Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)

    def write_records(self, records):
        self.store.data_dir.mkdir(parents=True, exist_ok=True)
        self.store.messages_file.write_text(json.dumps(records), encoding="utf-8")
        self.store.load()

    def media_record(self, id=1, contents=b"A complete video fixture", name="lesson.mp4"):
        self.store.media_dir.mkdir(parents=True, exist_ok=True)
        (self.store.media_dir / name).write_bytes(contents)
        return {"id": id, "date": "2025-01-01T12:00:00+00:00", "text": "Lesson",
                "media_file": f"telegram_data/media/{name}", "media_size": len(contents),
                "media_sha256": hashlib.sha256(contents).hexdigest()}

    def test_construction_and_empty_load_are_read_only(self):
        self.assertEqual(self.store.records, {})
        self.assertEqual(self.store.load(), {})
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIsNone(self.store.snapshot())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_empty_message_file_does_not_create_empty_backup(self):
        self.write_records([])
        self.assertIsNone(self.store.snapshot())
        self.assertFalse(self.store.archives_dir.exists())

    def test_corrupt_and_duplicate_records_are_not_overwritten(self):
        self.store.data_dir.mkdir()
        invalid = [b"{broken", b"{}", b'[{"id":1},{"id":1}]', b'[{"id":true}]',
                   b'[{"id":0}]', b'[{"id":1,"id":2}]', b'[{"id":1,"x":NaN}]',
                   b'[{"id":1,"revisions":{}}]']
        for payload in invalid:
            with self.subTest(payload=payload):
                self.store.messages_file.write_bytes(payload)
                with self.assertRaises(StoreError):
                    self.store.load()
                self.assertEqual(self.store.messages_file.read_bytes(), payload)
                self.assertFalse(self.store.verify()["ok"])

    def test_missing_message_file_with_existing_media_refuses_new_library(self):
        self.media_record()
        with self.assertRaisesRegex(StoreError, "messages_all.json is missing"):
            self.store.load()
        self.store.upsert({"id": 2})
        with self.assertRaises(StoreError):
            self.store.save()
        self.assertFalse(self.store.messages_file.exists())

    def test_save_preserves_unknown_fields_and_stable_numbering(self):
        self.write_records([{"id": 7, "entry": 91, "custom": {"source": "original"}},
                            {"id": 2, "entry": 90, "text": "早安"}])
        self.store.upsert({"id": 4, "text": "new"})
        self.store.save()
        first = self.store.messages_file.read_bytes()
        values = json.loads(first)
        self.assertEqual([r["id"] for r in values], [2, 4, 7])
        self.assertEqual([r["entry"] for r in values], [1, 2, 3])
        self.assertEqual(values[-1]["custom"], {"source": "original"})
        self.store.save()
        self.assertEqual(first, self.store.messages_file.read_bytes())

    def test_save_requires_loading_existing_data(self):
        self.write_records([{"id": 1, "text": "keep"}])
        other = Store(self.root)
        other.upsert({"id": 2})
        with self.assertRaisesRegex(StoreError, "Load the existing library"):
            other.save()
        self.assertEqual(json.loads(self.store.messages_file.read_text())[0]["id"], 1)

    def test_atomic_failure_keeps_original_and_removes_partial(self):
        self.write_records([{"id": 1, "text": "original"}])
        original = self.store.messages_file.read_bytes()
        self.store.upsert({"id": 1, "text": "edited"})
        with patch("telegram_scraper.storage.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(StoreError):
                self.store.save()
        self.assertEqual(original, self.store.messages_file.read_bytes())
        self.assertEqual(list(self.store.data_dir.glob("*.partial")), [])

    def test_non_json_and_nonfinite_data_does_not_clobber_library(self):
        self.write_records([{"id": 1}])
        original = self.store.messages_file.read_bytes()
        for bad in (object(), float("inf")):
            self.store.records[1]["unsupported"] = bad
            with self.assertRaises(StoreError):
                self.store.save()
            self.assertEqual(self.store.messages_file.read_bytes(), original)

    def test_external_edit_detected_before_save(self):
        self.write_records([{"id": 1}])
        self.store.upsert({"id": 2})
        external = b'[{"id":1,"note":"another writer"}]'
        self.store.messages_file.write_bytes(external)
        with self.assertRaisesRegex(StoreError, "changed in another program"):
            self.store.save()
        self.assertEqual(self.store.messages_file.read_bytes(), external)

    def test_upsert_preserves_edits_and_custom_fields_without_volatile_revisions(self):
        initial = {"id": 1, "text": "original", "date": "2025-01-01", "custom": "keep",
                   "raw": {"message": "original", "views": 10, "media": {"file_reference": "old"}}}
        self.assertEqual(self.store.upsert(initial), "added")
        self.assertEqual(self.store.upsert(initial), "unchanged")
        self.assertEqual(self.store.upsert({"id": 1, "media_status": "ready",
                                           "raw": {"message": "original", "views": 20, "media": {"file_reference": "new"}}}), "updated")
        self.assertNotIn("revisions", self.store.records[1])
        self.assertEqual(self.store.upsert({"id": 1, "text": "corrected", "date": "2025-01-02",
                                           "raw": {"message": "corrected"}}), "updated")
        record = self.store.records[1]
        self.assertEqual(record["custom"], "keep")
        self.assertEqual(len(record["revisions"]), 1)
        prior = record["revisions"][0]
        self.assertEqual(prior["text"], "original")
        self.assertEqual(prior["date"], "2025-01-01")
        self.assertEqual(prior["raw"]["views"], 20)
        self.assertIn("archived_at", prior)
        self.store.upsert({"id": 1, "text": "third", "revisions": []})
        self.assertEqual(len(self.store.records[1]["revisions"]), 2)

    def test_legacy_raw_enrichment_and_media_bookkeeping_are_not_edits(self):
        self.store.upsert({"id": 1, "text": "unchanged"})
        self.store.upsert({"id": 1, "raw": {"message": "unchanged"}, "media_file": "media/test.mp4",
                           "media_status": "ready", "media_size": 12, "media_error": None})
        self.assertNotIn("revisions", self.store.records[1])

    def test_media_paths_remain_inside_media_directory(self):
        record = self.media_record()
        expected = self.store.media_dir / "lesson.mp4"
        for value in (record["media_file"], "media/lesson.mp4", "lesson.mp4", str(expected),
                      "telegram_data\\media\\lesson.mp4"):
            self.assertEqual(self.store.media_path({"media_file": value}), expected)
        outside = self.root / "private.txt"
        outside.write_text("private")
        (self.store.media_dir / "link.mp4").symlink_to(outside)
        for value in ("../private.txt", "media/../private.txt", str(outside), "media/link.mp4",
                      "telegram_data/messages_all.json", "media", "", "\x00", "C:\\private.mp4"):
            with self.subTest(value=value):
                self.assertIsNone(self.store.media_path({"media_file": value}))

    def test_symbolic_message_file_refuses_load_and_save(self):
        other = self.root / "outside.json"
        other.write_text('[{"id":1}]')
        self.store.data_dir.mkdir()
        self.store.messages_file.symlink_to(other)
        with self.assertRaises(StoreError):
            self.store.load()
        with self.assertRaises(StoreError):
            self.store.save()
        self.assertEqual(other.read_text(), '[{"id":1}]')

    def test_channel_binding_requires_original_channel_for_legacy_library(self):
        self.write_records([{"id": 1}])
        original = self.store.messages_file.read_bytes()
        with self.assertRaisesRegex(StoreError, "original configured channel"):
            self.store.bind_channel(99, "Wrong", "@new", "@original")
        self.assertIsNone(self.store.channel_info())
        self.store.bind_channel(99, "Mentor", "@original", "@original")
        self.assertEqual(self.store.channel_info()["id"], 99)
        with self.assertRaisesRegex(StoreError, "different Telegram channel"):
            self.store.bind_channel(100, "Other", "@original", "@original")
        self.store.bind_channel(99, "Updated title", "@changed_username")
        self.assertEqual(self.store.channel_info()["title"], "Updated title")
        self.assertEqual(self.store.messages_file.read_bytes(), original)

    def test_corrupt_channel_metadata_is_not_replaced(self):
        self.store.data_dir.mkdir()
        path = self.store.data_dir / "channel.json"
        path.write_text("not JSON")
        with self.assertRaises(StoreError):
            self.store.bind_channel(99, "Mentor", "@mentor")
        self.assertEqual(path.read_text(), "not JSON")

    def test_lock_is_reentrant_but_rejects_another_process_promptly(self):
        with self.store.lock(), self.store.lock():
            script = "from pathlib import Path; from telegram_scraper.storage import Store; import sys\nwith Store(Path(sys.argv[1])).lock(): print('acquired')"
            result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already open", result.stderr)
        with Store(self.root).lock():
            pass

    def test_snapshot_is_complete_unique_and_readable(self):
        record = self.media_record()
        self.write_records([record])
        (self.store.data_dir / "notes.txt").write_text("Also preserve unrecognized files")
        before = self.store.messages_file.read_bytes()
        first = self.store.snapshot()
        second = self.store.snapshot()
        self.assertNotEqual(first, second)
        with zipfile.ZipFile(first) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("messages_all.json"), before)
            self.assertEqual(archive.read("media/lesson.mp4"), (self.store.media_dir / "lesson.mp4").read_bytes())
            self.assertIn("notes.txt", archive.namelist())
        self.assertEqual(self.store.messages_file.read_bytes(), before)
        self.assertEqual(list(self.store.archives_dir.glob("*.partial")), [])

    def test_cancelled_snapshot_leaves_no_partial_or_published_backup(self):
        self.write_records([self.media_record(contents=b"x" * (3 * 1024 * 1024))])
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls > 5

        with self.assertRaises(OperationCancelled):
            self.store.snapshot(cancel=cancel)
        self.assertEqual(list(self.store.archives_dir.glob("*")), [])
        self.assertTrue(self.store.messages_file.is_file())

    def test_snapshot_refuses_symbolic_files(self):
        self.write_records([{"id": 1}])
        target = self.root / "secret"
        target.write_text("private")
        (self.store.data_dir / "unsafe").symlink_to(target)
        with self.assertRaisesRegex(StoreError, "symbolic links"):
            self.store.snapshot()
        self.assertFalse(self.store.archives_dir.exists())

    def test_snapshot_detects_file_added_by_another_writer(self):
        self.write_records([self.media_record()])

        def change_during_backup(message):
            (self.store.data_dir / "added-during-backup.txt").write_text("concurrent data")

        with self.assertRaisesRegex(StoreError, "files changed during backup"):
            self.store.snapshot(report=change_during_backup)
        self.assertEqual(list(self.store.archives_dir.glob("*")), [])

    def test_verification_is_read_only_and_reports_success_for_complete_backup(self):
        self.write_records([self.media_record()])
        archive = self.store.snapshot()
        paths = [self.store.messages_file, self.store.media_dir / "lesson.mp4", archive]
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths]
        result = self.store.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_messages"], 1)
        self.assertEqual(result["checked_media"], 1)
        self.assertEqual(result["checked_archives"], 1)
        self.assertEqual(before, [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths])

    def test_absent_archives_and_interrupted_backups_are_not_certified(self):
        self.write_records([{"id": 1}])
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("No complete backup" in issue for issue in result["issues"]))
        self.store.archives_dir.mkdir()
        (self.store.archives_dir / "backup.zip.partial").write_bytes(b"incomplete")
        self.assertTrue(any("Incomplete backup" in issue for issue in self.store.verify()["issues"]))

    def test_edits_are_changes_but_missing_posts_are_issues(self):
        self.write_records([{"id": 1, "text": "old"}])
        self.store.snapshot()
        self.store.upsert({"id": 1, "text": "edited"})
        self.store.upsert({"id": 2, "text": "new"})
        self.store.save()
        self.store.snapshot()
        result = self.store.verify()
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("post 1 changed" in change for change in result["changes"]))
        del self.store.records[1]
        self.store.save()
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("post 1 is missing" in issue for issue in result["issues"]))

    def test_missing_and_changed_local_media_fail_verification(self):
        self.write_records([self.media_record()])
        self.store.snapshot()
        path = self.store.media_dir / "lesson.mp4"
        path.write_bytes(b"replaced")
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("checksum" in issue for issue in result["issues"]))
        self.assertTrue(any("media bytes changed" in issue for issue in result["issues"]))
        path.unlink()
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("saved media is missing" in issue for issue in result["issues"]))

    def test_current_unsafe_symlink_does_not_mislabel_valid_archived_path(self):
        self.write_records([self.media_record()])
        self.store.snapshot()
        path = self.store.media_dir / "lesson.mp4"
        path.unlink()
        outside = self.root / "private.mp4"
        outside.write_bytes(b"private")
        path.symlink_to(outside)
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["checked_archives"], 1)
        self.assertFalse(any("has an unsafe media path" in issue for issue in result["issues"]))

    def test_crc_damage_is_reported_and_archive_is_not_counted_as_verified(self):
        self.write_records([self.media_record()])
        archive = self.store.snapshot()
        with zipfile.ZipFile(archive) as z:
            info = z.getinfo("media/lesson.mp4")
            offset = info.header_offset
        damaged = bytearray(archive.read_bytes())
        name_size, extra_size = struct.unpack_from("<HH", damaged, offset + 26)
        damaged[offset + 30 + name_size + extra_size] ^= 1
        archive.write_bytes(damaged)
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["checked_archives"], 0)
        self.assertTrue(any("CRC" in issue for issue in result["issues"]))

    def test_archive_missing_messages_or_media_is_not_certified(self):
        self.write_records([self.media_record()])
        self.store.archives_dir.mkdir()
        with zipfile.ZipFile(self.store.archives_dir / "01.zip", "w") as archive:
            archive.writestr("notes.txt", "no messages")
        with zipfile.ZipFile(self.store.archives_dir / "02.zip", "w") as archive:
            archive.writestr("messages_all.json", self.store.messages_file.read_bytes())
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("messages_all.json is missing" in issue for issue in result["issues"]))
        self.assertTrue(any("is missing its media" in issue for issue in result["issues"]))

    def test_legacy_top_level_zip_folder_is_supported(self):
        self.write_records([self.media_record()])
        self.store.archives_dir.mkdir()
        with zipfile.ZipFile(self.store.archives_dir / "legacy.zip", "w") as archive:
            archive.writestr("telegram_data/messages_all.json", self.store.messages_file.read_bytes())
            archive.writestr("telegram_data/media/lesson.mp4", (self.store.media_dir / "lesson.mp4").read_bytes())
        self.assertTrue(self.store.verify()["ok"])

    def test_cancelled_verification_does_not_claim_success(self):
        self.write_records([self.media_record()])
        self.store.snapshot()
        result = self.store.verify(cancel=lambda: True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["checked_archives"], 0)

    def test_verification_detects_channel_identity_mismatch(self):
        self.write_records([{"id": 1}])
        self.store.bind_channel(99, "Original", "@original", "@original")
        self.store.snapshot()
        channel = self.store.data_dir / "channel.json"
        channel.write_text('{"id":100,"title":"Another channel"}')
        self.store.snapshot()
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("channel identity differs" in issue for issue in result["issues"]))
        channel.write_text("{broken")
        result = self.store.verify()
        self.assertFalse(result["ok"])
        self.assertTrue(any("channel.json is not valid JSON" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
