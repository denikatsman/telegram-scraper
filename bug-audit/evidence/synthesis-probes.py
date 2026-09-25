#!/usr/bin/env python3
"""Bounded reproduction of reported defects; copied code and synthetic data only.

Run with Python 3.14 and -B. This does not start a real Telegram client, install
an app, or read a real archive. JSON on stdout is the complete probe evidence.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import struct
import sys
import tempfile
import zipfile
import zlib

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "bug-audit/launch-20260919T063437Z/source-baseline.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_sources(baseline):
    for entry in baseline["files"]:
        path = ROOT / entry["path"]
        assert digest(path) == entry["sha256"], entry["path"]
        assert stat.S_IMODE(path.stat().st_mode) == entry["mode"], entry["path"]


def main():
    baseline = json.loads(BASELINE.read_text())
    check_sources(baseline)
    reports = sorted((ROOT / "bug-audit").glob("[0-9][0-9]-*.md"))
    assert len(reports) == 8
    report_hashes = {p.name: digest(p) for p in reports}
    results = []
    with tempfile.TemporaryDirectory(prefix="telegram-synthesis-") as temporary:
        temporary = Path(temporary).resolve()
        copied = temporary / "source"
        for entry in baseline["files"]:
            target = copied / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / entry["path"], target)
            assert digest(target) == entry["sha256"]
        sys.path.insert(0, str(copied))
        from telegram_scraper.storage import Store, StoreError
        from telegram_scraper.evidence import EvidenceStore
        from telethon.tl import types
        import telethon
        import telegram_scraper.storage as imported_storage
        assert Path(imported_storage.__file__).is_relative_to(copied)
        fixture = runpy.run_path(str(copied / "tests/test_engine.py"))
        ProtocolClient, WireObject = fixture["ProtocolClient"], fixture["WireObject"]
        make_service, day = fixture["service"], fixture["DAY"]

        async def first_photo(cancel):
            root = temporary / ("cancelled-photo" if cancel else "empty-photo")
            root.mkdir()
            size = (types.PhotoSize(type="s", w=8, h=8, size=4) if cancel else
                    types.PhotoCachedSize(type="s", w=8, h=8, bytes=b"jpeg"))
            photo = types.Photo(id=321, access_hash=654, file_reference=b"fixture",
                                date=day, sizes=[size], dc_id=2)

            class PhotoClient(ProtocolClient):
                async def __call__(self, request):
                    if type(request).__name__ == "GetFullChannelRequest":
                        return WireObject(_="messages.ChatFull", full_chat=WireObject(
                            _="ChannelFull", id=123, chat_photo=photo), users=[], chats=[])
                    return await super().__call__(request)

                async def download_file(self, location, *, file, progress_callback, **kwargs):
                    file.write(b"jpeg")
                    if cancel:
                        engine.cancel()
                    await progress_callback(4, 4)

            client = PhotoClient([])
            engine, store, updates = make_service(root, client, capture_context=True)
            with store.lock():
                outcome = await engine.run("sync")
            assert outcome["status"] == ("cancelled" if cancel else "completed")
            assert not store.messages_file.exists()
            try:
                Store(root).load()
            except StoreError as exc:
                error = str(exc)
            else:
                raise AssertionError("Expected the reported reopen failure")
            assert "messages_all.json is missing" in error
            results.append({"case": "first_photo_cancelled" if cancel else "first_photo_empty_sync",
                            "original_findings": ["04-01", "07-01"],
                            "status": outcome["status"], "processed": outcome["processed"],
                            "message_index_exists": store.messages_file.exists(),
                            "remaining_media": [str(p.relative_to(root)) for p in sorted(store.media_dir.rglob("*"))],
                            "reopen_error": error})
            await engine.close()

        asyncio.run(first_photo(False))
        asyncio.run(first_photo(True))

        root = temporary / "unreadable-subtree"
        root.mkdir()
        store = Store(root)
        store.load()
        store.upsert({"id": 1, "text": "fixture"})
        store.save()
        restricted = store.media_dir / "historical"
        restricted.mkdir(parents=True)
        (restricted / "old.mp4").write_bytes(b"saved historical attachment")
        restricted.chmod(0)
        try:
            try:
                list(restricted.iterdir())
            except PermissionError:
                pass
            else:
                raise AssertionError("This probe requires an actual directory PermissionError")
            with store.lock():
                backup = store.snapshot()
                verification = store.verify()
            with zipfile.ZipFile(backup) as archive:
                members = archive.namelist()
            assert members == ["messages_all.json"]
            assert verification["ok"] and not verification["issues"]
            results.append({"case": "unreadable_subtree", "original_findings": ["08-01"],
                            "uid": os.getuid(), "directory_read": "PermissionError",
                            "published_members": members, "verify_ok": verification["ok"],
                            "issues": verification["issues"]})
        finally:
            restricted.chmod(0o700)

        root = temporary / "backup-semantics"
        root.mkdir()
        store = Store(root)
        store.load()
        evidence = EvidenceStore(root)
        run = evidence.begin_run({"mode": "sync", "channel_id": 123})
        observation = evidence.append_observation(run, "message", 1,
            {"_": "Message", "id": 1, "message": "fixture"}, b"fixture-tl")
        evidence.finish_run(run)
        store.upsert({"id": 1, "text": "fixture", "source_channel_id": 123,
                      "source_observation_id": observation})
        store.save()
        store.archives_dir.mkdir()
        for number, (label, include_index, include_database) in enumerate([
            ("invalid_database", True, True),
            ("missing_database", True, False),
            ("source_only_invalid_database", False, True),
        ]):
            archive_path = store.archives_dir / (str(number) + ".zip")
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                if include_index:
                    archive.writestr("messages_all.json", store.messages_file.read_bytes())
                if include_database:
                    archive.writestr("evidence.sqlite3", b"truncated SQLite state")
            with zipfile.ZipFile(archive_path) as archive:
                assert archive.testzip() is None
            verification = store.verify()
            assert verification["ok"] and verification["checked_archives"] == 1
            results.append({"case": label, "original_findings": ["07-05"],
                            "zip_crc_ok": True, "verify_ok": verification["ok"],
                            "checked_archives": verification["checked_archives"],
                            "issues": verification["issues"]})
            archive_path.unlink()  # Synthetic ZIP in this probe's TemporaryDirectory only.

        for name in ("01-corrupt.zip", "02-good.zip"):
            with zipfile.ZipFile(store.archives_dir / name, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("messages_all.json", store.messages_file.read_bytes())
        corrupt = store.archives_dir / "01-corrupt.zip"
        content = bytearray(corrupt.read_bytes())
        filename_len, extra_len = struct.unpack_from("<HH", content, 26)
        content[30 + filename_len + extra_len] |= 6
        corrupt.write_bytes(content)
        progress = []
        try:
            store.verify(report=progress.append)
        except zlib.error as exc:
            message = str(exc)
        else:
            raise AssertionError("Expected reported zlib error")
        assert not any("02-good.zip" in p for p in progress)
        results.append({"case": "invalid_deflate", "original_findings": ["08-04"],
                        "exception": "zlib.error", "message": message,
                        "later_backup_checked": False})
        runtime_version = {"python": sys.version, "telethon": telethon.__version__}

    check_sources(baseline)
    assert report_hashes == {p.name: digest(p) for p in reports}
    print(json.dumps({"verified_at": datetime.now(timezone.utc).isoformat(),
                      "project_root": str(ROOT), "baseline": str(BASELINE),
                      "source_fingerprint_sha256": baseline["source_fingerprint_sha256"],
                      "source_files_checked": len(baseline["files"]),
                      "source_unchanged": True, "original_report_sha256": report_hashes,
                      "original_reports_unchanged": True, "environment": runtime_version,
                      "cases_passed": len(results), "results": results}, indent=2))


if __name__ == "__main__":
    main()
