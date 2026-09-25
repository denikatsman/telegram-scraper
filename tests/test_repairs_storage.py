"""Recovery regressions for TS-001 through TS-005 and TS-007. Synthetic data only."""
import hashlib
import json
import os
import struct
import zipfile
from pathlib import Path

import pytest

from telegram_scraper.storage import Store, StoreError
from telegram_scraper.evidence import EvidenceStore, EvidenceError


def library(root):
    store = Store(root)
    store.load()
    store.upsert({"id": 1, "text": "original"})
    store.save()
    return store


@pytest.mark.parametrize("failed_walk", [1, 2])
def test_snapshot_enumeration_failure_never_publishes(tmp_path, monkeypatch, failed_walk):
    store = library(tmp_path)
    source = store.messages_file.read_bytes()
    real_walk, calls = os.walk, 0

    def broken_walk(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failed_walk:
            error = PermissionError("synthetic unreadable subtree")
            if kwargs.get("onerror"):
                kwargs["onerror"](error)
        yield from real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", broken_walk)
    with pytest.raises((StoreError, PermissionError)):
        store.snapshot()
    assert store.messages_file.read_bytes() == source
    assert not list(store.archives_dir.glob("*"))


def test_invalid_deflate_does_not_abort_later_backups(tmp_path):
    store = library(tmp_path)
    store.archives_dir.mkdir()
    bad = store.archives_dir / "a-bad.zip"
    with zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("messages_all.json", store.messages_file.read_bytes())
    data = bytearray(bad.read_bytes())
    offset = 30 + struct.unpack_from("<H", data, 26)[0] + struct.unpack_from("<H", data, 28)[0]
    data[offset] = 7  # final block, reserved DEFLATE type
    bad.write_bytes(data)
    store.snapshot()
    result = store.verify()
    assert not result["ok"]
    assert result["checked_archives"] == 1
    assert any("a-bad.zip" in issue for issue in result["issues"])


def modern(root, *, revision=False):
    store = library(root)
    evidence = EvidenceStore(root)
    run = evidence.begin_run({"mode": "sync", "channel_id": 123})
    raw = {"_": "Message", "id": 1, "message": "source"}
    ref = evidence.append_observation(run, "message", 1, raw)
    record = {"id": 1, "raw": raw, "source_channel_id": 123, "source_observation_id": ref}
    store.records = {1: {"id": 1, "revisions": [record]} if revision else record}
    store.save()
    evidence.finish_run(run)
    return store, evidence


@pytest.mark.parametrize("revision", [False, True])
def test_missing_modern_source_blocks_verify_and_recreation(tmp_path, revision):
    store, evidence = modern(tmp_path, revision=revision)
    evidence.path.unlink()
    with pytest.raises(EvidenceError, match="required source evidence database is missing"):
        evidence.begin_run({"mode": "sync"})
    assert not evidence.path.exists()
    assert any("source evidence database is missing" in s for s in store.verify()["issues"])


@pytest.mark.parametrize("change", ["subject", "channel", "kind", "raw"])
def test_replacement_database_same_row_number_is_not_source_proof(tmp_path, change):
    store, evidence = modern(tmp_path)
    original = store.messages_file.read_bytes()
    evidence.path.unlink()
    other = EvidenceStore(tmp_path / "other")
    run = other.begin_run({"mode": "sync", "channel_id": 999 if change == "channel" else 123})
    other.append_observation(run, "entity" if change == "kind" else "message", 2 if change == "subject" else 1,
                             {"_": "Message", "id": 1, "message": "different" if change == "raw" else "source"})
    evidence.path.write_bytes(other.path.read_bytes())
    with pytest.raises(EvidenceError, match="does not match"):
        evidence.prepare()
    assert store.messages_file.read_bytes() == original


@pytest.mark.parametrize("field", ["revisions", "previous_media"])
def test_historical_media_before_first_backup_and_conflicting_receipts(tmp_path, field):
    store = library(tmp_path)
    store.media_dir.mkdir()
    path = store.media_dir / "old.bin"
    path.write_bytes(b"old")
    receipt = {"id": 1, "media_file": "media/old.bin", "media_size": 3, "media_sha256": hashlib.sha256(b"old").hexdigest()}
    store.records[1][field] = [receipt, dict(receipt), {**receipt, "media_sha256": "f" * 64}]
    store.save()
    result = store.verify()
    assert result["checked_media"] == 1
    assert any("checksum" in s for s in result["issues"])
    path.unlink()
    assert any("saved media is missing" in s for s in store.verify()["issues"])
    store.records[1][field][0]["media_file"] = "../../outside"
    store.save()
    assert any("unsafe" in s for s in store.verify()["issues"])


@pytest.mark.parametrize("layout", ["invalid", "missing", "source_only_invalid", "valid_source_only", "legacy", "modern"])
def test_each_backup_validates_its_own_source_database(tmp_path, layout):
    store, evidence = modern(tmp_path)
    store.archives_dir.mkdir()
    before = {p: p.read_bytes() for p in store.data_dir.rglob("*") if p.is_file()}
    with zipfile.ZipFile(store.archives_dir / "fixture.zip", "w") as archive:
        if layout not in {"source_only_invalid", "valid_source_only"}:
            archive.writestr("messages_all.json", b'[{"id":1}]' if layout == "legacy" else store.messages_file.read_bytes())
        if layout not in {"missing", "legacy"}:
            archive.writestr("evidence.sqlite3", b"not sqlite" if "invalid" in layout else evidence.path.read_bytes())
    result = store.verify()
    assert result["ok"] == (layout in {"valid_source_only", "legacy", "modern"}), result
    assert result["checked_archives"] == int(result["ok"])
    assert all(p.read_bytes() == data for p, data in before.items())


def test_real_unreadable_directory_aborts_snapshot(tmp_path):
    store = library(tmp_path)
    blocked = store.data_dir / "blocked"
    blocked.mkdir()
    (blocked / "old.bin").write_bytes(b"preserve")
    blocked.chmod(0)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("Current account bypasses directory permissions")
        with pytest.raises(StoreError, match="enumerate"):
            store.snapshot()
        assert not list(store.archives_dir.glob("*"))
    finally:
        blocked.chmod(0o700)
    assert (blocked / "old.bin").read_bytes() == b"preserve"
