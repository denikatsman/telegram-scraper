"""Validated, private configuration with working-directory independent paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


DEFAULTS = {
    "schema_version": 1,
    "api_id": "",
    "api_hash": "",
    "channel": "",
    "download_media": True,
    "archive_before_sync": True,
    "capture_context": True,
}


class ConfigError(ValueError):
    pass


def default_root() -> Path:
    """Source checkouts retain their existing data; installed apps use home storage."""
    if os.environ.get("TELEGRAM_ARCHIVE_HOME"):
        return Path(os.environ["TELEGRAM_ARCHIVE_HOME"]).expanduser().resolve()
    source = Path(__file__).resolve().parent.parent
    if (source / "pyproject.toml").is_file() and (source / "main.py").is_file():
        return source
    return Path.home() / ".local" / "share" / "channel-archive"


def normalize_channel(value: str) -> str:
    from .links import normalize_channel as parse, ChannelLinkError
    try:
        return parse(value)
    except ChannelLinkError as exc:
        raise ConfigError(str(exc)) from None


class Settings:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".telegram-scraper.json"
        self.values = dict(DEFAULTS)
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ConfigError("Settings could not be read. Restore .telegram-scraper.json from a backup before saving new settings.") from exc
            if not isinstance(data, dict) or type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
                raise ConfigError("These settings use an unsupported format. Keep the file and use the app version that created it.")
            self.values.update(data)
            self._validate(self.values)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def public(self) -> dict:
        return {key: self.values[key] for key in ("api_id", "channel", "download_media", "archive_before_sync", "capture_context")} | {
            "api_hash_set": bool(self.values.get("api_hash")),
        }

    @staticmethod
    def _validate(values):
        value = str(values.get("api_id", "")).strip()
        if value and (not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**31):
            raise ConfigError("API ID must be the positive number shown at my.telegram.org.")
        secret = values.get("api_hash", "")
        if not isinstance(secret, str) or secret and not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
            raise ConfigError("API hash must contain the 32 letters and numbers shown at my.telegram.org.")
        if not isinstance(values.get("channel"), str):
            raise ConfigError("Enter a channel username or link.")
        normalize_channel(values["channel"])
        for key in ("download_media", "archive_before_sync", "capture_context"):
            if type(values.get(key)) is not bool:
                raise ConfigError("Choose whether to enable media downloads and backups.")
        if "legacy_channel" in values and not isinstance(values["legacy_channel"], str):
            raise ConfigError("The original channel setting could not be read. Restore the settings file before syncing.")

    def update(self, incoming: dict) -> dict:
        allowed = {"api_id", "api_hash", "channel", "download_media", "archive_before_sync", "capture_context"}
        if set(incoming) - allowed:
            raise ConfigError("Some settings were not recognized. Reload the page and try again.")
        values = dict(self.values)
        if "api_id" in incoming:
            value = str(incoming["api_id"]).strip()
            if value and (not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**31):
                raise ConfigError("API ID must be the positive number shown at my.telegram.org.")
            values["api_id"] = int(value) if value else ""
        if "api_hash" in incoming:
            value = str(incoming["api_hash"]).strip()
            if value:
                if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
                    raise ConfigError("API hash must contain the 32 letters and numbers shown at my.telegram.org.")
                values["api_hash"] = value
        if "channel" in incoming:
            if not isinstance(incoming["channel"], str):
                raise ConfigError("Enter a channel username or link.")
            values["channel"] = normalize_channel(incoming["channel"])
        for key in ("download_media", "archive_before_sync", "capture_context"):
            if key in incoming:
                if type(incoming[key]) is not bool:
                    raise ConfigError("Choose whether to enable media downloads and backups.")
                values[key] = incoming[key]
        values["schema_version"] = 1
        self._validate(values)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, prefix=".settings-", delete=False) as file:
                temporary = Path(file.name)
                os.chmod(temporary, 0o600)
                json.dump(values, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ConfigError("Settings could not be saved. Check that the data folder is writable and the disk has free space.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.values = values
        return self.public()
