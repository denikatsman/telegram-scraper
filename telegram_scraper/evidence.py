"""Durable source observations, separate from the convenient message index.

Every changed JSON payload or Telegram TL byte sequence gets an immutable row.
Repeated observations keep their own occurrence and run association. A returned
write has committed with SQLite FULL synchronization and closed its connection.
The caller owns the library writer lock. Finish ``prepare`` before a Store
snapshot, and keep evidence writes quiescent for the entire file-copy operation.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from .storage import OperationCancelled, StoreError


SCHEMA_VERSION = 1
APPLICATION_ID = 0x54474145  # TGAE: Telegram Archive Evidence
_TERMINAL = {"completed", "warning", "cancelled", "error", "interrupted", "failed", "partial", "stopped"}


class EvidenceError(StoreError):
    """Evidence cannot be preserved safely; derived archive writes must stop."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: object) -> str:
    def check(item):
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise EvidenceError("Raw evidence contains an unsupported value. Keep its original type or bytes before saving it.")

    try:
        check(value)
        # ASCII escaping also preserves isolated UTF-16 surrogates if Telegram
        # ever returns malformed text; UTF-8 replacement would lose those bytes.
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("Raw evidence could not be encoded without losing information. Capture stopped before updating the library.") from exc


def _digest(payload_json: str, tl_bytes: bytes | None) -> str:
    encoded = payload_json.encode("utf-8")
    digest = hashlib.sha256(b"telegram-evidence-v1\x00")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(b"\x00" if tl_bytes is None else b"\x01")
    if tl_bytes is not None:
        digest.update(len(tl_bytes).to_bytes(8, "big"))
        digest.update(tl_bytes)
    return digest.hexdigest()


def _channel(value):
    if value is None:
        return None
    if type(value) is not int or value == 0 or not -(2**63) < value < 2**63:
        raise EvidenceError("The evidence run needs a valid Telegram channel ID.")
    return value


def _timestamp(value: str | None) -> str:
    if value is None:
        return _now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (AttributeError, ValueError, TypeError) as exc:
        raise EvidenceError("An evidence observation needs a timestamp with its time zone.") from exc
    return value


_SCHEMA = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
        status TEXT NOT NULL, mode TEXT NOT NULL, channel_id INTEGER,
        metadata_json TEXT NOT NULL, counts_json TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
        issues_json TEXT NOT NULL, context_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE observations (
        observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, channel_id INTEGER, subject_id TEXT NOT NULL,
        observed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        tl_bytes BLOB, payload_sha256 TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX observations_identity ON observations (
        kind, COALESCE(channel_id, 0), subject_id, payload_sha256
    )""",
    """CREATE TABLE occurrences (
        occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        observation_id INTEGER NOT NULL REFERENCES observations(observation_id),
        observed_at TEXT NOT NULL, context_json TEXT NOT NULL,
        parent_channel_id INTEGER, parent_message_id INTEGER
    )""",
    "CREATE INDEX occurrences_run ON occurrences(run_id, occurrence_id)",
    "CREATE INDEX occurrences_observation ON occurrences(observation_id, occurrence_id)",
    "CREATE INDEX occurrences_parent ON occurrences(parent_channel_id, parent_message_id, occurrence_id)",
    """CREATE TABLE run_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        occurred_at TEXT NOT NULL, event_json TEXT NOT NULL
    )""",
    "CREATE INDEX events_run ON run_events(run_id, event_id)",
    *(
        f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, 'Evidence records are append-only'); END"
        for table in ("observations", "occurrences", "run_events") for action in ("UPDATE", "DELETE")
    ),
    "CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'Evidence runs cannot be deleted'); END",
    """CREATE TRIGGER runs_preserve_origin BEFORE UPDATE ON runs
        WHEN OLD.run_id != NEW.run_id OR OLD.started_at != NEW.started_at
        OR OLD.metadata_json != NEW.metadata_json OR OLD.mode != NEW.mode
        BEGIN SELECT RAISE(ABORT, 'An evidence run origin is immutable'); END""",
)
_TABLE_COLUMNS = {
    "runs": ("run_id", "started_at", "ended_at", "status", "mode", "channel_id", "metadata_json", "counts_json", "checkpoint_json", "coverage_json", "issues_json", "context_json", "updated_at"),
    "observations": ("observation_id", "kind", "channel_id", "subject_id", "observed_at", "payload_json", "tl_bytes", "payload_sha256"),
    "occurrences": ("occurrence_id", "run_id", "observation_id", "observed_at", "context_json", "parent_channel_id", "parent_message_id"),
    "run_events": ("event_id", "run_id", "occurred_at", "event_json"),
}
_EXPECTED_OBJECTS = {statement.split()[2] if not statement.startswith("CREATE UNIQUE") else statement.split()[3]
                     for statement in _SCHEMA}
_EXPECTED_DEFINITIONS = {statement.split()[2] if not statement.startswith("CREATE UNIQUE") else statement.split()[3]:
                         " ".join(statement.split()) for statement in _SCHEMA}


class EvidenceStore:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.data_dir = self.root / "telegram_data"
        self.path = self.data_dir / "evidence.sqlite3"
        self._mutex = threading.RLock()
        self._validated_signature = None

    def _safe_path(self) -> None:
        try:
            self.data_dir.resolve().relative_to(self.root)
        except (ValueError, RuntimeError, OSError) as exc:
            raise EvidenceError("The evidence folder points outside this library.") from exc
        if self.path.is_symlink() or any(Path(str(self.path) + suffix).is_symlink() for suffix in ("-journal", "-wal", "-shm")):
            raise EvidenceError("The evidence database or its journal is a symbolic link. Capture cannot continue safely.")
        if self.path.exists() and not self.path.is_file():
            raise EvidenceError("The evidence database path is not a regular file.")

    def _signature(self):
        value = self.path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    def _check_header(self) -> None:
        # Fail before opening a future or unrelated database for crash recovery.
        try:
            with self.path.open("rb") as file:
                header = file.read(100)
        except OSError as exc:
            raise EvidenceError("The evidence database could not be read. Check folder permissions.") from exc
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
            raise EvidenceError("The evidence database is damaged or incomplete. Restore a known-good copy before capturing more posts.")
        version = int.from_bytes(header[60:64], "big")
        app_id = int.from_bytes(header[68:72], "big")
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise EvidenceError("The evidence database uses an unsupported format. Keep it unchanged and use the app version that created it.")

    @staticmethod
    def _connection(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
        mode = "ro" if readonly else "rw"
        connection = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if readonly:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA fullfsync = ON")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise EvidenceError("The evidence database uses an unsupported format. It was left unchanged.")
        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
            raise EvidenceError("The evidence database uses an unsupported journal mode. It must be recovered and closed before backing up or capturing more posts.")
        definitions = {row["name"]: " ".join((row["sql"] or "").split()) for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if set(definitions) != _EXPECTED_OBJECTS or definitions != _EXPECTED_DEFINITIONS:
            raise EvidenceError("The evidence database structure is incomplete or unfamiliar. Capture stopped to preserve it.")
        for table, expected in _TABLE_COLUMNS.items():
            columns = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
            if columns != expected:
                raise EvidenceError("The evidence database structure is incomplete or unfamiliar. Capture stopped to preserve it.")

    @staticmethod
    def _deep_check(connection: sqlite3.Connection, cancel=None) -> dict:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if len(integrity) != 1 or integrity[0][0] != "ok":
            raise EvidenceError("The evidence database failed its integrity check. Restore a known-good copy before capturing more posts.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise EvidenceError("Evidence run associations are damaged. Capture stopped to preserve the database.")
        for row in connection.execute("SELECT kind,channel_id,subject_id,payload_json,tl_bytes,payload_sha256 FROM observations"):
            if cancel and cancel():
                raise OperationCancelled("Evidence verification stopped before all observations were checked.")
            try:
                if _json(json.loads(row["payload_json"])) != row["payload_json"] or _digest(row["payload_json"], row["tl_bytes"]) != row["payload_sha256"]:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, EvidenceError) as exc:
                raise EvidenceError("Saved raw evidence no longer matches its checksum. Restore a known-good copy before capturing more posts.") from exc
            try:
                _channel(row["channel_id"])
                if not row["kind"] or not row["subject_id"] or row["kind"] == "message" and (row["channel_id"] is None or int(row["subject_id"]) <= 0):
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise EvidenceError("Saved observation identities are damaged. Capture stopped to preserve the database.") from exc
        for table, columns in (("runs", ("metadata_json", "counts_json", "checkpoint_json", "coverage_json", "issues_json", "context_json")),
                               ("occurrences", ("context_json",)), ("run_events", ("event_json",))):
            for row in connection.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                if cancel and cancel():
                    raise OperationCancelled("Evidence verification stopped before all run provenance was checked.")
                for column, encoded in zip(columns, row):
                    try:
                        value = json.loads(encoded)
                        if not isinstance(value, list if column == "issues_json" else dict) or _json(value) != encoded:
                            raise ValueError()
                    except (ValueError, TypeError, EvidenceError) as exc:
                        raise EvidenceError("Saved evidence provenance is damaged. Capture stopped to preserve the database.") from exc
        for row in connection.execute("SELECT status,started_at,ended_at,channel_id FROM runs"):
            if row["status"] not in _TERMINAL | {"running"} or (row["ended_at"] is None) != (row["status"] == "running"):
                raise EvidenceError("Saved evidence run completion records are damaged. Capture stopped to preserve the database.")
            _timestamp(row["started_at"])
            if row["ended_at"] is not None:
                _timestamp(row["ended_at"])
            _channel(row["channel_id"])
        return {"ok": True, "exists": True, "schema_version": SCHEMA_VERSION,
                **{name: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                   for name, table in (("observations", "observations"), ("runs", "runs"), ("events", "run_events"), ("occurrences", "occurrences"))}}

    def validate(self, cancel=None) -> dict:
        """Check existing evidence without creating, migrating, or repairing files."""
        with self._mutex:
            if cancel and cancel():
                raise OperationCancelled("Evidence verification was stopped.")
            self._safe_path()
            if not self.path.exists():
                if any(Path(str(self.path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
                    raise EvidenceError("Evidence journal files exist but the database is missing. Restore the database before capturing more posts.")
                return {"ok": True, "exists": False, "schema_version": SCHEMA_VERSION, "observations": 0, "runs": 0, "events": 0, "occurrences": 0}
            self._check_header()
            connection = None
            try:
                connection = self._connection(self.path, readonly=True)
                if cancel:
                    connection.set_progress_handler(lambda: int(bool(cancel())), 10000)
                connection.execute("BEGIN")
                self._check_schema(connection)
                result = self._deep_check(connection, cancel)
                self._validated_signature = self._signature()
                connection.rollback()
                return result
            except sqlite3.Error as exc:
                if cancel and cancel():
                    raise OperationCancelled("Evidence verification stopped before all observations were checked.") from exc
                raise EvidenceError("The evidence database could not be verified. It may need crash recovery, or be damaged or busy; capture has stopped.") from exc
            finally:
                if connection is not None:
                    connection.close()

    def prepare(self) -> dict:
        """Recover an interrupted DELETE-journal transaction, then fully validate.

        Call only while holding the library writer lock, before taking a snapshot.
        This does not create a database or mark an unfinished run as completed.
        """
        with self._mutex:
            self._safe_path()
            if not self.path.exists():
                return self.validate()
            self._check_header()
            connection = None
            try:
                connection = self._connection(self.path)
                self._check_schema(connection)  # A first read lets SQLite roll back a hot journal.
            except sqlite3.Error as exc:
                raise EvidenceError("The evidence database could not be recovered safely. Keep it and its journal files together and restore a known-good copy.") from exc
            finally:
                if connection is not None:
                    connection.close()
            return self.validate()

    def _initialize(self) -> None:
        self._safe_path()
        if self.path.exists():
            return
        self.validate()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.data_dir / f".evidence-{uuid.uuid4().hex}.sqlite3.partial"
        connection = None
        owned = False
        try:
            descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            owned = True
            os.close(descriptor)
            connection = self._connection(temporary)
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.commit()
            connection.close()
            connection = None
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                # Another owner may have initialized between the absence check
                # and publication. Validate it; never overwrite its database.
                self._check_header()
            temporary.unlink()
            owned = False
            descriptor = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceError("The evidence database could not be created. Check free disk space and folder permissions.") from exc
        finally:
            if connection is not None:
                connection.close()
            if owned:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + "-journal").unlink(missing_ok=True)

    @contextmanager
    def _write(self, *, create=False):
        with self._mutex:
            self._safe_path()
            if not self.path.exists() and not create:
                raise EvidenceError("That evidence run does not exist.")
            if create:
                self._initialize()
            self._check_header()
            if self._validated_signature != self._signature():
                self.prepare()
            connection = None
            try:
                connection = self._connection(self.path)
                self._check_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
                self._validated_signature = self._signature()
            except sqlite3.Error as exc:
                raise EvidenceError("Raw evidence could not be committed. Check disk space and folder permissions; capture stopped before updating the library.") from exc
            finally:
                if connection is not None:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()

    @contextmanager
    def _read(self):
        with self._mutex:
            self._safe_path()
            if not self.path.exists():
                self.validate()
                yield None
                return
            self._check_header()
            connection = None
            try:
                connection = self._connection(self.path, readonly=True)
                connection.execute("BEGIN")
                self._check_schema(connection)
                yield connection
            except sqlite3.Error as exc:
                raise EvidenceError("Saved evidence could not be read. Check database integrity before capturing more posts.") from exc
            finally:
                if connection is not None:
                    connection.rollback()
                    connection.close()

    @staticmethod
    def _run(connection: sqlite3.Connection, run_id: str, *, active: bool = False):
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise EvidenceError("That evidence run does not exist.")
        if active and row["status"] != "running":
            raise EvidenceError("That evidence run has already finished. Start a new run to record more observations.")
        return row

    def begin_run(self, metadata: dict) -> str:
        if not isinstance(metadata, dict):
            raise EvidenceError("An evidence run needs its capture metadata.")
        mode = metadata.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise EvidenceError("An evidence run needs its capture mode.")
        channel_id = _channel(metadata.get("channel_id"))
        encoded = _json(metadata)
        run_id, stamp = uuid.uuid4().hex, _now()
        with self._write(create=True) as connection:
            connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                run_id, stamp, None, "running", mode, channel_id, encoded,
                "{}", "{}", "{}", "[]", "{}", stamp))
            connection.execute("INSERT INTO run_events(run_id, occurred_at, event_json) VALUES (?,?,?)", (
                run_id, stamp, _json({"event": "run_started", "metadata": metadata})))
        return run_id

    def append_observation(self, run_id: str, kind: str, subject_id, payload: object,
                           tl_bytes: bytes | None = None, context: dict | None = None,
                           *, observed_at: str | None = None) -> int:
        if not isinstance(kind, str) or not kind.strip() or "\x00" in kind:
            raise EvidenceError("An evidence observation needs a valid kind.")
        if type(subject_id) not in (str, int) or not str(subject_id) or "\x00" in str(subject_id):
            raise EvidenceError("An evidence observation needs a valid subject ID.")
        if kind == "message":
            try:
                if int(subject_id) <= 0 or str(int(subject_id)) != str(subject_id):
                    raise ValueError()
            except (ValueError, TypeError):
                raise EvidenceError("A message observation needs a positive Telegram message ID.") from None
        if tl_bytes is not None and type(tl_bytes) is not bytes:
            raise EvidenceError("Exact Telegram bytes must be supplied as bytes, without conversion.")
        if context is not None and not isinstance(context, dict):
            raise EvidenceError("Observation provenance must be an object.")
        encoded, context_json = _json(payload), _json(context if context is not None else {})
        checksum, stamp = _digest(encoded, tl_bytes), _timestamp(observed_at)
        with self._write() as connection:
            run = self._run(connection, run_id, active=True)
            channel_id = run["channel_id"]
            if kind == "message" and channel_id is None:
                raise EvidenceError("Bind the evidence run to its Telegram channel before saving message observations.")
            connection.execute("""INSERT OR IGNORE INTO observations(kind,channel_id,subject_id,observed_at,payload_json,tl_bytes,payload_sha256)
                VALUES (?,?,?,?,?,?,?)""", (kind, channel_id, str(subject_id), stamp, encoded, tl_bytes, checksum))
            row = connection.execute("""SELECT observation_id,payload_json,tl_bytes FROM observations
                WHERE kind=? AND channel_id IS ? AND subject_id=? AND payload_sha256=?""",
                (kind, channel_id, str(subject_id), checksum)).fetchone()
            if row is None or row["payload_json"] != encoded or row["tl_bytes"] != tl_bytes:
                raise EvidenceError("An evidence checksum collision was detected. Capture stopped without replacing the original observation.")
            parent_channel_id = (context or {}).get("parent_channel_id")
            parent_message_id = (context or {}).get("parent_message_id")
            connection.execute("INSERT INTO occurrences(run_id,observation_id,observed_at,context_json,parent_channel_id,parent_message_id) VALUES (?,?,?,?,?,?)",
                               (run_id, row["observation_id"], stamp, context_json,
                                parent_channel_id if type(parent_channel_id) is int and -(2**63) < parent_channel_id < 2**63 else None,
                                parent_message_id if type(parent_message_id) is int and 0 < parent_message_id < 2**63 else None))
            return row["observation_id"]

    def append_event(self, run_id: str, event: dict) -> int:
        if not isinstance(event, dict):
            raise EvidenceError("Run events must be objects so their provenance is preserved.")
        encoded = _json(event)
        with self._write() as connection:
            self._run(connection, run_id, active=True)
            cursor = connection.execute("INSERT INTO run_events(run_id,occurred_at,event_json) VALUES (?,?,?)", (run_id, _now(), encoded))
            return cursor.lastrowid

    def update_run(self, run_id: str, **fields) -> None:
        if "state" in fields:
            if "status" in fields and fields["status"] != fields["state"]:
                raise EvidenceError("Run state and status disagree.")
            fields["status"] = fields.pop("state")
        allowed = {"status", "counts", "checkpoint", "coverage", "issues", "channel_id", "context"}
        if set(fields) - allowed:
            raise EvidenceError("Some evidence run fields are unsupported. Keep additional details in context.")
        if "status" in fields and fields["status"] != "running":
            raise EvidenceError("Use finish_run to close a capture run with its final coverage.")
        if "channel_id" in fields:
            fields["channel_id"] = _channel(fields["channel_id"])
        for key in ("counts", "checkpoint", "coverage", "context"):
            if key in fields and not isinstance(fields[key], dict):
                raise EvidenceError(f"Evidence run {key} must be an object.")
        if "issues" in fields and not isinstance(fields["issues"], list):
            raise EvidenceError("Evidence run issues must be a list.")
        encoded = _json(fields)
        with self._write() as connection:
            run = self._run(connection, run_id, active=True)
            if "channel_id" in fields and run["channel_id"] is not None and fields["channel_id"] != run["channel_id"]:
                raise EvidenceError("An evidence run cannot switch Telegram channels.")
            updates = {key + "_json" if key in {"counts", "checkpoint", "coverage", "issues", "context"} else key:
                       _json(value) if key in {"counts", "checkpoint", "coverage", "issues", "context"} else value for key, value in fields.items()}
            updates["updated_at"] = _now()
            connection.execute("UPDATE runs SET " + ",".join(key + "=?" for key in updates) + " WHERE run_id=?", (*updates.values(), run_id))
            connection.execute("INSERT INTO run_events(run_id,occurred_at,event_json) VALUES (?,?,?)",
                               (run_id, updates["updated_at"], _json({"event": "run_updated", "fields": json.loads(encoded)})))

    def finish_run(self, run_id: str, status: str = "completed", *, coverage: dict | None = None,
                   counts: dict | None = None, checkpoint: dict | None = None,
                   issues: list | None = None, context: dict | None = None) -> None:
        if status not in _TERMINAL:
            raise EvidenceError("Choose a final evidence run status: completed, warning, cancelled, error, interrupted, failed, partial, or stopped.")
        supplied = {key: value for key, value in {"coverage": coverage, "counts": counts, "checkpoint": checkpoint,
                                                 "issues": issues, "context": context}.items() if value is not None}
        for key, value in supplied.items():
            if not isinstance(value, list if key == "issues" else dict):
                raise EvidenceError(f"Evidence run {key} has an unsupported format.")
        encoded = _json(supplied)
        stamp = _now()
        with self._write() as connection:
            self._run(connection, run_id, active=True)
            updates = {key + "_json": _json(value) for key, value in supplied.items()}
            updates.update(status=status, ended_at=stamp, updated_at=stamp)
            connection.execute("UPDATE runs SET " + ",".join(key + "=?" for key in updates) + " WHERE run_id=?", (*updates.values(), run_id))
            connection.execute("INSERT INTO run_events(run_id,occurred_at,event_json) VALUES (?,?,?)",
                               (run_id, stamp, _json({"event": "run_finished", "status": status, "fields": json.loads(encoded)})))

    @staticmethod
    def _observation(row, occurrences: list | None = None) -> dict:
        result = {"observation_id": row["observation_id"], "kind": row["kind"], "channel_id": row["channel_id"],
                  "subject_id": int(row["subject_id"]) if row["kind"] == "message" else row["subject_id"],
                  "observed_at": row["observed_at"], "payload": json.loads(row["payload_json"]),
                  "payload_sha256": row["payload_sha256"],
                  "tl_bytes": {"encoding": "base64", "data": base64.b64encode(row["tl_bytes"]).decode("ascii")} if row["tl_bytes"] is not None else None}
        if row["kind"] == "message":
            result["message_id"] = int(row["subject_id"])
        if occurrences is not None:
            result["occurrences"] = occurrences
        return result

    @staticmethod
    def _occurrence(row) -> dict:
        return {"occurrence_id": row["occurrence_id"], "run_id": row["run_id"], "observation_id": row["observation_id"],
                "observed_at": row["observed_at"], "context": json.loads(row["context_json"])}

    @staticmethod
    def _run_summary(connection, row, *, include_ids=False) -> dict:
        result = {key: row[key] for key in ("run_id", "started_at", "ended_at", "status", "mode", "channel_id", "updated_at")}
        result["state"] = row["status"]
        for key in ("metadata", "counts", "checkpoint", "coverage", "issues", "context"):
            result[key] = json.loads(row[key + "_json"])
        result["observed_messages"] = connection.execute("""SELECT COUNT(DISTINCT o.subject_id) FROM observations o JOIN occurrences c USING(observation_id)
            WHERE c.run_id=? AND o.kind='message'""", (row["run_id"],)).fetchone()[0]
        if include_ids:
            seen = connection.execute("""SELECT DISTINCT o.subject_id FROM observations o JOIN occurrences c USING(observation_id)
                WHERE c.run_id=? AND o.kind='message' ORDER BY CAST(o.subject_id AS INTEGER)""", (row["run_id"],))
            result["message_ids"] = [int(item[0]) for item in seen]
        result["observations"] = connection.execute("SELECT COUNT(DISTINCT observation_id) FROM occurrences WHERE run_id=?", (row["run_id"],)).fetchone()[0]
        result["occurrences"] = connection.execute("SELECT COUNT(*) FROM occurrences WHERE run_id=?", (row["run_id"],)).fetchone()[0]
        return result

    def message_observations(self, channel_id: int, message_id: int) -> list[dict]:
        channel_id = _channel(channel_id)
        if type(message_id) is not int or message_id <= 0:
            raise EvidenceError("Choose a positive Telegram message ID.")
        with self._read() as connection:
            if connection is None:
                return []
            result = []
            for row in connection.execute("SELECT * FROM observations WHERE kind='message' AND channel_id IS ? AND subject_id=? ORDER BY observation_id", (channel_id, str(message_id))):
                occurrences = [self._occurrence(item) for item in connection.execute("SELECT * FROM occurrences WHERE observation_id=? ORDER BY occurrence_id", (row["observation_id"],))]
                result.append(self._observation(row, occurrences))
            return result

    def runs(self, limit: int = 100) -> list[dict]:
        if type(limit) is not int or limit < 1:
            raise EvidenceError("Choose a positive number of evidence runs to read.")
        with self._read() as connection:
            if connection is None:
                return []
            return [self._run_summary(connection, row) for row in connection.execute("SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,))]

    def run(self, run_id: str) -> dict:
        with self._read() as connection:
            if connection is None:
                raise EvidenceError("That evidence run does not exist.")
            return self._run_summary(connection, self._run(connection, run_id))

    def stats(self) -> dict:
        with self._read() as connection:
            if connection is None:
                return {"observations": 0, "message_observations": 0, "runs": 0, "occurrences": 0, "last_run": None}
            latest = connection.execute("SELECT * FROM runs ORDER BY started_at DESC,run_id DESC LIMIT 1").fetchone()
            return {"observations": connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                    "message_observations": connection.execute("SELECT COUNT(*) FROM observations WHERE kind='message'").fetchone()[0],
                    "runs": connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                    "occurrences": connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0],
                    "last_run": self._run_summary(connection, latest) if latest is not None else None}

    def related_observations(self, channel_id: int, message_id: int, limit: int = 200, offset: int = 0) -> list[dict]:
        channel_id = _channel(channel_id)
        if type(message_id) is not int or message_id <= 0 or type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise EvidenceError("Choose a valid message and evidence page size.")
        with self._read() as connection:
            if connection is None:
                return []
            rows = connection.execute("""SELECT * FROM observations WHERE observation_id IN (
                SELECT observation_id FROM observations WHERE kind='message' AND channel_id IS ? AND subject_id=?
                UNION SELECT observation_id FROM occurrences WHERE parent_channel_id=? AND parent_message_id=?
            ) ORDER BY observation_id LIMIT ? OFFSET ?""", (channel_id, str(message_id), channel_id, message_id, limit, offset))
            return [self._observation(row, [self._occurrence(item) for item in connection.execute(
                "SELECT * FROM occurrences WHERE observation_id=? ORDER BY occurrence_id", (row["observation_id"],))]) for row in rows]

    def post_preview(self, channel_id: int, message_id: int, limit: int = 20,
                     related_limit: int = 100, occurrence_limit: int = 10) -> dict:
        """Return recent source samples with SQL limits at both nesting levels.

        Complete retained history remains available through ``write_export``.
        Related rows exclude the primary message observations already returned
        in ``observations``. Counts always describe the full retained set.
        """
        channel_id = _channel(channel_id)
        if type(message_id) is not int or message_id <= 0 or any(type(value) is not int or value < 1 for value in (limit, related_limit, occurrence_limit)):
            raise EvidenceError("Choose a valid post and positive source preview limits.")
        result = {"observations": [], "related_observations": [],
                  "preview_limits": {"observations": limit, "related_observations": related_limit, "occurrences_per_observation": occurrence_limit},
                  "total_message_observations": 0, "total_related_observations": 0}
        with self._read() as connection:
            if connection is None:
                return result
            primary = "kind='message' AND channel_id IS ? AND subject_id=?"
            primary_args = (channel_id, str(message_id))
            related = "observation_id IN (SELECT observation_id FROM occurrences WHERE parent_channel_id=? AND parent_message_id=?) AND NOT (" + primary + ")"
            related_args = (channel_id, message_id, *primary_args)

            def select(where, args, row_limit):
                output = []
                for row in connection.execute("SELECT * FROM observations WHERE " + where + " ORDER BY observation_id DESC LIMIT ?", (*args, row_limit)):
                    occurrences = [self._occurrence(item) for item in connection.execute(
                        "SELECT * FROM occurrences WHERE observation_id=? ORDER BY occurrence_id DESC LIMIT ?", (row["observation_id"], occurrence_limit))]
                    value = self._observation(row, occurrences)
                    value["occurrence_count"] = connection.execute("SELECT COUNT(*) FROM occurrences WHERE observation_id=?", (row["observation_id"],)).fetchone()[0]
                    value["occurrences_truncated"] = value["occurrence_count"] > len(occurrences)
                    output.append(value)
                return output

            result["total_message_observations"] = connection.execute("SELECT COUNT(*) FROM observations WHERE " + primary, primary_args).fetchone()[0]
            result["total_related_observations"] = connection.execute("SELECT COUNT(*) FROM observations WHERE " + related, related_args).fetchone()[0]
            result["observations"] = select(primary, primary_args, limit)
            result["related_observations"] = select(related, related_args, related_limit)
            return result

    def export(self, run_id: str | None = None) -> dict:
        with self._read() as connection:
            result = {"schema_version": SCHEMA_VERSION, "format": "telegram-archive-evidence", "runs": [], "observations": [], "occurrences": [], "events": []}
            if connection is None:
                return result
            if run_id is None:
                runs = connection.execute("SELECT * FROM runs ORDER BY started_at,run_id").fetchall()
                observations = connection.execute("SELECT * FROM observations ORDER BY observation_id")
                occurrences = connection.execute("SELECT * FROM occurrences ORDER BY occurrence_id")
                events = connection.execute("SELECT * FROM run_events ORDER BY event_id")
            else:
                runs = [self._run(connection, run_id)]
                observations = connection.execute("SELECT DISTINCT o.* FROM observations o JOIN occurrences c USING(observation_id) WHERE c.run_id=? ORDER BY o.observation_id", (run_id,))
                occurrences = connection.execute("SELECT * FROM occurrences WHERE run_id=? ORDER BY occurrence_id", (run_id,))
                events = connection.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY event_id", (run_id,))
            result["runs"] = [self._run_summary(connection, row, include_ids=True) for row in runs]
            result["observations"] = [self._observation(row) for row in observations]
            result["occurrences"] = [self._occurrence(row) for row in occurrences]
            result["events"] = [{"event_id": row["event_id"], "run_id": row["run_id"], "occurred_at": row["occurred_at"], "event": json.loads(row["event_json"])} for row in events]
            return result

    def write_export(self, filelike, run_id: str | None = None) -> None:
        """Stream JSON from a private SQLite backup without blocking capture on a slow reader."""
        with tempfile.TemporaryDirectory(prefix="telegram-evidence-export-") as directory:
            snapshot = Path(directory) / "evidence.sqlite3"
            destination = source = None
            with self._mutex:
                self._safe_path()
                if not self.path.exists():
                    self.validate()
                    value = self.export(run_id)
                    encoded = _json(value)
                    try:
                        filelike.write(encoded.encode("utf-8"))
                    except TypeError:
                        filelike.write(encoded)
                    return
                self._check_header()
                try:
                    descriptor = os.open(snapshot, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(descriptor)
                    source = self._connection(self.path, readonly=True)
                    self._check_schema(source)
                    destination = self._connection(snapshot)
                    busy_since = None

                    def progress(status, remaining, total):
                        nonlocal busy_since
                        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            busy_since = time.monotonic() if busy_since is None else busy_since
                            if time.monotonic() - busy_since > 5:
                                raise EvidenceError("The evidence database is busy in another operation. Retry the source export after it finishes.")
                        else:
                            busy_since = None

                    source.backup(destination, pages=256, sleep=0.05, progress=progress)
                except (sqlite3.Error, OSError) as exc:
                    raise EvidenceError("The source export could not be prepared. Check temporary disk space and database access.") from exc
                finally:
                    if source is not None:
                        source.close()
                    if destination is not None:
                        destination.close()
            connection = self._connection(snapshot, readonly=True)
            try:
                self._check_schema(connection)
                if run_id is not None:
                    self._run(connection, run_id)
                binary = None

                def emit(value):
                    nonlocal binary
                    if binary is None:
                        try:
                            filelike.write(value.encode("utf-8"))
                            binary = True
                        except TypeError:
                            filelike.write(value)
                            binary = False
                    else:
                        filelike.write(value.encode("utf-8") if binary else value)

                emit('{"schema_version":1,"format":"telegram-archive-evidence"')
                parameters = () if run_id is None else (run_id,)
                run_sql = "SELECT * FROM runs" + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY started_at,run_id"
                queries = (
                    ("runs", run_sql, lambda row: self._run_summary(connection, row, include_ids=True)),
                    ("observations", "SELECT * FROM observations ORDER BY observation_id" if run_id is None else
                     "SELECT DISTINCT o.* FROM observations o JOIN occurrences c USING(observation_id) WHERE c.run_id=? ORDER BY o.observation_id", self._observation),
                    ("occurrences", "SELECT * FROM occurrences" + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY occurrence_id", self._occurrence),
                    ("events", "SELECT * FROM run_events" + (" WHERE run_id=?" if run_id is not None else "") + " ORDER BY event_id",
                     lambda row: {"event_id": row["event_id"], "run_id": row["run_id"], "occurred_at": row["occurred_at"], "event": json.loads(row["event_json"])}),
                )
                for name, sql, serialize in queries:
                    emit(',"' + name + '":[')
                    first = True
                    for row in connection.execute(sql, parameters):
                        emit(("" if first else ",") + _json(serialize(row)))
                        first = False
                    emit("]")
                emit("}")
            finally:
                connection.close()
