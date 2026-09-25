import asyncio
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from telegram_scraper.evidence import EvidenceStore
from test_engine import Message, ProtocolClient, DAY
from test_repairs_identity import runtime_with_client, wait_job
from telegram_scraper.storage import Store


def test_legacy_repair_duplicate_open_then_separate_capture(tmp_path):
    runtime = runtime_with_client(tmp_path, legacy=True, configured=False)
    original = runtime.store.messages_file.read_bytes()
    try:
        resolved = runtime.call(runtime.resolve_identity({"channel": "@renamed"}))
        runtime.call(runtime.confirm_identity({"token": resolved["token"], "original_channel": True}))
        assert runtime.store.messages_file.read_bytes() == original
        runtime.connection["authorized"] = False
        candidate = runtime.call(runtime.add_channel({"channel": "https://t.me/c/123/456"}))["active_channel"]
        runtime.connection["authorized"] = True
        duplicate = runtime.call(runtime.start_job({"mode": "sync"}))
        assert duplicate["capture_started"] is False and runtime.channels.active == candidate
        assert not runtime.evidence.path.exists() and not runtime.store.messages_file.exists()
        runtime.call(runtime.select_channel({"id": duplicate["existing_archive_id"]}))
        assert runtime.task is None
        assert runtime.call(runtime.start_job({"mode": "sync"}))["capture_started"]
        runtime.call(wait_job(runtime))
        assert runtime.job["status"] == "completed"
        assert len(runtime.evidence.runs()) == 1
    finally:
        runtime.close()


def test_watch_media_checkpoint_browser_refresh_then_shutdown(tmp_path):
    downloaded, release, watching = threading.Event(), threading.Event(), threading.Event()
    class HeldDownload(ProtocolClient):
        async def download_media(self, message, file, progress_callback):
            file.write(message.payload)
            downloaded.set()
            await asyncio.to_thread(release.wait)
            await progress_callback(len(message.payload), len(message.payload))
    client = HeldDownload([Message(1, text="original")])
    runtime = runtime_with_client(tmp_path, client=client)
    service = runtime.get_service()
    report = service.report
    def updates(value):
        report(value)
        if value.get("phase") == "watching": watching.set()
    service.report = updates
    try:
        runtime.call(runtime.start_job({"mode": "watch"}))
        assert watching.wait(5)
        watching.clear()
        runtime.call(client.handlers[0][0](SimpleNamespace(message=Message(1, text="edited", media=True, edit_date=DAY))))
        assert downloaded.wait(5)
        pending = {"state": runtime.call(runtime.state()), "posts": runtime.call(runtime.posts({}))}
        assert pending["posts"]["posts"][0]["media_missing"]
        release.set()
        assert watching.wait(5)
        complete = {"state": runtime.call(runtime.state()), "posts": runtime.call(runtime.posts({}))}
        assert not complete["posts"]["posts"][0]["media_missing"]
        assert pending["state"]["library"]["revision"] != complete["state"]["library"]["revision"]
        script = "const fs=require('fs');const {harness}=require('./tests/app_harness.cjs');const snapshots=JSON.parse(fs.readFileSync(0,'utf8'));let i=0,count=0;const h=harness(url=>url==='/api/state'?snapshots[i].state:(count++,snapshots[i].posts));(async()=>{await h.app.loadState();const first=h.app.signature();i=1;await h.app.loadState();if(count!==2||first===h.app.signature())process.exitCode=1;})();"
        subprocess.run(["node", "-e", script], input=json.dumps([pending, complete]), text=True, check=True, timeout=5)
        runtime.close()
        receipt = EvidenceStore(tmp_path).runs()[0]
        assert receipt["status"] == "cancelled"
        assert {r["payload"]["message"] for r in EvidenceStore(tmp_path).message_observations(123, 1)} == {"original", "edited"}
        runtime.store.snapshot()
        assert runtime.store.verify()["ok"]
    finally:
        release.set()
        runtime.close()


def test_library_revision_tracks_replacements_and_media_without_persisting(tmp_path):
    store = Store(tmp_path)
    initial = store.revision
    store.load()
    assert store.revision != initial
    store.upsert({"id": 1, "text": "unchanged", "media_status": "pending"})
    pending = store.revision
    store.upsert({"id": 1, "text": "unchanged", "media_status": "pending"})
    assert store.revision == pending
    store.upsert({"id": 1, "text": "unchanged", "media_status": "downloaded"})
    assert store.revision != pending
    store.save()
    assert "revision_epoch" not in store.messages_file.read_text()
    assert Store(tmp_path).revision != store.revision
