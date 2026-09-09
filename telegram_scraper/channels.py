"""Private channel catalogue; every library remains inside the workspace."""

import copy
import hashlib
from pathlib import Path
import re
import uuid

from .config import normalize_channel
from .storage import StoreError, _atomic_json, _decode


class Channels:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / ".telegram-scraper-channels.json"
        self.values = {"version": 1, "active": "main", "channels": {}}
        self.digest = None
        if self.path.is_symlink():
            raise StoreError("The channel list is a symbolic link. Restore the original local file.")
        if self.path.exists():
            try:
                data = self.path.read_bytes()
                self.values = _decode(data, "The saved channel list")
                self.digest = hashlib.sha256(data).hexdigest()
                self._validate(self.values)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise StoreError("The saved channel list could not be read. Keep it and restore its backup before adding channels.") from exc

    @staticmethod
    def _validate(value):
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or not isinstance(value.get("channels"), dict):
            raise ValueError("Invalid channel catalogue")
        for key, item in value["channels"].items():
            if not re.fullmatch(r"[a-f0-9]{32}", key) or not isinstance(item, dict) or not normalize_channel(item.get("channel")):
                raise ValueError("Invalid channel entry")
        if value.get("active") != "main" and value.get("active") not in value["channels"]:
            raise ValueError("Unknown active channel")

    @property
    def active(self):
        return self.values["active"]

    def folder(self, key):
        if key == "main":
            return self.root
        if not isinstance(key, str) or key not in self.values["channels"]:
            raise StoreError("That channel is not in this project.")
        parent = self.root / "channels"
        path = parent / key
        if parent.is_symlink() or path.is_symlink() or path.resolve() != path:
            raise StoreError("Channel archives must be ordinary folders inside this project.")
        if not path.is_dir():
            raise StoreError("This channel's archive folder is missing. Restore it inside the project before opening it.")
        return path

    def channel(self, primary, key=None):
        key = key or self.active
        return primary if key == "main" else self.values["channels"][key]["channel"]

    def entries(self, primary):
        return [{"id": "main", "channel": primary, "name": primary or "Your first channel"}] + [
            {"id": key, "channel": item["channel"], "name": item["channel"]}
            for key, item in self.values["channels"].items()]

    def _save(self, values):
        self._validate(values)
        if self.path.is_symlink():
            raise StoreError("The channel list changed outside the app. Reopen the app before continuing.")
        current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        if current != self.digest:
            raise StoreError("The channel list changed outside the app. Reopen the app before continuing.")
        data = _atomic_json(self.path, values)
        self.values, self.digest = values, hashlib.sha256(data).hexdigest()

    def add(self, channel, primary):
        channel = normalize_channel(channel)
        if not channel:
            raise ValueError("Paste the channel or message link you want to archive.")
        for entry in self.entries(primary):
            if normalize_channel(entry["channel"]) == channel:
                return entry["id"], False
        values = copy.deepcopy(self.values)
        key = uuid.uuid4().hex
        parent = self.root / "channels"
        if parent.is_symlink():
            raise StoreError("Channel archives must be ordinary folders inside this project.")
        parent.mkdir(parents=True, exist_ok=True)
        folder = parent / key
        folder.mkdir(mode=0o700)
        values["channels"][key] = {"channel": channel}
        try:
            self._save(values)
        except BaseException:
            # Only remove this call's new, still-empty directory on failure.
            try:
                folder.rmdir()
            except OSError:
                pass
            raise
        return key, True

    def select(self, key):
        self.folder(key)
        if key != self.active:
            values = copy.deepcopy(self.values)
            values["active"] = key
            self._save(values)
