import asyncio
import pytest
from telethon.tl import types
from telegram_scraper.storage import Store, StoreError
from telegram_scraper.engine import UserError
from test_engine import service, ProtocolClient, WireObject, DAY


@pytest.mark.parametrize("mode,cancel", [("sync", False), ("sync", True), ("range", False)])
def test_empty_photo_capture_can_reopen_retry_snapshot_verify(tmp_path, mode, cancel):
    async def run():
        photo = types.Photo(id=321, access_hash=654, file_reference=b"fixture", date=DAY,
                            sizes=[types.PhotoSize(type="s", w=8, h=8, size=4)], dc_id=2)
        class PhotoClient(ProtocolClient):
            async def __call__(self, request):
                if type(request).__name__ == "GetFullChannelRequest":
                    return WireObject(_="messages.ChatFull", full_chat=WireObject(_="ChannelFull", id=123, chat_photo=photo), users=[], chats=[])
                return await super().__call__(request)
            async def download_file(self, location, *, file, progress_callback, **kwargs):
                file.write(b"jpeg")
                if cancel:
                    engine.cancel()
                await progress_callback(4, 4)
        engine, store, _ = service(tmp_path, PhotoClient([]), capture_context=True)
        result = await engine.run(mode, "2025-03-11" if mode == "range" else "")
        assert result["status"] == ("cancelled" if cancel else "completed")
        assert store.messages_file.exists() and Store(tmp_path).load() == {}
        engine.settings["capture_context"] = False
        assert (await engine.run("sync"))["status"] == "completed"
        store.snapshot()
        assert store.verify()["ok"]
    asyncio.run(run())


def test_initial_index_write_failure_precedes_media(tmp_path, monkeypatch):
    async def run():
        engine, store, _ = service(tmp_path, ProtocolClient([]), capture_context=True)
        def fail():
            raise StoreError("synthetic index failure")
        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(UserError, match="index failure"):
            await engine.run("sync")
        assert not store.media_dir.exists()
    asyncio.run(run())
