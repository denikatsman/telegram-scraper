"""Loopback-only HTTP application. No hosted service, CDN, or telemetry."""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .storage import Store, StoreError
from .evidence import EvidenceStore, EvidenceError


def date_bounds(start="", end=""):
    try:
        lower = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc) if start else None
        upper = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1) if end else None
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Use valid dates in YYYY-MM-DD format.") from exc
    if lower and upper and lower >= upper:
        raise ValueError("The end date must be on or after the start date.")
    return lower, upper


def media_kind(record):
    existing = record.get("media_kind")
    if existing in {"video", "photo", "text"}:
        return existing
    filename = str(record.get("media_file") or record.get("media_name") or "")
    mime = record.get("media_mime") or mimetypes.guess_type(filename)[0] or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "photo"
    return "file" if filename or record.get("has_media") else "text"


class Runtime:
    """One event loop owns the Telethon session and all mutations."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.settings = Settings(self.root)
        self.store = Store(self.root)
        self.evidence = EvidenceStore(self.root)
        self.library_error = None
        self.evidence_error = None
        try:
            self.store.load()
        except StoreError as exc:
            self.library_error = str(exc)
        try:
            self.evidence.validate()
        except EvidenceError as exc:
            self.evidence_error = str(exc)
        self.connection = {"authorized": False, "step": "disconnected"}
        self.job = {"running": False, "status": "idle", "phase": "idle", "message": "Ready when you are."}
        self.service = None
        self.task = None
        self.auth_busy = False
        self.verify_cancel = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._loop_main, name="telegram-worker", daemon=True)
        self.thread.start()

    def _loop_main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout=45):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise ValueError("Telegram is taking too long to respond. Check your connection and try again.") from exc

    def report(self, update):
        if threading.current_thread() is not self.thread:
            self.loop.call_soon_threadsafe(self.report, update)
            return
        if isinstance(update, str):
            update = {"message": update}
        self.job.update(update)

    def get_service(self):
        if self.service is None:
            from .engine import TelegramService
            self.service = TelegramService(self.root, dict(self.settings.values), self.store, self.report)
        return self.service

    async def state(self):
        records = list(self.store.records.values())
        dates = sorted(r.get("date") for r in records if isinstance(r.get("date"), str) and r["date"])
        try:
            channel = self.store.channel_info() or {}
        except StoreError as exc:
            channel = {}
            self.library_error = str(exc)
        health = await self.health(records)
        return {
            "settings": self.settings.public(),
            "connection": dict(self.connection),
            "job": dict(self.job),
            "health": health,
            "library": {
                "total": len(records),
                "media": sum(media_kind(r) != "text" for r in records),
                "videos": sum(media_kind(r) == "video" for r in records),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "channel_title": channel.get("title") or "Your channel archive",
                "error": self.library_error,
            },
        }

    async def health(self, records=None):
        records = records if records is not None else list(self.store.records.values())
        raw_count = sum(isinstance(r.get("raw"), dict) for r in records)
        media_health = await asyncio.to_thread(self._media_health, records)
        try:
            runs = await asyncio.to_thread(self.evidence.runs, limit=20)
            stats = await asyncio.to_thread(self.evidence.stats)
            if self.evidence_error:
                # Summary reads do not verify payload digests. Only a complete
                # validation may clear an earlier corruption/recovery failure.
                await asyncio.to_thread(self.evidence.validate)
        except EvidenceError as exc:
            self.evidence_error = str(exc)
            runs, stats = [], {}
        else:
            self.evidence_error = None
        for run in runs:
            active = self.job.get("running") and self.job.get("run_id") == run.get("id", run.get("run_id"))
            status = run.get("status", run.get("state", "unknown"))
            run["display_status"] = "interrupted" if status in {"running", "starting"} and not active else status
        runs = [self.compact_run(run) for run in runs]
        stats = {k: v for k, v in stats.items() if k != "last_run"}
        return {
            "posts_with_raw_metadata": raw_count, "legacy_posts": len(records) - raw_count,
            **media_health, "evidence": stats, "runs": runs,
            "error": self.evidence_error,
            "coverage_note": "A completed scan records what Telegram returned within its saved scope. Deleted, hidden, or inaccessible history cannot be recovered by the scraper.",
        }

    def _media_health(self, records):
        # Folder reads can be slow on external drives. Never block Telethon's
        # event loop (or the Stop action) on a library-wide filesystem scan.
        available, referenced, checksums, missing = 0, 0, 0, 0
        for record in records:
            if not record.get("media_file") and not record.get("has_media"):
                continue
            referenced += 1
            path = self.store.media_path(record)
            try:
                size = path.stat().st_size if path and path.is_file() else 0
                expected = record.get("media_size")
                valid = size > 0 and (expected is None or size == expected)
            except OSError:
                valid = False
            available += int(valid)
            missing += int(not valid)
            checksums += int(bool(record.get("media_sha256")))
        return {
            "media_referenced": referenced, "media_available": available, "media_missing": missing,
            "media_with_recorded_checksums": checksums,
        }

    @staticmethod
    def compact_run(run):
        def compact(value):
            if isinstance(value, dict):
                return {k: compact(v) for k, v in value.items() if k not in {"message_ids", "seen_ids", "seen_message_ids", "raw"}}
            if isinstance(value, list):
                if len(value) > 30:
                    return {"count": len(value), "preview": [compact(v) for v in value[:5]], "full_details": "Download the scrape report."}
                return [compact(v) for v in value]
            if type(value) is int and abs(value) > 9007199254740991:
                return str(value)
            return value
        fields = ("run_id", "id", "status", "state", "display_status", "counts", "checkpoint", "coverage", "issues", "started_at", "ended_at", "observed_messages")
        result = compact({k: run[k] for k in fields if k in run})
        result["metadata"] = compact({k: v for k, v in run.get("metadata", {}).items() if k in {"mode", "start", "end", "channel_id", "app_version", "telethon_version", "telegram_api_layer"}})
        return result

    def recover_evidence(self):
        """Called by the CLI only after acquiring the exclusive library lock."""
        try:
            self.evidence.prepare()
            self.evidence_error = None
        except EvidenceError as exc:
            self.evidence_error = str(exc)

    def post_view(self, record, detail=False):
        path = self.store.media_path(record)
        available = path is not None and path.is_file() and path.stat().st_size > 0
        result = {key: record.get(key) for key in ("id", "date", "text", "type", "action", "action_title", "edit_date", "media_status", "media_error")}
        result.update({
            "media_kind": media_kind(record),
            "media_url": f"/api/media/{record['id']}" if available else None,
            "media_name": path.name if path else record.get("media_name"),
            "media_missing": bool(record.get("media_file") or record.get("has_media")) and not available,
            "media_size": path.stat().st_size if available else record.get("media_size"),
            "revision_count": len(record.get("revisions", [])),
        })
        if detail:
            result["revisions"] = [{k: r.get(k) for k in ("text", "date", "edit_date", "archived_at")} for r in record.get("revisions", []) if isinstance(r, dict)]
            result["source"] = {
                "has_raw": isinstance(record.get("raw"), dict),
                "first_observed_at": record.get("first_observed_at"),
                "last_observed_at": record.get("last_observed_at"),
                "channel_id": record.get("source_channel_id"),
                "media_sha256": record.get("media_sha256"),
                "context_capture": record.get("context_capture"),
            }
            result["structured_content"] = self.structured_content(record)
        return result

    @staticmethod
    def structured_content(record):
        raw = record.get("raw") or {}
        if not isinstance(raw, dict):
            return {}
        media = raw.get("media") or {}
        poll = media.get("poll") if isinstance(media, dict) else None
        return {"poll": poll, "poll_results": media.get("results") if poll else None,
                "author_signature": raw.get("post_author"), "forward": raw.get("fwd_from"),
                "reactions": raw.get("reactions"), "reply": raw.get("reply_to"),
                "views": raw.get("views"), "forwards": raw.get("forwards"),
                "album_id": str(raw["grouped_id"]) if raw.get("grouped_id") is not None else None}

    async def source_post(self, message_id):
        record = self.store.records.get(message_id)
        if record is None:
            raise KeyError("That post is not in your archive.")
        channel = self.store.channel_info() or {}
        channel_id = record.get("source_channel_id") or channel.get("id")
        preview = await asyncio.to_thread(self.evidence.post_preview, channel_id, message_id) if channel_id else {"observations": [], "related_observations": []}
        return {"record": record, **preview, "complete_export_url": "/api/evidence/export",
                "note": "This is a bounded source preview, not a complete per-post export or a reconstruction of data Telegram did not return. The complete source export contains every saved observation and occurrence; Export posts contains the full reading index."}

    async def posts(self, params):
        if self.library_error:
            raise ValueError(self.library_error)
        q = params.get("q", "").casefold().strip()
        kind = params.get("kind", "all")
        if kind not in {"all", "video", "photo", "file", "text"}:
            raise ValueError("Choose a supported media filter.")
        lower, upper = date_bounds(params.get("start", ""), params.get("end", ""))
        try:
            offset = max(0, int(params.get("offset", 0)))
            limit = max(1, min(100, int(params.get("limit", 40))))
        except (TypeError, ValueError) as exc:
            raise ValueError("Page and page size must be numbers.") from exc
        matches = []
        for record in self.store.records.values():
            if q and q not in str(record.get("text", "")).casefold() and q not in str(record["id"]):
                continue
            if kind != "all" and media_kind(record) != kind:
                continue
            if lower or upper:
                try:
                    stamp = datetime.fromisoformat((record.get("date") or "").replace("Z", "+00:00"))
                    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                except (ValueError, AttributeError):
                    continue
                if lower and stamp < lower or upper and stamp >= upper:
                    continue
            matches.append(record)
        matches.sort(key=lambda r: r["id"], reverse=True)
        return {"posts": [self.post_view(r) for r in matches[offset:offset+limit]], "total": len(matches), "offset": offset, "limit": limit}

    async def post(self, message_id):
        record = self.store.records.get(message_id)
        if record is None:
            raise KeyError("That post is not in your archive.")
        return self.post_view(record, detail=True)

    async def media(self, message_id):
        record = self.store.records.get(message_id)
        path = self.store.media_path(record) if record else None
        if path is None or not path.is_file() or path.stat().st_size == 0:
            raise KeyError("This media file is missing. Run a sync with media downloads enabled to retry it.")
        return path

    async def variant(self, message_id, index):
        record = self.store.records.get(message_id)
        variants = (record or {}).get("context_capture", {}).get("media_variants", [])
        if index < 0 or index >= len(variants):
            raise KeyError("That media variant is not in the saved source record.")
        path = self.store.media_path({"media_file": variants[index].get("media_file")})
        if path is None or not path.is_file():
            raise KeyError("This media variant is not saved locally. Sync the post again to retry accessible files.")
        mime = variants[index].get("mime_type")
        extension = mimetypes.guess_extension(mime) if isinstance(mime, str) else None
        if not extension or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension):
            extension = ".bin"
        return path, f"post-{message_id}-variant-{index + 1}{extension}"

    async def export(self):
        # Export must obey the same boundary as library reads, including when
        # a file was replaced by a symlink after the application started.
        self.store._read_records()
        return self.store.messages_file

    async def update_settings(self, incoming):
        if self.job["running"] or self.auth_busy:
            raise ValueError("Wait for the current operation to finish before changing settings.")
        # A configured legacy library has one known source. Never silently rebind it.
        if self.store.records and "channel" in incoming:
            from .config import normalize_channel
            if not isinstance(incoming["channel"], str):
                raise ValueError("Enter a channel username or link.")
            if normalize_channel(incoming["channel"]) != normalize_channel(self.settings.values.get("channel", "")):
                raise ValueError("This library already contains a channel. To archive another channel, launch with --data-dir pointing to a new folder.")
        old_identity = tuple(self.settings.values.get(k) for k in ("api_id", "api_hash", "channel"))
        result = self.settings.update(incoming)
        new_identity = tuple(self.settings.values.get(k) for k in ("api_id", "api_hash", "channel"))
        if old_identity != new_identity:
            if self.service:
                await self.service.close()
                self.service = None
            self.connection = {"authorized": False, "step": "disconnected"}
        elif self.service:
            self.service.settings = dict(self.settings.values)
        return {"settings": result}

    async def authenticate(self, action, payload):
        if self.job["running"] or self.auth_busy:
            raise ValueError("Another operation is running. Wait for it to finish first.")
        self.auth_busy = True
        try:
            service = self.get_service()
            if action == "connect":
                result = await service.status()
                result["step"] = "connected" if result.get("authorized") else "phone"
            elif action == "code":
                result = await service.send_code(str(payload.get("phone", "")))
            else:
                result = await service.sign_in(code=str(payload.get("code", "")), password=str(payload.get("password", "")))
            self.connection.update(result)
            self.connection["authorized"] = result.get("step") == "connected"
            return result
        except Exception as exc:
            from .engine import AuthRequired
            if isinstance(exc, AuthRequired):
                self.connection = {"authorized": False, "step": "phone"}
            raise
        finally:
            self.auth_busy = False

    async def start_job(self, payload):
        if self.job["running"] or self.auth_busy:
            raise ValueError("An operation is already running. Stop it or wait for it to finish.")
        mode = payload.get("mode")
        if mode not in {"sync", "range", "watch", "verify"}:
            raise ValueError("Choose sync, a date range, live capture, or an archive check.")
        if self.library_error and mode != "verify":
            raise ValueError(self.library_error)
        if self.evidence_error and mode != "verify":
            raise ValueError(self.evidence_error)
        start, end = payload.get("start", ""), payload.get("end", "")
        if mode == "range":
            if not start:
                raise ValueError("Choose the first day to archive.")
            end = end or start
            date_bounds(start, end)
        if mode != "verify":
            if not self.connection.get("authorized"):
                raise ValueError("Connect your Telegram account in Settings before starting a sync.")
            if not self.settings.values.get("channel"):
                raise ValueError("Add a channel in Settings first.")
        self.job = {"running": True, "status": "running", "phase": "starting", "mode": mode,
                    "message": "Starting archive check…" if mode == "verify" else "Preparing your archive…",
                    "processed": 0, "added": 0, "updated": 0, "media_downloaded": 0, "media_failed": 0}
        self.verify_cancel.clear()
        self.task = asyncio.create_task(self._run_job(mode, start, end))
        return {"job": dict(self.job)}

    async def _run_job(self, mode, start, end):
        try:
            if self.verify_cancel.is_set():
                self.job.update({"status": "cancelled", "phase": "finished", "message": "Stopped before starting. Your archive is unchanged."})
                return
            if mode == "verify":
                result = await asyncio.to_thread(self.store.verify, cancel=self.verify_cancel.is_set, report=self.report)
                cancelled = result.get("cancelled", False)
                self.job.update({"result": result, "status": "cancelled" if cancelled else "completed" if result["ok"] else "warning", "phase": "finished",
                                 "message": "Archive check stopped." if cancelled else "Archive check passed." if result["ok"] else "Archive check finished. Some items need attention."})
            else:
                await self.get_service().run(mode, start=start, end=end)
        except asyncio.CancelledError:
            self.job.update({"status": "cancelled", "message": "Stopped. Saved posts are kept."})
        except Exception as exc:
            from .engine import UserError, AuthRequired
            if isinstance(exc, AuthRequired):
                self.connection = {"authorized": False, "step": "phone"}
            message = str(exc) if isinstance(exc, (UserError, StoreError, ValueError)) else "The operation could not finish. Your saved archive is kept. Check your connection and available disk space, then retry."
            self.job.update({"status": "error", "phase": "finished", "message": message})
        finally:
            self.job["running"] = False

    async def stop_job(self):
        if self.job["running"]:
            self.verify_cancel.set()
            if self.service:
                self.service.cancel()
            self.job.update({"phase": "stopping", "message": "Stopping safely. Keeping completed downloads and saved posts…"})
        return {"job": dict(self.job)}

    async def _close(self):
        await self.stop_job()
        if self.task and not self.task.done():
            # A timeout must never detach a disk worker and release the library
            # lock while it is still publishing a checkpoint or backup.
            await asyncio.gather(self.task, return_exceptions=True)
        if self.service:
            try:
                await asyncio.wait_for(self.service.close(), timeout=15)
            except (Exception, asyncio.CancelledError):
                pass
        await self.loop.shutdown_default_executor()

    def close(self):
        try:
            self.call(self._close(), timeout=None)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if not self.thread.is_alive():
                self.loop.close()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime):
        if address[0] != "127.0.0.1":
            raise ValueError("The archive interface can only listen on 127.0.0.1.")
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)
        self.cookie_name = f"archive_session_{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ChannelArchive"

    def log_message(self, format, *args):
        pass

    @property
    def app(self):
        return self.server.runtime

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def trusted_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def authorized(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookies.get(self.server.cookie_name)
            return bool(value and secrets.compare_digest(value.value, self.server.token))
        except Exception:
            return False

    def json_response(self, data, status=200, *, indent=None):
        body = json.dumps(data, ensure_ascii=False, indent=indent).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, message, status=400):
        self.json_response({"ok": False, "error": message}, status)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if not self.trusted_host():
                self.error("Open the app using its local address.", 403)
                return
            path = urlparse(self.path).path
            if path in {"/", "/index.html", "/app.css", "/app.js"}:
                filename = "index.html" if path in {"/", "/index.html"} else path[1:]
                file = Path(__file__).parent / "static" / filename
                self.send_file(file, set_cookie=filename == "index.html")
                return
            if not self.authorized():
                self.error("Reload the app to reconnect to this local session.", 403)
                return
            if path == "/api/state":
                self.json_response(self.app.call(self.app.state()) | {"csrf_token": self.server.token})
            elif path == "/api/instance":
                import os
                from . import __version__
                self.json_response({"app": "channel-archive", "root": str(self.app.root), "pid": os.getpid(), "version": __version__})
            elif path == "/api/posts":
                params = {key: value[-1] for key, value in parse_qs(urlparse(self.path).query).items()}
                self.json_response(self.app.call(self.app.posts(params)))
            elif re.fullmatch(r"/api/posts/\d+", path):
                self.json_response(self.app.call(self.app.post(int(path.rsplit("/", 1)[1]))))
            elif re.fullmatch(r"/api/posts/\d+/source", path):
                self.json_response(self.app.call(self.app.source_post(int(path.split("/")[3]))), indent=2)
            elif re.fullmatch(r"/api/posts/\d+/variants/\d+", path):
                parts = path.split("/")
                file, filename = self.app.call(self.app.variant(int(parts[3]), int(parts[5])))
                self.send_file(file, download=filename)
            elif path == "/api/health":
                self.json_response(self.app.call(self.app.health()))
            elif path == "/api/evidence/export":
                params = parse_qs(urlparse(self.path).query)
                run_id = params.get("run", [None])[-1]
                self.send_evidence(run_id)
            elif re.fullmatch(r"/api/media/\d+", path):
                self.send_file(self.app.call(self.app.media(int(path.rsplit("/", 1)[1]))), media=True)
            elif path == "/api/export":
                self.send_file(self.app.call(self.app.export()), download="messages_all.json")
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.error("That page was not found.", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except KeyError as exc:
            self.error(str(exc.args[0]), 404)
        except (ValueError, StoreError) as exc:
            self.error(str(exc))
        except Exception:
            self.error("The archive could not be read. Check the data folder and try again.", 500)

    def do_POST(self):
        try:
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if not self.trusted_host() or not self.authorized() or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.token) or origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self.error("Reload the app before trying that action again.", 403)
                return
            if self.headers.get_content_type() != "application/json":
                self.error("Send this action as JSON.", 415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > 16384:
                self.error("This request is too large or incomplete.", 413)
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                self.error("The request could not be read. Reload the page and try again.")
                return
            if not isinstance(payload, dict):
                self.error("The request must contain a settings or action object.")
                return
            path = urlparse(self.path).path
            if path == "/api/settings":
                result = self.app.call(self.app.update_settings(payload))
            elif path in {"/api/connect", "/api/login/code", "/api/login/verify"}:
                result = self.app.call(self.app.authenticate(path.rsplit("/", 1)[1], payload))
            elif path == "/api/jobs":
                result = self.app.call(self.app.start_job(payload))
            elif path == "/api/jobs/stop":
                result = self.app.call(self.app.stop_job())
            else:
                self.error("That action was not found.", 404)
                return
            self.json_response({"ok": True, **result})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, StoreError) as exc:
            self.error(str(exc))
        except Exception:
            self.error("The action could not finish. Check your connection and try again.", 500)

    def send_evidence(self, run_id=None):
        import tempfile
        # Stream SQLite rows to a temporary export before sending any headers.
        # The writer owns a consistent read transaction; export does not mutate
        # the archive, and errors cannot masquerade as a successful download.
        with tempfile.TemporaryDirectory(prefix="channel-source-export-") as directory:
            path = Path(directory) / ("scrape-report.json" if run_id else "source-observations.json")
            with path.open("w", encoding="utf-8") as target:
                self.app.evidence.write_export(target, run_id=run_id)
            self.send_file(path, download=path.name)

    def send_file(self, path, *, set_cookie=False, media=False, download=None):
        try:
            file = path.open("rb")
        except OSError:
            self.error("This file is not available in the archive.", 404)
            return
        with file:
            import os
            size = os.fstat(file.fileno()).st_size
            start, end, code = 0, size - 1, 200
            requested = self.headers.get("Range") if media else None
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                valid = bool(match and (match[1] or match[2]))
                if valid:
                    if match[1]:
                        start = int(match[1])
                        end = min(int(match[2]), size - 1) if match[2] else size - 1
                    else:
                        start = max(0, size - int(match[2]))
                    valid = 0 <= start <= end < size
                if not valid:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            if set_cookie:
                self.send_header("Set-Cookie", f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
            if media:
                self.send_header("Accept-Ranges", "bytes")
                if mime in {"text/html", "image/svg+xml", "application/xhtml+xml"}:
                    download = path.name
            if download:
                from urllib.parse import quote
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command != "HEAD":
                file.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
