"""Source and alternate-media verification never touches personal archives."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from telegram_scraper.evidence import EvidenceStore
from telegram_scraper.integrity import verify_extended


class ExtendedIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "telegram_data"
        self.variants = self.data / "media" / "variants"
        self.index = self.variants / "index"

    def variant(self, contents=b"complete thumbnail", identity="Photo:42:PhotoSize:x", recovered=False):
        self.index.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(contents).hexdigest()
        key = hashlib.sha256(identity.encode()).hexdigest()
        suffix = "-recovered-" + "a" * 32 if recovered else ""
        path = self.variants / f"{digest}{suffix}.bin"
        path.write_bytes(contents)
        receipt = {"identity": identity, "path": path.relative_to(self.root).as_posix(), "sha256": digest, "size": len(contents)}
        if recovered:
            receipt["recovered_from"] = (self.variants / f"{digest}.bin").relative_to(self.root).as_posix()
        manifest = self.index / f"{key}-{digest}{'-' + 'b' * 32 if recovered else ''}.json"
        manifest.write_text(json.dumps(receipt))
        return path, manifest, receipt

    def messages(self, records):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "messages_all.json").write_text(json.dumps(records))

    def test_legacy_library_without_new_evidence_or_variants_is_valid_and_unchanged(self):
        self.messages([{"id": 1, "text": "legacy"}])
        before = (self.data / "messages_all.json").read_bytes()
        result = verify_extended(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_evidence_observations"], 0)
        self.assertEqual(result["checked_variant_files"], 0)
        self.assertEqual(list(self.data.iterdir()), [self.data / "messages_all.json"])
        self.assertEqual((self.data / "messages_all.json").read_bytes(), before)

    def test_empty_library_verification_is_read_only(self):
        self.assertTrue(verify_extended(self.root)["ok"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_valid_evidence_and_variant_counted_once_across_all_receipts(self):
        path, manifest, receipt = self.variant()
        self.messages([{"id": 1, "context_capture": {"media_variants": [{**receipt, "state": "downloaded", "expected_bytes": path.stat().st_size}]}}])
        evidence = EvidenceStore(self.root)
        run = evidence.begin_run({"mode": "sync", "channel_id": 99})
        evidence.append_observation(run, "media_variant_file", "99:1", receipt)
        evidence.finish_run(run, coverage={"capture_complete": True, "history_scan_complete": True})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (path, manifest, evidence.path)}
        result = verify_extended(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_variant_files"], 1)
        self.assertEqual(result["checked_variant_manifests"], 1)
        self.assertEqual(result["checked_evidence_observations"], 1)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_corrupt_evidence_does_not_stop_independent_variant_check(self):
        self.variant()
        evidence = EvidenceStore(self.root)
        evidence.path.write_bytes(b"damaged database")
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checked_variant_files"], 1)
        self.assertTrue(any("database is damaged" in issue for issue in result["issues"]))
        self.assertEqual(evidence.path.read_bytes(), b"damaged database")

    def test_future_evidence_schema_is_not_migrated(self):
        evidence = EvidenceStore(self.root)
        evidence.begin_run({"mode": "sync", "channel_id": 99})
        with closing(sqlite3.connect(evidence.path)) as connection, connection:
            connection.execute("PRAGMA user_version=2")
        before = evidence.path.read_bytes()
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("unsupported format" in issue for issue in result["issues"]))
        self.assertEqual(evidence.path.read_bytes(), before)

    def test_changed_or_missing_variant_is_an_integrity_issue(self):
        path, _, _ = self.variant()
        path.write_bytes(b"changed")
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bytes do not match" in issue for issue in result["issues"]))
        path.unlink()
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing" in issue for issue in result["issues"]))

    def test_invalid_manifest_and_missing_manifest_are_reported(self):
        _, manifest, _ = self.variant()
        manifest.write_text('{"duplicate":1,"duplicate":2}')
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("invalid JSON" in issue for issue in result["issues"]))
        self.assertTrue(any("no immutable manifest" in issue for issue in result["issues"]))

    def test_manifest_filename_must_match_identity_and_checksum(self):
        _, manifest, receipt = self.variant()
        receipt["identity"] = "Photo:43:PhotoSize:x"
        manifest.write_text(json.dumps(receipt))
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("manifest filename" in issue for issue in result["issues"]))

    def test_unsafe_paths_and_symlinks_are_never_followed(self):
        path, manifest, receipt = self.variant()
        outside = self.root / "private"
        outside.write_bytes(b"do not read as a variant")
        receipt["path"] = "../../private"
        manifest.write_text(json.dumps(receipt))
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("path is unsafe" in issue for issue in result["issues"]))
        path.unlink()
        path.symlink_to(outside)
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checked_variant_files"], 0)
        self.assertEqual(outside.read_bytes(), b"do not read as a variant")

    def test_parent_directory_symlink_is_rejected(self):
        self.data.mkdir()
        outside = self.root / "alternate"
        outside.mkdir()
        (self.data / "media").symlink_to(outside)
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("symbolic-link folder" in issue for issue in result["issues"]))

    def test_immutable_recovery_names_are_supported_and_original_damage_remains_visible(self):
        path, _, receipt = self.variant(recovered=True)
        result = verify_extended(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_variant_files"], 1)
        self.assertTrue(any("recovered media variant" in item for item in result["limitations"]))
        original = self.root / receipt["recovered_from"]
        original.write_bytes(b"preserved damaged original")
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("bytes do not match" in issue for issue in result["issues"]))
        self.assertEqual(path.read_bytes(), b"complete thumbnail")

    def test_disabled_metadata_only_and_failed_downloads_are_not_claimed_as_saved_files(self):
        self.messages([{"id": 1, "context_capture": {"media_variants": [
            {"identity": "a", "state": "disabled"}, {"identity": "b", "state": "metadata_only"},
            {"identity": "c", "state": "failed"}, {"identity": "d", "state": "primary_archive"}]}}])
        result = verify_extended(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked_variant_files"], 0)
        self.assertEqual(len(result["limitations"]), 3)
        self.assertTrue(all("not claimed as archived" in item for item in result["limitations"]))

    def test_advertised_size_conflict_is_detected(self):
        _, _, receipt = self.variant()
        self.messages([{"id": 1, "context_capture": {"media_variants": [{**receipt, "state": "downloaded", "expected_bytes": 900}]}}])
        result = verify_extended(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("Telegram advertised" in issue for issue in result["issues"]))

    def test_latest_capture_limitations_remain_separate_from_file_integrity(self):
        evidence = EvidenceStore(self.root)
        run = evidence.begin_run({"mode": "sync", "channel_id": 99})
        evidence.append_observation(run, "context_limitation", "99:1", {"code": "anonymous_poll", "message": "Telegram does not expose anonymous voter identities."})
        evidence.finish_run(run, "warning", coverage={"capture_complete": False, "history_scan_complete": True, "limitations": ["Only accessible channel history can be captured."]})
        result = verify_extended(self.root)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("anonymous voter identities" in item for item in result["limitations"]))
        self.assertTrue(any("does not claim complete" in item for item in result["limitations"]))

    def test_incomplete_download_and_cancellation_cannot_pass(self):
        self.index.mkdir(parents=True)
        (self.variants / ".variant-fixture.partial").write_bytes(b"unfinished")
        self.assertFalse(verify_extended(self.root)["ok"])
        result = verify_extended(self.root, cancel=lambda: True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])


if __name__ == "__main__":
    unittest.main()
