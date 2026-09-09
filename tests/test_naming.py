"""Renaming must preserve existing libraries and saved account selection."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_scraper import config
from telegram_scraper.engine import TelegramService


def installed_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_SCRAPER_HOME", raising=False)
    monkeypatch.delenv("TELEGRAM_ARCHIVE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "__file__", str(tmp_path / "installed/telegram_scraper/config.py"))


def test_installed_upgrade_reuses_existing_library_without_moving_it(monkeypatch, tmp_path):
    installed_home(monkeypatch, tmp_path)
    current = tmp_path / ".local/share/telegram-scraper"
    previous = current.with_name("channel-archive")
    assert config.default_root() == current
    assert not current.exists()
    previous.mkdir(parents=True)
    (previous / "saved-data").write_bytes(b"preserved")
    assert config.default_root() == previous
    assert not current.exists()
    assert (previous / "saved-data").read_bytes() == b"preserved"


def test_new_storage_override_takes_precedence_over_legacy_override(monkeypatch, tmp_path):
    installed_home(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_ARCHIVE_HOME", str(tmp_path / "old"))
    assert config.default_root() == tmp_path / "old"
    monkeypatch.setenv("TELEGRAM_SCRAPER_HOME", str(tmp_path / "chosen"))
    assert config.default_root() == tmp_path / "chosen"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["telegram-scraper.session", "telegram_scraper.session"])
def test_connection_reuses_the_saved_session_filename(monkeypatch, tmp_path, name):
    import telethon
    saved = tmp_path / name
    saved.write_bytes(b"existing login remains untouched")
    calls = []
    client = SimpleNamespace(is_connected=lambda: True)

    def factory(path, *args, **kwargs):
        calls.append(path)
        return client

    monkeypatch.setattr(telethon, "TelegramClient", factory)
    service = TelegramService(tmp_path, {"api_id": 123, "api_hash": "a" * 32}, None, lambda _: None)
    assert asyncio.run(service._connected_client()) is client
    assert calls == [str(saved)]
    assert saved.read_bytes() == b"existing login remains untouched"
    assert list(tmp_path.iterdir()) == [saved]


def test_ambiguous_sessions_do_not_silently_pick_an_account(tmp_path):
    for name in ("telegram-scraper.session", "telegram_scraper.session"):
        (tmp_path / name).write_bytes(name.encode())
    with pytest.raises(config.ConfigError, match="Two Telegram login files"):
        config.session_path(tmp_path)
    for path in tmp_path.iterdir():
        assert path.read_bytes() == path.name.encode()


def test_session_symlink_does_not_open_an_external_login(tmp_path):
    saved = tmp_path / "external.session"
    saved.write_bytes(b"untouched")
    (tmp_path / "telegram-scraper.session").symlink_to(saved)
    with pytest.raises(config.ConfigError, match="symbolic link"):
        config.session_path(tmp_path)
    assert saved.read_bytes() == b"untouched"
