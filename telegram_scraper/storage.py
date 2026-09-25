"""Lossless, atomic local storage and read-only archive verification.

The repository's historical ``telegram_data`` layout remains portable.  Merely
constructing a Store never creates directories or modifies an existing library.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator


class StoreError(RuntimeError):
    """The library could not be handled safely; existing data is preserved."""


class OperationCancelled(StoreError):
    """A cancellable storage operation was stopped without publishing output."""


Cancel = Callable[[], bool] | None
Report = Callable[[str], None] | None
_CHUNK = 1024 * 1024
_VOLATILE_RAW = {
    "views", "forwards", "replies", "reactions", "file_reference",
    "access_hash", "edit_hide", "pinned",
}


def _cancelled(cancel: Cancel) -> None:
    if cancel and cancel():
        raise OperationCancelled("Stopped. Your existing library and archives are unchanged.")


def _report(report: Report, message: str) -> None:
    if report:
        report(message)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _decode(data: bytes, source: str) -> object:
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise StoreError(f"{source} is not valid JSON. Restore a known-good copy before syncing.") from exc


def _records(value: object, source: str) -> dict[int, dict]:
    if not isinstance(value, list):
        raise StoreError(f"{source} must contain a list of saved posts. It has not been changed.")
    records: dict[int, dict] = {}
    for position, record in enumerate(value, 1):
        if not isinstance(record, dict):
            raise StoreError(f"{source}: saved post {position} is not an object.")
        message_id = record.get("id")
        if type(message_id) is not int or message_id <= 0:
            raise StoreError(f"{source}: saved post {position} has an invalid message ID.")
        if message_id in records:
            raise StoreError(f"{source} contains duplicate message ID {message_id}. Nothing was overwritten.")
        if "revisions" in record and not isinstance(record["revisions"], list):
            raise StoreError(f"{source}: post {message_id} has invalid revision history.")
        records[message_id] = record
    return records


def _stable_raw(value: object) -> object:
    if isinstance(value, dict):
        return {key: _stable_raw(item) for key, item in value.items() if key not in _VOLATILE_RAW}
    if isinstance(value, list):
        return [_stable_raw(item) for item in value]
    return value


def retained_records(records):
    """Walk current posts, retained revisions and attachment receipts together."""
    def walk(record, message_id, label):
        if not isinstance(record, dict):
            raise StoreError(f"{label}: invalid retained record.")
        yield message_id, label, record
        for field in ("revisions", "previous_media"):
            items = record.get(field, [])
            if not isinstance(items, list):
                raise StoreError(f"{label}: invalid {field} history.")
            for index, item in enumerate(items, 1):
                yield from walk(item, message_id, f"{label}, {field} {index}")
    values = records.values() if isinstance(records, dict) else records
    for record in values:
        yield from walk(record, record.get("id"), f"Post {record.get('id')}")


def _meaningful(record: dict) -> dict:
    keys = ("text", "date", "type", "action", "action_title", "edit_date", "media_id")
    result = {key: record.get(key) for key in keys}
    # Adding raw metadata to a legacy record is enrichment, not an edit. The
    # caller compares raw only when both versions already have it.
    return result


def _within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _enumeration_error(error: OSError) -> None:
    raise StoreError(f"Could not enumerate the complete library for backup: {error.filename or error}.") from error


def _atomic_json(path: Path, value: object) -> bytes:
    try:
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StoreError("A post contains unsupported data. The saved library was left unchanged.") from exc
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
        return encoded
    except OSError as exc:
        raise StoreError("Could not save the library. Check free disk space and folder permissions.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.data_dir = self.root / "telegram_data"
        self.media_dir = self.data_dir / "media"
        self.archives_dir = self.root / "archives"
        self.messages_file = self.data_dir / "messages_all.json"
        self.records: dict[int, dict] = {}
        self._loaded = False
        self._disk_digest: str | None = None
        self._lock_fd: int | None = None
        self._lock_owner: int | None = None
        self._lock_depth = 0
        self._revision_epoch = uuid.uuid4().hex
        self._revision_counter = 0

    @property
    def revision(self):
        return f"{self._revision_epoch}:{self._revision_counter}"

    def _check_layout(self) -> None:
        for directory in (self.data_dir, self.media_dir, self.archives_dir):
            if not _within(directory, self.root):
                raise StoreError(f"The {directory.name} folder points outside this library. Choose a regular local folder.")
        if self.messages_file.is_symlink():
            raise StoreError("messages_all.json is a symbolic link. Use a regular file to keep saves safe.")
        if not _within(self.media_dir, self.data_dir):
            raise StoreError("The media folder points outside telegram_data. Use a regular local media folder.")

    def _read_records(self) -> tuple[dict[int, dict], str | None]:
        self._check_layout()
        if not self.messages_file.exists():
            if self.media_dir.exists() and any(self.media_dir.iterdir()):
                raise StoreError("Saved media exists but messages_all.json is missing. Restore the message file before syncing.")
            return {}, None
        try:
            data = self.messages_file.read_bytes()
        except OSError as exc:
            raise StoreError("The saved posts could not be read. Check the library folder's permissions.") from exc
        return _records(_decode(data, "messages_all.json"), "messages_all.json"), hashlib.sha256(data).hexdigest()

    def load(self) -> dict[int, dict]:
        records, digest = self._read_records()
        self.records = records
        self._disk_digest = digest
        self._loaded = True
        self._revision_counter += 1
        return self.records

    def save(self) -> None:
        self._check_layout()
        if not self._loaded:
            if self.messages_file.exists():
                raise StoreError("Load the existing library before saving so previous posts are preserved.")
            self._read_records()  # Reject orphaned media instead of treating it as a new library.
        try:
            actual_digest = hashlib.sha256(self.messages_file.read_bytes()).hexdigest() if self.messages_file.exists() else None
        except OSError as exc:
            raise StoreError("The saved posts could not be checked before saving.") from exc
        if actual_digest != self._disk_digest:
            raise StoreError("The library changed in another program. Reload it before syncing again; nothing was overwritten.")
        result = []
        for index, message_id in enumerate(sorted(self.records), 1):
            original = self.records[message_id]
            if original.get("id") != message_id:
                raise StoreError("A saved post has mismatched message IDs. Nothing was overwritten.")
            record = {"entry": index, **copy.deepcopy(original)}
            record["entry"] = index
            result.append(record)
        validated = _records(result, "The library")
        encoded = _atomic_json(self.messages_file, result)
        self.records = validated
        self._disk_digest = hashlib.sha256(encoded).hexdigest()
        self._loaded = True

    def upsert(self, record: dict) -> str:
        incoming = copy.deepcopy(record)
        _records([incoming], "The downloaded post")
        message_id = incoming["id"]
        old = self.records.get(message_id)
        if old is None:
            self.records[message_id] = incoming
            self._revision_counter += 1
            return "added"
        merged = {**copy.deepcopy(old), **incoming}
        history = copy.deepcopy(old.get("revisions", []))
        for item in incoming.get("revisions", []):
            if item not in history:
                history.append(item)
        meaningful_change = _meaningful(old) != _meaningful(merged)
        if "raw" in old and "raw" in merged:
            meaningful_change |= _stable_raw(old["raw"]) != _stable_raw(merged["raw"])
        if meaningful_change:
            prior = {key: copy.deepcopy(value) for key, value in old.items() if key != "revisions"}
            prior["archived_at"] = datetime.now(timezone.utc).isoformat()
            history.append(prior)
        if history or "revisions" in old or "revisions" in incoming:
            merged["revisions"] = history
        if old == merged:
            return "unchanged"
        self.records[message_id] = merged
        self._revision_counter += 1
        return "updated"

    def media_path(self, record: dict) -> Path | None:
        value = record.get("media_file")
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        normalized = value.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if ".." in parts:
            return None
        if Path(normalized).is_absolute():
            candidate = Path(normalized)
        elif parts[:2] == ("telegram_data", "media"):
            candidate = self.data_dir / Path(*parts[1:])
        elif parts and parts[0] == "media":
            candidate = self.data_dir / Path(*parts)
        elif len(parts) == 1 and ":" not in parts[0]:
            candidate = self.media_dir / parts[0]
        else:
            return None
        if not _within(self.media_dir, self.data_dir) or not _within(self.media_dir, self.root) or not _within(candidate, self.media_dir):
            return None
        if candidate.resolve() == self.media_dir.resolve():
            return None
        return candidate.resolve()

    def channel_info(self) -> dict | None:
        self._check_layout()
        path = self.data_dir / "channel.json"
        if not path.exists():
            return None
        if path.is_symlink():
            raise StoreError("The channel identity file is a symbolic link. It cannot be used safely.")
        try:
            value = _decode(path.read_bytes(), "channel.json")
        except OSError as exc:
            raise StoreError("The saved channel identity could not be read.") from exc
        if not isinstance(value, dict) or type(value.get("id")) is not int or not value["id"]:
            raise StoreError("The saved channel identity is invalid. Resolve it before syncing another channel.")
        if "peer_kind" in value and value["peer_kind"] not in {"channel", "chat"}:
            raise StoreError("The saved channel peer kind is invalid.")
        return value

    def channel_identity(self):
        info = self.channel_info()
        if not info:
            return None
        kind = info.get("peer_kind")
        if kind is None:
            from .identity import peer_identity
            candidates = set()
            records, _ = self._read_records()
            objects = [(record.get("raw") or {}).get("peer_id") for _, _, record in retained_records(records)]
            from .evidence import EvidenceStore, EvidenceError, _digest
            evidence = EvidenceStore(self.root)
            if evidence.path.exists():
                with evidence._read() as connection:
                    for row in connection.execute("SELECT payload_json,tl_bytes,payload_sha256 FROM observations WHERE kind='channel' AND subject_id=?", (str(info["id"]),)):
                        if _digest(row[0], row[1]) != row[2]:
                            raise EvidenceError("Saved channel evidence no longer matches its checksum. Restore it before resolving archive identity.")
                        objects.append(json.loads(row[0]))
            for obj in objects:
                if obj:
                    try:
                        identity = peer_identity(obj)
                    except (StoreError, TypeError, AttributeError):
                        continue
                    if identity["id"] == info["id"]:
                        candidates.add(identity["kind"])
            if len(candidates) == 1:
                kind = candidates.pop()
        return {"kind": kind, "id": info["id"]}

    def bind_channel(self, id: int, title: str, configured_channel: str, legacy_channel: str | None = None,
                     *, peer_kind=None, confirmed=False) -> None:
        if type(id) is not int or not id:
            raise StoreError("Telegram did not provide a valid channel identity.")
        existing = self.channel_info()
        if existing:
            identity = self.channel_identity()
            if existing["id"] != id or peer_kind and identity["kind"] and peer_kind != identity["kind"]:
                raise StoreError("This library belongs to a different Telegram channel. Use a separate library folder for this channel.")
            if peer_kind and identity["kind"] is None and not confirmed:
                raise StoreError("Confirm this archive's original channel or group before capturing.")
            values = {**existing, "title": title, **({"peer_kind": peer_kind} if peer_kind else {})}
            if values != existing:
                _atomic_json(self.data_dir / "channel.json", values)
            return
        records, _ = self._read_records()
        from .config import normalize_channel, ConfigError
        configured = str(configured_channel or "").strip().rstrip("/")
        legacy = str(legacy_channel or "").strip().rstrip("/")
        try:
            configured, legacy = normalize_channel(configured), normalize_channel(legacy)
        except ConfigError:
            # Storage also reads older identifiers. Keep the exact-match
            # boundary for those; current user input is validated in Settings.
            pass
        if records and not confirmed and (not legacy or configured != legacy):
            raise StoreError("This existing library has no saved channel identity. Reconnect its original configured channel before syncing.")
        _atomic_json(self.data_dir / "channel.json", {"version": 1, "id": id, "title": title, **({"peer_kind": peer_kind} if peer_kind else {})})

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Take an immediate advisory writer lock; never wait on another process."""
        owner = threading.get_ident()
        if self._lock_fd is not None and self._lock_owner == owner:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = None
        try:
            descriptor = os.open(self.root / ".telegram-scraper.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise StoreError("This library is already open in another scraper. Close that copy and try again.") from exc
            raise StoreError("The library could not be locked. Check folder permissions.") from exc
        self._lock_fd, self._lock_owner, self._lock_depth = descriptor, owner, 1
        try:
            yield
        finally:
            self._lock_depth = 0
            self._lock_fd = self._lock_owner = None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def has_snapshot_data(self) -> bool:
        self._check_layout()
        if self.records:
            return True
        return any(path.is_file() and path.name not in {"messages_all.json", "channel.json"}
                   for path in self.data_dir.rglob("*")) if self.data_dir.exists() else False

    def snapshot(self, cancel: Cancel = None, report: Report = None) -> Path | None:
        self._check_layout()
        _cancelled(cancel)
        records, _ = self._read_records()
        if not self.data_dir.exists():
            return None
        files = []
        for directory, directories, names in os.walk(self.data_dir, followlinks=False, onerror=_enumeration_error):
            _cancelled(cancel)
            for name in sorted(directories + names):
                path = Path(directory) / name
                if path.is_symlink():
                    raise StoreError("The library contains symbolic links. A complete, safe backup cannot be created.")
            files.extend(Path(directory) / name for name in sorted(names))
        if not files:
            return None
        if not records and all(path.name in {"messages_all.json", "channel.json"} and path.parent == self.data_dir for path in files):
            return None
        self.archives_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        target = self.archives_dir / f"telegram_data_archive_PRE_SYNC_{stamp}_{uuid.uuid4().hex[:8]}.zip"
        partial = target.with_suffix(".zip.partial")
        owned_partial = False
        source_stats = {}
        try:
            with partial.open("xb") as output:
                owned_partial = True
                os.chmod(partial, 0o600)
                with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
                    for position, path in enumerate(files, 1):
                        _cancelled(cancel)
                        relative = path.relative_to(self.data_dir).as_posix()
                        _report(report, f"Backing up file {position} of {len(files)}: {relative}")
                        before = path.stat()
                        source_stats[path] = (before.st_size, before.st_mtime_ns, before.st_ino)
                        if not stat.S_ISREG(before.st_mode):
                            raise StoreError("The library contains a file that cannot be safely backed up.")
                        info = zipfile.ZipInfo.from_file(path, relative, strict_timestamps=False)
                        info.compress_type = zipfile.ZIP_DEFLATED if path.suffix.lower() == ".json" else zipfile.ZIP_STORED
                        with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
                            while chunk := source.read(_CHUNK):
                                _cancelled(cancel)
                                destination.write(chunk)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise StoreError("A library file changed during backup. Stop other writers and try again.")
                output.flush()
                os.fsync(output.fileno())
            _cancelled(cancel)
            current_files = set()
            for directory, directories, names in os.walk(self.data_dir, followlinks=False, onerror=_enumeration_error):
                _cancelled(cancel)
                for name in directories + names:
                    path = Path(directory) / name
                    if path.is_symlink():
                        raise StoreError("A library file changed during backup. Stop other writers and try again.")
                current_files.update(Path(directory) / name for name in names)
            if current_files != set(source_stats):
                raise StoreError("The library's files changed during backup. Stop other writers and try again.")
            for path, before in source_stats.items():
                after = path.stat()
                if before != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise StoreError("A library file changed during backup. Stop other writers and try again.")
            # Exclusive publication keeps even an unexpected filename collision
            # from overwriting a user's existing backup.
            os.link(partial, target)
            partial.unlink()
            owned_partial = False
            _fsync_directory(self.archives_dir)
            return target
        except OSError as exc:
            raise StoreError("The backup could not be completed. Check free disk space and folder permissions.") from exc
        finally:
            if owned_partial:
                partial.unlink(missing_ok=True)

    def _archive_media_name(self, record: dict) -> str | None:
        # An archived path describes ZIP contents. Resolve it lexically, without
        # allowing today's missing files or symbolic links to rewrite history.
        value = record.get("media_file")
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        normalized = value.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if ".." in parts:
            return None
        if Path(normalized).is_absolute():
            try:
                tail = Path(normalized).relative_to(self.media_dir).parts
            except ValueError:
                return None
        elif parts[:2] == ("telegram_data", "media"):
            tail = parts[2:]
        elif parts and parts[0] == "media":
            tail = parts[1:]
        elif len(parts) == 1 and ":" not in parts[0]:
            tail = parts
        else:
            return None
        return PurePosixPath("media", *tail).as_posix() if tail else None

    @staticmethod
    def _compare_records(previous: dict[int, dict], current: dict[int, dict], label: str,
                         issues: list[str], changes: list[str]) -> None:
        for message_id, old in previous.items():
            new = current.get(message_id)
            if new is None:
                issues.append(f"{label}: previously saved post {message_id} is missing.")
                continue
            changed = [key for key in ("text", "date", "type", "media_id") if old.get(key) != new.get(key)]
            if changed:
                changes.append(f"{label}: post {message_id} changed ({', '.join(changed)}). This may be a Telegram edit.")

    def verify(self, cancel: Cancel = None, report: Report = None) -> dict:
        """Read every ZIP member to validate its CRC, plus referenced media hashes.

        Missing data and damaged/replaced bytes are issues. Message edits and new
        posts are reported separately as changes, not asserted to be corruption.
        No archive is ever extracted and no library data is written.
        """
        result = {"ok": False, "checked_messages": 0, "checked_media": 0,
                  "checked_archives": 0, "issues": [], "changes": [], "cancelled": False}
        issues, changes = result["issues"], result["changes"]
        try:
            _cancelled(cancel)
            local, _ = self._read_records()
            try:
                local_channel = self.channel_info()
            except StoreError as exc:
                local_channel = None
                issues.append(str(exc))
            if not self.messages_file.exists():
                issues.append("There are no saved posts to verify yet.")
            result["checked_messages"] = len(local)
            from .integrity import verify_extended
            local_media: dict[str, tuple[int, str]] = {}
            extended = verify_extended(self.root, cancel=cancel, report=report, primary_cache=local_media)
            issues.extend(extended["issues"])
            result["limitations"] = extended["limitations"]
            result.update({key: value for key, value in extended.items() if key.startswith("checked_")})
            if extended["cancelled"]:
                raise OperationCancelled("Source verification was stopped.")
            result["checked_media"] = len(local_media)
            for position, (_, receipt_label, record) in enumerate(retained_records(local), 1):
                _cancelled(cancel)
                if not record.get("media_file"):
                    if record.get("media_status") in ("failed", "missing", "pending"):
                        if "," not in receipt_label:
                            issues.append(f"{receipt_label}: media has not been downloaded successfully.")
                    continue
                path = self.media_path(record)
                if path is None:
                    issues.append(f"{receipt_label}: the media path is unsafe or unsupported.")
                    continue
                name = path.relative_to(self.data_dir.resolve()).as_posix()
                if not path.is_file():
                    issues.append(f"{receipt_label}: saved media is missing ({name}).")
                    continue
                if name not in local_media:
                    _report(report, f"Checking saved media for post {position} of {len(local)}")
                    digest, size = hashlib.sha256(), 0
                    try:
                        with path.open("rb") as handle:
                            while chunk := handle.read(_CHUNK):
                                _cancelled(cancel)
                                size += len(chunk)
                                digest.update(chunk)
                    except OSError:
                        issues.append(f"{receipt_label}: saved media could not be read ({name}).")
                        continue
                    local_media[name] = size, digest.hexdigest()
                    result["checked_media"] += 1
                self._check_media_record(record, local_media[name], receipt_label, issues)

            archives = sorted(self.archives_dir.glob("*.zip")) if self.archives_dir.exists() else []
            if not archives:
                issues.append("No complete backup archives were found; backup integrity is unverified.")
            for partial in sorted(self.archives_dir.glob("*.partial")) if self.archives_dir.exists() else []:
                issues.append(f"Incomplete backup found: {partial.name}. It is not a usable archive.")
            cumulative: dict[int, dict] = {}
            cumulative_media: dict[str, tuple[int, str]] = {}
            archive_channel_id = None
            for index, archive_path in enumerate(archives, 1):
                _cancelled(cancel)
                label = archive_path.name
                _report(report, f"Checking backup {index} of {len(archives)}: {label}")
                if archive_path.is_symlink():
                    issues.append(f"{label}: symbolic-link archives cannot be verified safely.")
                    continue
                try:
                    with tempfile.TemporaryDirectory(prefix="telegram-backup-check-") as temporary, zipfile.ZipFile(archive_path) as archive:
                        from .evidence import EvidenceStore
                        backup_evidence = EvidenceStore(temporary)
                        backup_evidence.data_dir.mkdir()
                        issue_start = len(issues)
                        payload = None
                        channel_payload = None
                        media = {}
                        seen = set()
                        for info in archive.infolist():
                            _cancelled(cancel)
                            name = info.filename.replace("\\", "/")
                            parts = PurePosixPath(name).parts
                            if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
                                issues.append(f"{label}: contains an unsafe archive path.")
                            if parts and parts[0] == "telegram_data":
                                name = PurePosixPath(*parts[1:]).as_posix()
                            if info.is_dir():
                                continue
                            if name in seen:
                                issues.append(f"{label}: duplicate archive member {name}.")
                            seen.add(name)
                            if stat.S_ISLNK(info.external_attr >> 16):
                                issues.append(f"{label}: contains a symbolic link ({name}).")
                            digest, size = hashlib.sha256(), 0
                            message_chunks = [] if name in {"messages_all.json", "channel.json"} else None
                            from contextlib import nullcontext
                            sink = backup_evidence.path.open("wb") if name == "evidence.sqlite3" else nullcontext(None)
                            with archive.open(info) as handle, sink as database:
                                while chunk := handle.read(_CHUNK):
                                    _cancelled(cancel)
                                    size += len(chunk)
                                    if name.startswith("media/"):
                                        digest.update(chunk)
                                    if message_chunks is not None:
                                        message_chunks.append(chunk)
                                    if database is not None:
                                        database.write(chunk)
                            if message_chunks is not None:
                                if name == "messages_all.json":
                                    payload = b"".join(message_chunks)
                                else:
                                    channel_payload = b"".join(message_chunks)
                            if name.startswith("media/"):
                                media[name] = size, digest.hexdigest()
                        if payload is None and "evidence.sqlite3" not in seen:
                            raise StoreError("messages_all.json is missing from the backup")
                        archived = _records(_decode(payload, label), label) if payload is not None else {}
                        channel = None
                        if channel_payload is not None:
                            channel = _decode(channel_payload, f"{label}: channel.json")
                            if not isinstance(channel, dict) or type(channel.get("id")) is not int or not channel["id"]:
                                issues.append(f"{label}: the saved channel identity is invalid.")
                            else:
                                if archive_channel_id is not None and channel["id"] != archive_channel_id:
                                    issues.append(f"{label}: the channel identity differs from previous backups.")
                                if local_channel and channel["id"] != local_channel["id"]:
                                    issues.append(f"{label}: the channel identity differs from the current library.")
                                archive_channel_id = channel["id"]
                        if any(name in seen for name in ("evidence.sqlite3-journal", "evidence.sqlite3-wal", "evidence.sqlite3-shm")):
                            issues.append(f"{label}: evidence database requires journal recovery; verification did not repair it.")
                        else:
                            try:
                                backup_evidence.validate(cancel=cancel, records=archived, channel=channel)
                                from .integrity import check_primary_evidence
                                check_primary_evidence(backup_evidence, lambda path: media.get(path.removeprefix("telegram_data/")),
                                                       issues, result["limitations"], cancel, label)
                            except StoreError as exc:
                                issues.append(f"{label}: {exc}")
                        for _, receipt_label, record in retained_records(archived):
                            if record.get("media_file"):
                                name = self._archive_media_name(record)
                                if name is None:
                                    issues.append(f"{label}: {receipt_label} has an unsafe media path.")
                                elif name not in media:
                                    issues.append(f"{label}: {receipt_label} is missing its media ({name}).")
                                else:
                                    self._check_media_record(record, media[name], f"{label}, {receipt_label}", issues)
                        self._compare_records(cumulative, archived, label, issues, changes)
                        for name, evidence in cumulative_media.items():
                            if name not in media:
                                issues.append(f"{label}: previously archived media is missing ({name}).")
                            elif media[name] != evidence:
                                issues.append(f"{label}: previously archived media bytes changed ({name}).")
                        cumulative.update(archived)
                        cumulative_media.update(media)
                        if len(issues) == issue_start:
                            result["checked_archives"] += 1
                except OperationCancelled:
                    raise
                except (OSError, ValueError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
                    issues.append(f"{label}: backup could not be verified ({str(exc)}).")
            self._compare_records(cumulative, local, "Current library", issues, changes)
            if archive_channel_id is not None and local_channel is None:
                issues.append("Current library: saved channel identity is missing or invalid, but exists in the backups.")
            new_count = len(local.keys() - cumulative.keys())
            if new_count and archives:
                changes.append(f"{new_count} current post(s) are newer than the verified backups.")
            for name, evidence in cumulative_media.items():
                _cancelled(cancel)
                path = self.media_path({"media_file": name})
                if path is None or not path.is_file():
                    issues.append(f"Current library: previously archived media is missing ({name}).")
                    continue
                current = local_media.get(name)
                if current is None:
                    digest, size = hashlib.sha256(), 0
                    try:
                        with path.open("rb") as handle:
                            while chunk := handle.read(_CHUNK):
                                _cancelled(cancel)
                                size += len(chunk)
                                digest.update(chunk)
                        current = (size, digest.hexdigest())
                        result["checked_media"] += 1
                    except OSError:
                        issues.append(f"Current library: previously archived media could not be read ({name}).")
                        continue
                if current != evidence:
                    issues.append(f"Current library: previously archived media bytes changed ({name}).")
        except OperationCancelled:
            result["cancelled"] = True
            issues.append("Verification was stopped before all files were checked.")
        except (StoreError, OSError) as exc:
            issues.append(str(exc))
        result["ok"] = not issues
        return result

    @staticmethod
    def _check_media_record(record: dict, actual: tuple[int, str], label: str, issues: list[str]) -> None:
        expected_size = record.get("media_size")
        expected_hash = record.get("media_sha256")
        if expected_size is not None and expected_size != actual[0]:
            issues.append(f"{label}: saved media size does not match the recorded size.")
        if expected_hash is not None and str(expected_hash).lower() != actual[1]:
            issues.append(f"{label}: saved media checksum does not match; the file may have changed or been damaged.")
        if actual[0] == 0:
            issues.append(f"{label}: saved media is empty.")
