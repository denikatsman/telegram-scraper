import hashlib
from pathlib import Path
import pytest
from telegram_scraper.channels import Channels, CatalogueSaveError
from telegram_scraper.storage import StoreError


@pytest.mark.parametrize("boundary", ["before", "after", "uncertain"])
def test_add_reconciles_publication_before_folder_rollback(tmp_path, monkeypatch, boundary):
    channels = Channels(tmp_path)
    from telegram_scraper import channels as module
    original = module._atomic_json
    read = Path.read_bytes
    def failure(path, values):
        if boundary != "before":
            original(path, values)
        if boundary == "uncertain":
            def fail_read(p):
                if p == path:
                    raise OSError("synthetic read failure")
                return read(p)
            monkeypatch.setattr(Path, "read_bytes", fail_read)
        raise StoreError("synthetic flush failure")
    monkeypatch.setattr(module, "_atomic_json", failure)
    with pytest.raises(CatalogueSaveError) as failure_info:
        channels.add("@fixture", "@main")
    assert failure_info.value.publication == {"before": "old", "after": "published", "uncertain": "unknown"}[boundary]
    assert len(list((tmp_path / "channels").iterdir())) == (boundary != "before")
    monkeypatch.undo()
    if boundary != "before":
        restarted = Channels(tmp_path)
        key, added = restarted.add("@fixture", "@main")
        assert not added and restarted.folder(key).is_dir()
        restarted.select(key)
        assert Channels(tmp_path).active == key
        if boundary == "after":
            assert channels.values["channels"] == restarted.values["channels"]
            assert channels.digest == hashlib.sha256(read(channels.path)).hexdigest() or channels.active == "main"
