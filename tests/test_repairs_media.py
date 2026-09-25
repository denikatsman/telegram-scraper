import asyncio
import os
from pathlib import Path
import subprocess
import sys
import pytest
from telethon.tl import types
from telethon.client.downloads import DownloadMethods
from telegram_scraper.storage import Store
from telegram_scraper.evidence import EvidenceStore, EvidenceError
from telegram_scraper.engine import UserError
from test_engine import service, Client, ProtocolClient, Message, DAY


CRASH = r'''
import asyncio, os, sys
from pathlib import Path
from test_engine import service, ProtocolClient, Message
from telegram_scraper.evidence import EvidenceStore
from telegram_scraper.storage import Store
root, point = Path(sys.argv[1]), sys.argv[2]
real_link, append, save = os.link, EvidenceStore.append_observation, Store.save
def link(src, dst, *args, **kwargs):
    primary = Path(dst).name.startswith('Message-')
    if primary and point == 'before_link': os._exit(70)
    result = real_link(src, dst, *args, **kwargs)
    if primary and point == 'after_link': os._exit(70)
    return result
def receipt(self, run, kind, *args, **kwargs):
    result = append(self, run, kind, *args, **kwargs)
    if kind == 'primary_media_file' and point == 'after_receipt': os._exit(70)
    return result
def checkpoint(self):
    if point == 'before_json' and any(r.get('media_receipt') for r in self.records.values()): os._exit(70)
    return save(self)
os.link, EvidenceStore.append_observation, Store.save = link, receipt, checkpoint
async def run():
    engine, store, _ = service(root, ProtocolClient([Message(1, media=True)]))
    await engine.run('sync')
asyncio.run(run())
'''


@pytest.mark.parametrize("point", ["before_link", "after_link", "after_receipt", "before_json"])
def test_primary_crash_retry_reuses_publication_and_verifies_backup(tmp_path, point):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]))
    crashed = subprocess.run([sys.executable, "-B", "-c", CRASH, str(tmp_path), point], env=env, capture_output=True, text=True, timeout=20)
    assert crashed.returncode == 70, crashed.stderr
    async def retry():
        client = ProtocolClient([Message(1, media=True)])
        engine, store, _ = service(tmp_path, client)
        assert (await engine.run("sync"))["status"] == "completed"
        assert client.downloads == ([1] if point == "before_link" else [])
        files = list(store.media_dir.glob("Message-*"))
        assert len(files) == 1 and files[0].read_bytes() == b"video"
        # Replay the completed run without transferring another copy.
        await engine.run("sync")
        assert len(list(store.media_dir.glob("Message-*"))) == 1
        client.messages = [Message(1, text="attachment removed")]
        await engine.run("sync")
        assert store.records[1]["previous_media"]
        store.snapshot()
        assert store.verify()["ok"]
    asyncio.run(retry())


def test_primary_completion_receipt_failure_reuses_intent(tmp_path):
    async def run():
        class FailOnce(EvidenceStore):
            failed = False
            def append_observation(self, run, kind, *args, **kwargs):
                if kind == "primary_media_file" and not self.failed:
                    self.failed = True
                    raise EvidenceError("synthetic completion failure")
                return super().append_observation(run, kind, *args, **kwargs)
        client = ProtocolClient([Message(1, media=True)])
        engine, store, _ = service(tmp_path, client)
        engine.evidence = FailOnce(tmp_path)
        with pytest.raises(UserError, match="completion failure"):
            await engine.run("sync")
        await engine.run("sync")
        assert client.downloads == [1] and len(list(store.media_dir.glob("Message-*"))) == 1
        path = store.media_path(store.records[1])
        path.write_bytes(b"other")  # same-size corruption must be retained
        await engine.run("sync")
        assert path.read_bytes() == b"other"
        assert len(list(store.media_dir.glob("Message-*"))) == 2
        store.snapshot()
        assert any("checksum" in issue for issue in store.verify()["issues"])
    asyncio.run(run())


def test_prefixed_legacy_file_exact_match_reused_and_symlink_preserved(tmp_path):
    async def run():
        engine, store, _ = service(tmp_path, ProtocolClient([Message(1, media=True)]))
        store.save()
        store.media_dir.mkdir()
        path = store.media_dir / "Message-1 (2025-03-11T00-00-00).mp4"
        path.write_bytes(b"video")
        await engine.run("sync")
        assert store.media_path(store.records[1]) == path
        assert len(list(store.media_dir.glob("Message-*"))) == 1
        # Restore a pending index to model a pre-fix file without provenance.
        second = tmp_path / "second"
        other, saved, _ = service(second, ProtocolClient([Message(1, media=True)]))
        saved.save()
        saved.media_dir.mkdir()
        external = second / "keep.bin"
        external.write_bytes(b"video")
        link = saved.media_dir / path.name
        link.symlink_to(external)
        await other.run("sync")
        assert link.is_symlink() and external.read_bytes() == b"video"
        assert saved.media_path(saved.records[1]) != link.resolve()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["document", "preview_document", "preview_both", "progressive", "video", "stripped"])
def test_primary_variant_exact_representation_transferred_once(tmp_path, kind):
    async def run():
        document = types.Document(id=100, access_hash=123, file_reference=b"d", date=DAY,
                                  mime_type="video/mp4", size=4, dc_id=2, attributes=[])
        small = types.PhotoSize("s", 5, 5, 2)
        large = types.PhotoSizeProgressive("x", 10, 10, [2, 4])
        if kind == "stripped":
            large = types.PhotoStrippedSize("i", b"\x01\x01\x01x")
        photo = types.Photo(id=200, access_hash=456, file_reference=b"p", date=DAY,
                            sizes=[large] if kind == "stripped" else [small, large], dc_id=2,
                            video_sizes=[types.VideoSize("v", 10, 10, 6)] if kind == "video" else None)
        if kind.startswith("preview"):
            webpage = types.WebPage(id=300, url="https://example.test", display_url="example.test", hash=0,
                                     document=document, photo=photo if kind == "preview_both" else None)
            media = types.MessageMediaWebPage(webpage=webpage)
        else:
            media = types.MessageMediaDocument(document=document) if kind == "document" else types.MessageMediaPhoto(photo=photo)
        message = types.Message(id=1, peer_id=types.PeerChannel(123), date=DAY, message="fixture", media=media)
        class Transfer(ProtocolClient):
            async def get_entity(self, target):
                return types.Channel(id=123, title="fixture", photo=types.ChatPhotoEmpty(), date=DAY, access_hash=123)
            async def get_input_entity(self, target):
                return types.InputPeerChannel(123, 123)
            download_media = DownloadMethods.download_media
            _download_photo = DownloadMethods._download_photo
            _get_thumb = staticmethod(DownloadMethods._get_thumb)
            _get_proper_filename = staticmethod(DownloadMethods._get_proper_filename)
            _download_cached_photo_size = DownloadMethods._download_cached_photo_size
            async def _download_document(self, media, file, date, thumb, progress_callback, msg_data):
                doc = media.document if hasattr(media, "document") else media
                return await self.download_file(types.InputDocumentFileLocation(doc.id, doc.access_hash, doc.file_reference, ""), file, file_size=doc.size, progress_callback=progress_callback)
            async def download_file(self, location, file=None, *, file_size=None, progress_callback=None, **kwargs):
                self.downloads.append((location.id, location.thumb_size))
                file.write((location.thumb_size or "d").encode() * file_size)
                if progress_callback:
                    await progress_callback(file_size, file_size)
                return file
        client = Transfer([message])
        engine, store, _ = service(tmp_path, client, capture_context=True)
        result = await engine.run("sync")
        assert result["status"] == "completed", engine._issues
        assert len(client.downloads) == len(set(client.downloads))
        summary = store.records[1]["context_capture"]
        reused = [r for r in summary["media_variants"] if r["state"] == "primary_archive"]
        assert len(reused) == (0 if kind == "stripped" else 1)
        if kind == "stripped":
            assert summary["media_variants"][0]["descriptor"]["representation"] == "embedded"
            assert store.records[1]["media_receipt"]["descriptor"]["representation"] == "expanded-jpeg"
        if kind in {"preview_both", "video", "progressive"}:
            assert summary["counts"]["variants_downloaded"] >= 1
        store.snapshot()
        assert store.verify()["ok"], store.verify()["issues"]
    asyncio.run(run())
