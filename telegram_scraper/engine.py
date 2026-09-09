"""Cancellable Telegram ingestion. The caller owns the Store's writer lock.

No Telegram connection is made on import or construction. All network activity
uses the same asyncio loop; disk checkpoints and snapshots run off that loop.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import math
import mimetypes
import os
import re
import tempfile
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .storage import StoreError


class UserError(ValueError):
    """A problem whose message is safe to show in the app."""


class AuthRequired(UserError):
    """The UI must clear its connected state and show account connection."""


class RawCaptureError(UserError):
    """A required raw source could not be preserved completely."""


_AUTH_ERRORS = {"AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError"}


def _user_error(exc: BaseException) -> UserError:
    if isinstance(exc, UserError):
        return exc
    cls = AuthRequired if type(exc).__name__ in _AUTH_ERRORS else UserError
    return cls(friendly_error(exc))


class _Cancelled(Exception):
    pass


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, UserError):
        return str(exc)
    name = type(exc).__name__
    messages = {
        "ApiIdInvalidError": "Telegram rejected the API ID or API hash. Check both in Settings.",
        "ApiIdPublishedFloodError": "Telegram has restricted these API credentials. Use your own credentials from my.telegram.org.",
        "PhoneNumberInvalidError": "Enter a valid phone number including its country code, for example +41….",
        "PhoneNumberBannedError": "Telegram has restricted this phone number. Contact Telegram support.",
        "PhoneCodeInvalidError": "That code is incorrect. Check the latest code from Telegram and try again.",
        "PhoneCodeEmptyError": "Enter the login code Telegram sent you.",
        "PhoneCodeExpiredError": "That code has expired. Request a new code and try again.",
        "PasswordHashInvalidError": "That two-step verification password is incorrect. Try again.",
        "SessionPasswordNeededError": "Enter your Telegram two-step verification password.",
        "AuthKeyUnregisteredError": "Your Telegram session has expired. Connect your account again.",
        "SessionRevokedError": "This Telegram session was revoked. Connect your account again.",
        "SessionExpiredError": "Your Telegram session has expired. Connect your account again.",
        "AuthKeyDuplicatedError": "Telegram invalidated this session because it was used elsewhere. Reconnect your account.",
        "ChannelPrivateError": "This account cannot access that channel. Check the channel and your membership in Telegram.",
        "ChannelInvalidError": "Telegram could not find that channel. Check the channel in Settings.",
        "UsernameInvalidError": "That channel username is invalid. Enter its @username, t.me link, or numeric ID.",
        "UsernameNotOccupiedError": "No Telegram channel has that username. Check the channel in Settings.",
        "InviteHashExpiredError": "That invite link has expired. Use the channel's current link or numeric ID.",
        "InviteHashInvalidError": "That Telegram invite link is invalid. Check the link in Settings.",
        "ChatAdminRequiredError": "Telegram does not allow this account to read that channel's history.",
        "FileReferenceExpiredError": "Telegram's download reference expired. Run sync again to refresh it.",
        "FileReferenceInvalidError": "Telegram's download reference is no longer valid. Run sync again.",
        "MediaEmptyError": "Telegram no longer has this attachment available.",
    }
    if name in messages:
        return messages[name]
    if name in {"FloodWaitError", "FloodPremiumWaitError", "PhoneNumberFloodError", "PhonePasswordFloodError"}:
        seconds = max(1, int(getattr(exc, "seconds", 60)))
        return f"Telegram is limiting requests. Try again in {seconds} seconds."
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return "Telegram could not be reached. Check your internet connection and try again."
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) == 28:
            return "Your disk is full. Free some space, then run sync again."
        if isinstance(exc, PermissionError):
            return "The app cannot write to its archive folder. Check the folder's permissions."
        return "A network or file operation failed. Check your connection and available disk space, then try again."
    return "Telegram could not finish this operation. Check your connection and account access, then try again."


def _json_value(value: Any) -> Any:
    """Retain raw Telegram metadata, including lossless binary references."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "data": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Raw metadata contains a non-string dictionary key")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("Raw metadata contains a non-finite number")
        return value
    raise TypeError(f"Raw metadata contains unsupported type {type(value).__module__}.{type(value).__name__}")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def date_bounds(start: str, end: str = "") -> tuple[datetime, datetime]:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            raise ValueError()
        first = date.fromisoformat(start)
        if end and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            raise ValueError()
        last = date.fromisoformat(end or start)
        if first > last:
            raise UserError("The end date must be on or after the start date.")
        return (datetime.combine(first, datetime.min.time(), timezone.utc),
                datetime.combine(last + timedelta(days=1), datetime.min.time(), timezone.utc))
    except (ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, UserError):
            raise
        raise UserError("Choose valid start and end dates in YYYY-MM-DD format.") from None


class TelegramService:
    CATCH_UP_INTERVAL = 60
    TRANSIENT_RETRIES = 3
    RETRY_DELAYS = (1, 3, 8)

    def __init__(self, root: Path, settings: dict, store: Any, report: Callable[[dict], Any], *, account_root: Path | None = None):
        self.root = Path(root).resolve()
        self.account_root = Path(account_root).resolve() if account_root is not None else self.root
        self.settings = settings
        self.store = store
        self.report = report
        self._client = None
        self._phone = ""
        self._phone_code_hash = None
        self._password_needed = False
        self._cancel = asyncio.Event()
        self._running = False
        self._auth_lock = asyncio.Lock()
        self._counts: dict[str, int] = {}
        self._dirty = False
        self._since_save = 0
        self._last_progress = 0.0
        self._next_catch_up = 0.0
        self.evidence = None
        self._run_id = None
        self._scans = []
        self._issues = []
        self._source_channel_id = None
        self._entity_fetch_attempted = set()
        self._collector = None
        self._watch_failure = None
        self._last_saved_at = None
        self._full_channel = None

    @staticmethod
    def _transient(exc: BaseException) -> bool:
        return isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ in {"ServerError", "RpcCallFailError", "TimedOutError", "InterdcCallErrorError", "InterdcCallRichErrorError"}

    def _emit(self, **fields: Any) -> None:
        result = self.report({**self._counts, **fields})
        if inspect.isawaitable(result):
            # A UI callback is deliberately synchronous; do not create a growing
            # collection of unobserved reporting tasks during a large download.
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("The progress callback must be synchronous")

    def _check_cancel(self) -> None:
        if self._watch_failure is not None:
            raise self._watch_failure
        if self._cancel.is_set():
            raise _Cancelled()

    async def _await(self, awaitable: Any) -> Any:
        """Interrupt network operations promptly, without detaching disk writes."""
        if not self._running:
            return await awaitable
        operation = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(self._cancel.wait())
        try:
            done, _ = await asyncio.wait((operation, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done and self._cancel.is_set():
                operation.cancel()
                with contextlib.suppress(BaseException):
                    await operation
                self._check_cancel()
            return await operation
        finally:
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled
            if not operation.done():
                operation.cancel()
                with contextlib.suppress(BaseException):
                    await operation

    async def _request(self, call: Callable[[], Any], *, retry_transient: bool = True) -> Any:
        attempts = 0
        while True:
            if self._running:
                self._check_cancel()
            try:
                return await self._await(call())
            except Exception as exc:
                if type(exc).__name__ in _AUTH_ERRORS:
                    raise AuthRequired(friendly_error(exc)) from None
                if retry_transient and self._running and self._transient(exc) and attempts < self.TRANSIENT_RETRIES:
                    delay = self.RETRY_DELAYS[min(attempts, len(self.RETRY_DELAYS) - 1)]
                    attempts += 1
                    await self._save()
                    await self._event({"type": "request_retry", "attempt": attempts, "error_type": type(exc).__name__, "delay_seconds": delay})
                    self._emit(phase="retrying", message=f"Telegram's connection was interrupted. Retrying in {delay} seconds ({attempts}/{self.TRANSIENT_RETRIES})…")
                    await self._await(asyncio.sleep(delay))
                    continue
                if type(exc).__name__ not in {"FloodWaitError", "FloodPremiumWaitError"} or not self._running:
                    raise
                seconds = max(1, int(getattr(exc, "seconds", 1)))
                await self._save()
                deadline = time.monotonic() + seconds
                while True:
                    remaining = max(0, int(deadline - time.monotonic() + 0.999))
                    if not remaining:
                        break
                    self._emit(status="running", phase="waiting", message=f"Telegram asked us to wait {remaining} seconds. Your progress is saved.")
                    await self._await(asyncio.sleep(min(remaining, 5)))
                self._emit(status="running", phase="syncing", message="Telegram's wait is over. Continuing…")

    async def _disk(self, call: Callable, *args: Any, **kwargs: Any) -> Any:
        """Do not release the caller's writer lock while a worker still writes."""
        worker = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self._cancel.set()
            with contextlib.suppress(Exception):
                await worker
            raise

    async def _event(self, value: dict) -> None:
        if self.evidence is not None and self._run_id is not None:
            await self._disk(self.evidence.append_event, self._run_id, _json_value(value))

    async def _issue(self, stage: str, exc: BaseException | None = None, **context: Any) -> None:
        issue = {"stage": stage, **context}
        if exc is not None:
            issue.update(error_type=type(exc).__name__, message=friendly_error(exc))
        self._issues.append(issue)
        await self._event({"type": "capture_limitation", **issue})

    async def _observe(self, kind: str, subject_id: Any, payload: Any, tl_bytes: bytes | None = None, context: dict | None = None) -> int | None:
        if self.evidence is None or self._run_id is None:
            return None
        context = {"source_channel_id": self._source_channel_id, **(context or {})}
        if type(context.get("message_id")) is int:
            context.setdefault("parent_message_id", context["message_id"])
            context.setdefault("parent_channel_id", self._source_channel_id)
        return await self._disk(self.evidence.append_observation, self._run_id, kind, subject_id,
                                _json_value(payload), tl_bytes=tl_bytes,
                                context=context)

    async def _capture_object(self, kind: str, subject_id: Any, obj: Any, *, context: dict | None = None, required: bool = False) -> tuple[Any, int | None]:
        """Persist the untouched source object before looking at derived fields."""
        if kind == "message" and (type(subject_id) is not int or subject_id <= 0):
            context = {**(context or {}), "invalid_message_id": subject_id if isinstance(subject_id, (str, int)) else None}
            kind, subject_id = "unidentified_message", type(obj).__name__
        binary = None
        binary_error = None
        if hasattr(obj, "__bytes__"):
            try:
                binary = bytes(obj)
            except Exception as exc:
                binary_error = type(exc).__name__
        try:
            raw = _json_value(obj.to_dict() if hasattr(obj, "to_dict") else obj)
        except Exception as exc:
            raw = {"capture_error": {"object_type": f"{type(obj).__module__}.{type(obj).__name__}",
                                     "error_type": type(exc).__name__, "message": "Raw JSON serialization failed; this is not a complete JSON representation."}}
            observation = await self._observe(kind, subject_id, raw, binary, {**(context or {}), "json_capture": "failed", "tl_capture": "available" if binary is not None else "unavailable"})
            await self._issue("raw_serialization", exc, kind=kind, subject_id=subject_id, observation_id=observation)
            if required:
                raise RawCaptureError("A Telegram object could not be fully represented. Its available serialized TL bytes and capture failure were saved; the run is incomplete.") from None
            return raw, observation
        observation = await self._observe(kind, subject_id, raw, binary,
                                          {**(context or {}), "json_capture": "complete", "tl_capture": "available" if binary is not None else "unavailable", **({"tl_error_type": binary_error} if binary_error else {})})
        if binary_error:
            await self._issue("tl_serialization", None, kind=kind, subject_id=subject_id, error_type=binary_error)
        return raw, observation

    async def _checkpoint(self, scan: dict, *, force: bool = False) -> None:
        if self.evidence is None or not self._run_id:
            return
        if force or scan.get("returned_count", 0) % 25 == 0:
            await self._disk(self.evidence.update_run, self._run_id,
                             checkpoint=self._checkpoint_fields(scan),
                             coverage={"scans": self._scans, "limitations": self._scope_limitations()})

    def _checkpoint_fields(self, scan: dict) -> dict:
        return {"scan_id": scan.get("scan_id"), "last_durable_id": scan.get("last_durable_id"),
                "last_raw_message_id": scan.get("last_raw_message_id"), "returned_count": scan.get("returned_count", 0),
                "last_derived_checkpoint_at": self._last_saved_at,
                "last_durable_id_meaning": "Message raw observation and completed processing receipt; derived JSON may be waiting for its next batch checkpoint.",
                "recovery_policy": "A new sync starts a new anchored scan. Raw observations remain available for inspection/export; this receipt is not an automatic resume cursor."}

    @staticmethod
    def _scope_limitations() -> list[str]:
        return ["Coverage describes history Telegram returned to this account within each anchored scan, not content deleted before capture or content hidden from this account.",
                "Telegram does not provide an atomic historical snapshot; edits, deletions and access changes may occur while pages are being collected.",
                "The initial Telegram total is a channel-wide server count; it is not the expected count for a selected date range."]

    async def _channel_context(self, client: Any, entity: Any) -> None:
        await self._capture_object("channel", entity.id, entity, context={"stage": "basic"}, required=True)
        if not hasattr(client, "__call__"):
            await self._issue("full_channel", None, message="The client does not expose Telegram's full channel metadata request.")
            return
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await self._request(lambda: client(GetFullChannelRequest(entity)))
            await self._capture_object("channel_full", entity.id, full, context={"stage": "full"}, required=True)
            self._full_channel = full
            for related in [*getattr(full, "users", []), *getattr(full, "chats", [])]:
                await self._capture_object("entity", f"{type(related).__name__}:{getattr(related, 'id', 'unknown')}", related, context={"source": "full_channel"}, required=True)
        except (_Cancelled, asyncio.CancelledError, StoreError, AuthRequired, RawCaptureError):
            raise
        except Exception as exc:
            await self._issue("full_channel", exc, channel_id=entity.id)

    async def _message_entities(self, client: Any, message: Any) -> None:
        related = []
        for attribute in ("sender", "chat", "via_bot", "_linked_chat", "_reply_to_chat", "_reply_to_sender"):
            obj = getattr(message, attribute, None)
            if obj is not None:
                related.append((attribute, obj))
        forwarded = getattr(message, "forward", None)
        if forwarded is not None:
            for attribute in ("sender", "chat"):
                obj = getattr(forwarded, attribute, None)
                if obj is not None:
                    related.append(("forward_" + attribute, obj))
        related.extend(("action_entity", obj) for obj in (getattr(message, "action_entities", None) or []) if obj is not None)
        for role, obj in related:
            await self._capture_object("entity", f"{type(obj).__name__}:{getattr(obj, 'id', 'unknown')}", obj, context={"message_id": message.id, "role": role}, required=True)
        for owner, prefix in ((message, ""), (forwarded, "forward_")):
            if owner is None:
                continue
            for role in ("sender", "chat"):
                reference = getattr(owner, role + "_id", None)
                method = getattr(owner, "get_" + role, None)
                key = (prefix + role, reference)
                if reference is None or getattr(owner, role, None) is not None or not callable(method) or key in self._entity_fetch_attempted:
                    continue
                self._entity_fetch_attempted.add(key)
                try:
                    obj = await self._request(method)
                    if obj is None:
                        await self._issue("entity_resolution", None, message_id=message.id, role=prefix + role, reference=reference, message="Telegram did not return this referenced entity.")
                    else:
                        await self._capture_object("entity", f"{type(obj).__name__}:{getattr(obj, 'id', 'unknown')}", obj, context={"message_id": message.id, "role": prefix + role}, required=True)
                except (_Cancelled, asyncio.CancelledError, StoreError, AuthRequired, RawCaptureError):
                    raise
                except Exception as exc:
                    await self._issue("entity_resolution", exc, message_id=message.id, role=prefix + role, reference=reference)

    async def _connected_client(self):
        if self._client is None:
            try:
                api_id = int(self.settings.get("api_id") or 0)
            except (TypeError, ValueError):
                api_id = 0
            api_hash = str(self.settings.get("api_hash") or "").strip()
            if api_id <= 0 or not api_hash:
                raise UserError("Add your Telegram API ID and API hash in Settings first.")
            try:
                from telethon import TelegramClient
            except ImportError:
                raise UserError("Telegram support is not installed. Run the app's setup command and try again.") from None
            self._client = TelegramClient(str(self.account_root / "telegram_scraper"), api_id, api_hash,
                                          flood_sleep_threshold=0, request_retries=3,
                                          connection_retries=3, timeout=15, catch_up=True, sequential_updates=True)
        if not self._client.is_connected():
            await self._request(self._client.connect)
            session = self.account_root / "telegram_scraper.session"
            if session.exists():
                session.chmod(0o600)
        return self._client

    async def status(self) -> dict:
        if not self.settings.get("api_id") or not self.settings.get("api_hash"):
            return {"authorized": False, "configured": False}
        # Merely opening the app must not create a new empty login session.
        if self._client is None and not (self.account_root / "telegram_scraper.session").exists():
            return {"authorized": False, "configured": True}
        try:
            async with self._auth_lock:
                client = await self._connected_client()
                return {"authorized": bool(await self._request(client.is_user_authorized)), "configured": True}
        except Exception as exc:
            raise _user_error(exc) from None

    async def send_code(self, phone: str) -> dict:
        phone = re.sub(r"[\s()\-]", "", str(phone))
        if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            raise UserError("Enter your phone number with its country code, for example +41791234567.")
        try:
            async with self._auth_lock:
                client = await self._connected_client()
                result = await self._request(lambda: client.send_code_request(phone))
                self._phone = phone
                self._phone_code_hash = getattr(result, "phone_code_hash", None)
                self._password_needed = False
            return {"step": "code"}
        except Exception as exc:
            raise _user_error(exc) from None

    async def sign_in(self, code: str = "", password: str = "") -> dict:
        if not self._phone:
            raise UserError("Request a login code first.")
        if not password and not str(code).strip():
            raise UserError("Enter your two-step verification password." if self._password_needed else "Enter the login code Telegram sent you.")
        try:
            async with self._auth_lock:
                client = await self._connected_client()
                if password:
                    await self._request(lambda: client.sign_in(password=password))
                else:
                    await self._request(lambda: client.sign_in(phone=self._phone, code=str(code).replace(" ", ""), phone_code_hash=self._phone_code_hash))
                self._phone = ""
                self._phone_code_hash = None
                self._password_needed = False
            return {"step": "connected"}
        except Exception as exc:
            if type(exc).__name__ == "SessionPasswordNeededError":
                self._password_needed = True
                return {"step": "password"}
            raise _user_error(exc) from None

    async def _entity(self, client: Any):
        from .config import normalize_channel
        channel = normalize_channel(str(self.settings.get("channel") or ""))
        if not channel:
            raise UserError("Choose the Telegram channel to archive in Settings.")
        parsed = urlparse(channel if "://" in channel else "https://" + channel)
        path = parsed.path.strip("/")
        invite = None
        if parsed.hostname in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            if path.startswith("+"):
                invite = path[1:]
            elif path.startswith("joinchat/"):
                invite = path.split("/", 1)[1]
        if invite:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            invitation = await self._request(lambda: client(CheckChatInviteRequest(invite)))
            await self._capture_object("channel_resolution", channel, invitation, context={"method": "CheckChatInviteRequest"})
            if type(invitation).__name__ != "ChatInviteAlready" or not getattr(invitation, "chat", None):
                raise UserError("This account has not joined that private channel. Join it in Telegram first, then try again.")
            return invitation.chat
        target = int(channel) if re.fullmatch(r"-?\d+", channel) else channel
        try:
            entity = await self._request(lambda: client.get_entity(target))
        except ValueError:
            if not isinstance(target, int) or target >= -10**12:
                raise UserError("Telegram could not find that channel. Check its link and your account's access.") from None
            # Private message links carry an ID but no access hash. Populate
            # the session cache from channels the account already belongs to.
            entity = None
            dialogs = client.iter_dialogs().__aiter__()
            while True:
                self._check_cancel()
                try:
                    dialog = await self._request(dialogs.__anext__, retry_transient=False)
                except StopAsyncIteration:
                    break
                if getattr(dialog, "id", None) == target:
                    entity = dialog.entity
                    break
            if entity is None:
                raise UserError("This account cannot access that private channel. Open it in Telegram with the same account first.") from None
        if not getattr(entity, "title", None):
            raise UserError("That link belongs to a person or bot. Choose a Telegram channel or group.")
        return entity

    def _record(self, message: Any, raw: dict | None = None, observation_id: int | None = None) -> dict:
        raw = _json_value(message.to_dict()) if raw is None else raw
        old = self.store.records.get(message.id, {})
        observed_at = datetime.now(timezone.utc).isoformat()
        message_date = getattr(message, "date", None)
        record = {**old, "id": int(message.id), "type": raw.get("_", "Message"),
                  "date": _utc(message_date).isoformat() if message_date else None,
                  "text": getattr(message, "message", None) or getattr(message, "text", None) or "",
                  "edit_date": raw.get("edit_date"), "raw": raw,
                  "first_observed_at": old.get("first_observed_at") or observed_at, "last_observed_at": observed_at,
                  "source_channel_id": self._source_channel_id, "source_observation_id": observation_id,
                  "source_availability": "empty_telegram_object" if raw.get("_") == "MessageEmpty" else "returned_by_telegram"}
        action = raw.get("action")
        if isinstance(action, dict):
            record["action"] = action.get("_", "Unknown")
            if "title" in action:
                record["action_title"] = action["title"]
        document = getattr(message, "document", None)
        photo = getattr(message, "photo", None)
        downloadable = document is not None or photo is not None
        record["has_media"] = downloadable
        if downloadable:
            obj = document if document is not None else photo
            file = getattr(message, "file", None)
            mime = getattr(file, "mime_type", None) or getattr(document, "mime_type", None) or ("image/jpeg" if photo else None)
            kind = "photo" if photo else ("video" if mime and mime.startswith("video/") else "audio" if mime and mime.startswith("audio/") else "document")
            media_id = getattr(obj, "id", None)
            # Replacement attachments must never silently overwrite an older file.
            if old.get("media_id") is not None and old.get("media_id") != media_id:
                previous = list(old.get("previous_media", []))
                previous.append({key: value for key, value in old.items() if key.startswith("media_")})
                record["previous_media"] = previous
                record["media_file"] = None
                record["media_error"] = None
                record["media_sha256"] = None
            record.update(media_id=media_id, media_access_hash=getattr(obj, "access_hash", None),
                          media_kind=kind, media_mime=mime, media_size=getattr(file, "size", None) or getattr(document, "size", None) or (old.get("media_size") if old.get("media_id") == media_id else None),
                          media_name=getattr(file, "name", None))
            current = self.store.media_path(record)
            valid = self._valid_media(current, record)
            record["media_status"] = "downloaded" if valid else "pending"
            if not valid and not self.settings.get("download_media", True):
                record["media_status"] = "not_requested"
        else:
            if old.get("has_media") or old.get("media_file") or old.get("media_id"):
                previous = list(old.get("previous_media", []))
                previous.append({key: value for key, value in old.items() if key.startswith("media_")})
                record["previous_media"] = previous
                # The original file remains on disk and in revision history;
                # the current post must reflect Telegram's removed attachment.
                for key in ("media_file", "media_id", "media_access_hash", "media_sha256", "media_kind", "media_mime", "media_size", "media_name", "media_error"):
                    record[key] = None
            # Polls, locations and other non-file media remain available in raw.
            record["media_status"] = "unavailable" if getattr(message, "media", None) else None
        return record

    @staticmethod
    def _valid_media(path: Path | None, record: dict) -> bool:
        try:
            size = path.stat().st_size if path is not None and path.is_file() else 0
            expected = record.get("media_size")
            return size > 0 and (not expected or size == int(expected))
        except (OSError, ValueError, TypeError):
            return False

    async def _save(self) -> None:
        if self._dirty:
            await self._disk(self.store.save)
            self._dirty = False
            self._since_save = 0
            self._last_saved_at = datetime.now(timezone.utc).isoformat()

    async def _download(self, client: Any, message: Any, record: dict) -> None:
        self._check_cancel()
        media_dir = Path(self.store.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        file_info = getattr(message, "file", None)
        suffix = getattr(file_info, "ext", None) or mimetypes.guess_extension(record.get("media_mime") or "") or ".bin"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ".bin"
        stamp = _utc(message.date).strftime("%Y-%m-%dT%H-%M-%S") if message.date else "undated"
        base = f"Message-{message.id} ({stamp})"
        # Each download owns one temporary file. Passing a stream prevents
        # Telethon from selecting an existing filename or changing its suffix.
        descriptor, name = tempfile.mkstemp(prefix=f".message-{message.id}-", suffix=".part", dir=media_dir)
        temporary = Path(name)
        try:
            async def progress(received: int, total: int) -> None:
                self._check_cancel()
                now = time.monotonic()
                if now - self._last_progress >= 0.2 or received == total:
                    self._last_progress = now
                    self._emit(status="running", phase="downloading", message=f"Downloading attachment for post {message.id}…", current_bytes=received, total_bytes=total)

            with os.fdopen(descriptor, "wb") as stream:
                await self._request(lambda: self._download_to_stream(client, message, stream, progress))
                stream.flush()
                os.fsync(stream.fileno())
            expected = record.get("media_size")
            actual = temporary.stat().st_size
            if actual <= 0:
                raise UserError("Telegram did not return this attachment. Run sync again to retry it.")
            if expected and actual != int(expected):
                raise UserError("The attachment download was incomplete. Run sync again to retry it.")
            digest = await self._disk(self._checksum, temporary)
            self._check_cancel()
            destination = media_dir / f"{base}{suffix}"
            version = 1
            while True:
                try:
                    # Atomic, exclusive publication: os.replace would overwrite.
                    os.link(temporary, destination)
                    break
                except FileExistsError:
                    destination = media_dir / f"{base}-{record.get('media_id') or 'copy'}-{version}{suffix}"
                    version += 1
            record["media_file"] = str(destination.relative_to(self.root))
            record["media_status"] = "downloaded"
            record["media_error"] = None
            record["media_sha256"] = digest
            record["media_size"] = actual
            self._counts["media_downloaded"] += 1
        finally:
            temporary.unlink(missing_ok=True)
            self._emit(current_bytes=0, total_bytes=0)

    def _checksum(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self._check_cancel()
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _download_to_stream(client: Any, message: Any, stream: Any, progress: Any) -> Any:
        # A flood-wait retry starts a fresh transfer, never appends to partial data.
        stream.seek(0)
        stream.truncate()
        return await client.download_media(message, file=stream, progress_callback=progress)

    async def _process(self, client: Any, message: Any, *, source: str = "history", scan_id: str | None = None,
                       captured: tuple | None = None) -> None:
        if captured is None:
            captured = await self._capture_object("message", getattr(message, "id", "unknown"), message,
                                                  context={"source": source, "scan_id": scan_id}, required=True)
        try:
            await self._process_inner(client, message, source=source, scan_id=scan_id, captured=captured)
        except BaseException as exc:
            await self._event({"type": "message_processing_interrupted" if isinstance(exc, (_Cancelled, asyncio.CancelledError)) else "message_processing_failed",
                               "message_id": getattr(message, "id", None), "source": source, "scan_id": scan_id,
                               "observation_id": captured[1], "error_type": type(exc).__name__})
            raise

    async def _process_inner(self, client: Any, message: Any, *, source: str, scan_id: str | None,
                             captured: tuple) -> None:
        self._check_cancel()
        if not getattr(message, "id", None):
            await self._issue("message_identity", None, message="Telegram returned a message without a usable ID.")
            raise UserError("Telegram returned a message without a usable ID. Its raw source was saved; the run is incomplete.")
        old = self.store.records.get(message.id)
        record = self._record(message, raw=captured[0], observation_id=captured[1])
        change = self.store.upsert(record)
        receipt_fields = {"entry", "first_observed_at", "last_observed_at", "source_observation_id"}
        visible_change = change
        if old is not None and change == "updated" and ({key: value for key, value in old.items() if key not in receipt_fields} == {key: value for key, value in record.items() if key not in receipt_fields}):
            visible_change = "unchanged"
        # Store may append revisions; use its canonical merged record from here.
        record = dict(self.store.records[message.id])
        self._dirty = self._dirty or change != "unchanged"
        self._counts["processed"] += 1
        self._since_save += 1
        if visible_change == "added":
            self._counts["added"] += 1
        elif visible_change == "updated":
            self._counts["updated"] += 1
        self._emit(status="running", phase="syncing", message=f"Checking post {message.id}…")
        await self._message_entities(client, message)
        media_enabled = record.get("has_media") and self.settings.get("download_media", True)
        media_path = self.store.media_path(record)
        needs_download = media_enabled and not self._valid_media(media_path, record)
        if media_enabled and not needs_download and record.get("media_sha256"):
            self._emit(phase="verifying", message=f"Checking the saved attachment for post {message.id}…")
            checksum = await self._disk(self._checksum, media_path)
            if checksum != record["media_sha256"]:
                needs_download = True
                record["media_status"] = "pending"
                record["media_error"] = "The saved attachment did not match its checksum. A fresh download is needed."
                self.store.upsert(record)
                self._dirty = True
                if change == "unchanged":
                    self._counts["updated"] += 1
                    change = "updated"
        if needs_download:
            await self._save()  # Keep text even if a large video is interrupted.
            try:
                await self._download(client, message, record)
            except (_Cancelled, asyncio.CancelledError, StoreError, AuthRequired, RawCaptureError):
                raise
            except OSError:
                # A full/read-only disk is not an attachment-specific failure.
                raise
            except Exception as exc:
                record["media_status"] = "failed"
                record["media_error"] = friendly_error(exc)
                self._counts["media_failed"] += 1
                self._emit(message=f"Saved post {message.id}. Its attachment needs another attempt.")
            changed_after_download = self.store.upsert(record)
            self._dirty = self._dirty or changed_after_download != "unchanged"
            if change == "unchanged" and old is not None and changed_after_download == "updated":
                self._counts["updated"] += 1
            await self._save()
        elif self._since_save >= 25:
            await self._save()
        if self._collector is not None:
            self._emit(phase="context", message=f"Capturing related information for post {message.id}…")
            summary = await self._collector.capture_message(self._channel_entity, message)
            if summary.get("status") == "partial":
                await self._issue("related_context", None, message_id=message.id, summary=summary)
            record = dict(self.store.records[message.id])
            record["context_capture"] = summary
            self.store.upsert(record)
            self._dirty = True
            await self._save()
        await self._event({"type": "message_processed", "message_id": message.id, "source": source, "scan_id": scan_id,
                           "observation_id": captured[1], "media_status": self.store.records[message.id].get("media_status")})

    async def _history(self, client: Any, entity: Any, bounds: tuple | None = None) -> None:
        if hasattr(client, "__call__"):
            await self._history_pages(client, entity, bounds)
            return
        # Compatibility path for alternate clients that only expose a high-level
        # iterator. Its missing page/anchor receipts are explicitly incomplete.
        await self._issue("history_protocol", None, message="This client exposes only an iterator; raw history pages, the anchored top ID and Telegram's total are unavailable.")
        # Walk newest to oldest. offset_date is exclusive, matching the next-day
        # upper bound; there is intentionally no arbitrary history-size limit.
        kwargs = {"limit": None}
        if bounds:
            kwargs["offset_date"] = bounds[1]
        iterator = client.iter_messages(entity, **kwargs).__aiter__()
        while True:
            self._check_cancel()
            try:
                message = await self._request(lambda: iterator.__anext__(), retry_transient=False)
            except StopAsyncIteration:
                break
            if bounds:
                if not message.date:
                    continue
                timestamp = _utc(message.date)
                if timestamp < bounds[0]:
                    break
                if timestamp >= bounds[1]:
                    continue
            await self._process(client, message)

    async def _history_pages(self, client: Any, entity: Any, bounds: tuple | None = None) -> None:
        from telethon.tl.functions.messages import GetHistoryRequest
        from telethon import utils
        scan = {"scan_id": f"scan-{len(self._scans) + 1}", "scope": "utc_date_range" if bounds else "all_accessible_channel_history",
                "start": bounds[0].isoformat() if bounds else None, "end_exclusive": bounds[1].isoformat() if bounds else None,
                "anchor_top_id": None, "telegram_total_before": None, "telegram_total_scope": "all_channel_history",
                "returned_count": 0, "accessible_message_count": 0, "empty_object_count": 0,
                "date_membership_unconfirmed": 0,
                "last_durable_id": None, "pages": 0, "history_traversal_complete": False,
                "coverage_complete": False, "count_reconciled": None}
        self._scans.append(scan)
        await self._checkpoint(scan, force=True)
        def request(offset_id=0, *, limit=100, upper=0, offset_date=None):
            return GetHistoryRequest(peer=entity, offset_id=offset_id, offset_date=offset_date,
                                     add_offset=0, limit=limit, max_id=upper, min_id=0, hash=0)
        try:
            input_chat = utils.get_input_peer(entity)
        except (TypeError, ValueError):
            input_chat = None
        anchor = await self._request(lambda: client(request(limit=1)))
        await self._capture_object("history_anchor", scan["scan_id"], anchor, context={"scan_id": scan["scan_id"]}, required=True)
        anchor_messages = self._history_vector(anchor)
        scan["anchor_top_id"] = max((getattr(message, "id", 0) for message in anchor_messages), default=0)
        count = getattr(anchor, "count", None)
        scan["telegram_total_before"] = count if isinstance(count, int) else len(anchor_messages) if type(anchor).__name__ == "Messages" else None
        if scan["telegram_total_before"] is None:
            await self._issue("history_total_unavailable", None, scan_id=scan["scan_id"], message="The anchor response did not include a usable Telegram history total.")
        await self._checkpoint(scan, force=True)
        seen = set()
        cursor = 0
        lower_reached = False
        while True:
            self._check_cancel()
            if scan["anchor_top_id"] == 0:
                scan["history_traversal_complete"] = True
                break
            response = await self._request(lambda: client(request(cursor, upper=scan["anchor_top_id"] + 1,
                                                                  offset_date=bounds[1] if bounds and cursor == 0 else None)))
            scan["pages"] += 1
            await self._capture_object("history_page", f"{scan['scan_id']}:{cursor}", response,
                                       context={"scan_id": scan["scan_id"], "offset_id": cursor, "upper_exclusive": scan["anchor_top_id"] + 1, "page": scan["pages"]}, required=True)
            entities = {}
            for obj in [*getattr(response, "users", []), *getattr(response, "chats", [])]:
                await self._capture_object("entity", f"{type(obj).__name__}:{getattr(obj, 'id', 'unknown')}", obj,
                                           context={"scan_id": scan["scan_id"], "source": "history_page", "page": scan["pages"]}, required=True)
                try:
                    entities[utils.get_peer_id(obj)] = obj
                except (TypeError, ValueError):
                    pass
            messages = self._history_vector(response)
            if not messages:
                scan["history_traversal_complete"] = True
                break
            next_cursor = cursor
            for message in messages:
                captured = await self._capture_object("message", getattr(message, "id", "unknown"), message,
                                                      context={"source": "history", "scan_id": scan["scan_id"], "page": scan["pages"]}, required=True)
                message_id = getattr(message, "id", None)
                if not isinstance(message_id, int) or message_id <= 0:
                    raise UserError("A Telegram history page contained an invalid message ID. The raw page is saved; coverage is incomplete.")
                scan["last_raw_message_id"] = message_id
                if message_id > scan["anchor_top_id"] or cursor and message_id >= cursor:
                    continue
                next_cursor = message_id if not next_cursor else min(next_cursor, message_id)
                if message_id in seen:
                    continue
                seen.add(message_id)
                scan["returned_count"] += 1
                stamp = getattr(message, "date", None)
                if bounds and stamp is not None:
                    stamp = _utc(stamp)
                    if stamp < bounds[0]:
                        lower_reached = True
                        continue
                    if stamp >= bounds[1]:
                        continue
                elif bounds and stamp is None:
                    scan["date_membership_unconfirmed"] += 1
                    await self._issue("range_date_unavailable", None, message_id=message_id, message="A returned Telegram object has no date, so its date-range membership is unconfirmed.")
                    continue
                if hasattr(message, "_finish_init"):
                    # Source JSON/TL bytes above precede Telethon's local enrichments.
                    message._finish_init(client, entities, input_chat)
                await self._process(client, message, source="history", scan_id=scan["scan_id"], captured=captured)
                scan["accessible_message_count"] += 1
                if captured[0].get("_") == "MessageEmpty":
                    scan["empty_object_count"] += 1
                    await self._issue("message_contents_unavailable", None, message_id=message_id, message="Telegram returned an empty message object. Its available fields were captured; its original content is unavailable from this response.")
                scan["last_durable_id"] = message_id
                await self._checkpoint(scan)
            if lower_reached:
                scan["history_traversal_complete"] = True
                scan["stopped_at_range_lower_bound"] = True
                break
            if not next_cursor or next_cursor == cursor:
                await self._issue("history_no_progress", None, scan_id=scan["scan_id"], offset_id=cursor, message="Telegram repeated a history page without an older message. The scan cannot claim complete coverage.")
                raise UserError("Telegram repeated a history page without advancing. The captured pages are saved; run sync again to continue checking coverage.")
            cursor = next_cursor
        if not bounds and scan["telegram_total_before"] is not None:
            scan["count_reconciled"] = scan["accessible_message_count"] == scan["telegram_total_before"]
            if not scan["count_reconciled"]:
                await self._issue("history_count_difference", None, scan_id=scan["scan_id"], expected=scan["telegram_total_before"], captured=scan["accessible_message_count"], message="The anchored Telegram total differs from returned objects. This can reflect access limits or changes during capture; completeness is unconfirmed.")
        scan["coverage_complete"] = bool(scan["history_traversal_complete"] and scan["count_reconciled"] is not False
                                         and scan["telegram_total_before"] is not None and not scan["date_membership_unconfirmed"])
        await self._checkpoint(scan, force=True)

    @staticmethod
    def _history_vector(response: Any) -> list:
        messages = getattr(response, "messages", None)
        if not isinstance(messages, (list, tuple)):
            raise RawCaptureError("Telegram returned an unsupported history response without a message list. The raw response was saved; history coverage is incomplete.")
        if any(type(getattr(message, "id", None)) is not int or message.id <= 0 for message in messages):
            raise RawCaptureError("Telegram returned a history object without a valid message ID. The raw response was saved; history coverage is incomplete.")
        return list(messages)

    async def _watch(self, client: Any, entity: Any) -> None:
        from telethon import events
        pending: OrderedDict[int, Any] = OrderedDict()
        wake = asyncio.Event()
        overflow = False
        stopping = False
        receipts = set()

        async def receive(event: Any) -> None:
            nonlocal overflow
            if stopping:
                return
            finished = asyncio.get_running_loop().create_future()
            receipts.add(finished)
            message = event.message
            try:
                update = getattr(event, "original_update", None)
                if update is not None:
                    await self._capture_object("telegram_update", message.id, update, context={"source": "watch"}, required=True)
                captured = await self._capture_object("message", message.id, message, context={"source": "watch"}, required=True)
            except BaseException as exc:
                self._watch_failure = exc
                self._cancel.set()
                wake.set()
                return
            finally:
                finished.set_result(None)
                receipts.discard(finished)
            if message.id in pending:
                pending[message.id] = (message, captured)
            elif len(pending) < 512:
                pending[message.id] = (message, captured)
            else:
                # Coalesce bursts and recover from history instead of allowing
                # a download queue to grow without bound.
                overflow = True
            wake.set()

        handlers = [events.NewMessage(chats=entity), events.MessageEdited(chats=entity)]
        for handler in handlers:
            client.add_event_handler(receive, handler)
        try:
            # Register first, so posts arriving during the catch-up are retained.
            if hasattr(client, "catch_up"):
                await self._request(client.catch_up)
            self._next_catch_up = time.monotonic() + self.CATCH_UP_INTERVAL
            await self._history(client, entity)
            while True:
                self._check_cancel()
                if overflow:
                    overflow = False
                    self._emit(phase="syncing", message="Catching up after a burst of channel activity…")
                    await self._history(client, entity)
                while pending:
                    _, (message, captured) = pending.popitem(last=False)
                    await self._process(client, message, source="watch", captured=captured)
                await self._save()
                if overflow:
                    continue
                wake.clear()
                self._emit(status="running", phase="watching", message="Up to date. Watching for new posts and edits…")
                await self._wait_for_update(client, wake)
        finally:
            stopping = True
            for handler in handlers:
                client.remove_event_handler(receive, handler)
            # An event callback may already be committing raw evidence when Stop
            # arrives. Drain its receipt, not Telethon's long-lived updater task,
            # before closing this run and permitting another one to start.
            if receipts:
                await asyncio.gather(*tuple(receipts), return_exceptions=True)

    async def _wait_for_update(self, client: Any, wake: asyncio.Event) -> None:
        update = asyncio.create_task(wake.wait())
        timer = asyncio.create_task(asyncio.sleep(max(0, self._next_catch_up - time.monotonic())))
        disconnected = getattr(client, "disconnected", None)
        shield = asyncio.ensure_future(asyncio.shield(disconnected)) if disconnected is not None else None
        try:
            pending = (update, timer, shield) if shield is not None else (update, timer)
            done, _ = await self._await(asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED))
            if shield is not None and shield in done:
                raise UserError("The Telegram connection ended. Run Watch again to reconnect and catch up.")
            if timer in done:
                # Telethon 1.x's reconnect callback does not itself catch up.
                # Its public catch_up() requests missed new/edit updates using
                # the persisted update cursor and remains bounded by our queue.
                if hasattr(client, "catch_up"):
                    await self._request(client.catch_up)
                self._next_catch_up = time.monotonic() + self.CATCH_UP_INTERVAL
        finally:
            for task in (update, timer, shield):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def run(self, mode: str, start: str = "", end: str = "") -> dict:
        if self._running:
            raise UserError("An archive operation is already running. Stop it before starting another.")
        if mode not in {"sync", "range", "watch"}:
            raise UserError("Choose Sync, Date range, or Watch.")
        bounds = date_bounds(start, end) if mode == "range" else None
        self._running = True
        self._cancel.clear()
        self._counts = {"processed": 0, "added": 0, "updated": 0, "media_downloaded": 0, "media_failed": 0}
        self._dirty = False
        self._since_save = 0
        self._scans = []
        self._issues = []
        self._entity_fetch_attempted = set()
        self._source_channel_id = None
        self._watch_failure = None
        self._collector = None
        self._run_id = None
        self._last_saved_at = None
        self._full_channel = None
        outcome = "completed"
        failure = None
        authentication_failure = False
        self._emit(status="running", phase="connecting", message="Connecting to Telegram…", current_bytes=0, total_bytes=0)
        try:
            await self._disk(self.store.load)
            from .evidence import EvidenceStore
            if self.evidence is None:
                self.evidence = EvidenceStore(self.root)
            await self._disk(self.evidence.prepare)
            from . import __version__
            import telethon
            from telethon.tl.alltlobjects import LAYER
            self._run_id = await self._disk(self.evidence.begin_run, {"mode": mode, "start": start or None, "end": end or None,
                                                                    "channel": str(self.settings.get("channel") or ""),
                                                                    "download_media": bool(self.settings.get("download_media", True)),
                                                                    "capture_context": bool(self.settings.get("capture_context", True)),
                                                                    "app_version": __version__, "telethon_version": telethon.__version__, "telegram_api_layer": LAYER,
                                                                    "tl_bytes_representation": "Telethon serialization of the returned TL object, not the original encrypted network packet",
                                                                    "scope_limitations": self._scope_limitations()})
            self._emit(run_id=self._run_id)
            client = await self._connected_client()
            if not await self._request(client.is_user_authorized):
                raise AuthRequired("Connect your Telegram account before starting an archive.")
            entity = await self._entity(client)
            self._source_channel_id = entity.id
            self._channel_entity = entity
            await self._disk(self.store.bind_channel, entity.id, getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id), str(self.settings.get("channel") or ""), str(self.settings.get("legacy_channel") or ""))
            await self._disk(self.evidence.update_run, self._run_id, channel_id=entity.id)
            self._check_cancel()
            if self.settings.get("archive_before_sync", True) and self.store.has_snapshot_data():
                self._emit(phase="snapshot", message="Saving a recovery snapshot before syncing…")
                await self._disk(self.store.snapshot, cancel=self._cancel.is_set, report=lambda message: self._emit(phase="snapshot", message=message))
            self._check_cancel()
            await self._channel_context(client, entity)
            if self.settings.get("capture_context", True):
                from .enrichment import ContextCollector
                async def record_context(kind, subject_id, obj, context=None):
                    _, observation = await self._capture_object(kind, subject_id, obj, context=context, required=True)
                    return observation
                self._collector = ContextCollector(client, self._request, record_context, self._cancel.is_set,
                                                   bool(self.settings.get("download_media", True)), self.root)
                channel_photo = getattr(getattr(self._full_channel, "full_chat", None), "chat_photo", None)
                if channel_photo is not None:
                    summary = await self._collector.capture_channel_photo(entity, channel_photo)
                    if summary.get("status") == "partial":
                        await self._issue("channel_photo", None, summary=summary)
            if mode == "watch":
                await self._watch(client, entity)
            else:
                await self._history(client, entity, bounds)
        except (_Cancelled, asyncio.CancelledError):
            outcome = "cancelled"
        except Exception as exc:
            if type(exc).__name__ == "OperationCancelled" and self._cancel.is_set():
                outcome = "cancelled"
            else:
                outcome = "error"
                authentication_failure = isinstance(exc, AuthRequired) or type(exc).__name__ in _AUTH_ERRORS
                # Storage errors already contain actionable, locally controlled text.
                failure = str(exc) if isinstance(exc, StoreError) else friendly_error(exc)
        finally:
            try:
                await self._save()
            except Exception as exc:
                outcome = "error"
                failure = str(exc) if isinstance(exc, StoreError) else friendly_error(exc)
                if self._run_id is not None:
                    failure += " Raw source observations already captured remain in the source archive; this run is incomplete."
            if outcome == "completed" and (self._counts["media_failed"] or self._issues):
                outcome = "warning"
            coverage = {"scans": self._scans, "limitations": self._scope_limitations(),
                        "history_scan_complete": bool(self._scans and all(scan.get("coverage_complete") for scan in self._scans)),
                        "capture_complete": outcome == "completed" and not self._issues,
                        "related_context_requested": bool(self.settings.get("capture_context", True))}
            if self.evidence is not None and self._run_id is not None:
                try:
                    await self._disk(self.evidence.finish_run, self._run_id, status=outcome, coverage=coverage,
                                     counts=self._counts, issues=self._issues, checkpoint=self._checkpoint_fields(self._scans[-1] if self._scans else {}),
                                     context={"failure": failure, "stopped": outcome == "cancelled"})
                except Exception as exc:
                    outcome = "error"
                    failure = "The run's final evidence receipt could not be saved. Its recorded observations remain; completeness is unconfirmed."
            self._running = False
            self._emit(status=outcome, phase="idle", message=failure or ("Stopped. Captured observations, completed posts and downloads are saved; this run is incomplete." if outcome == "cancelled" else "Sync complete. The run's source and coverage receipts are saved." if outcome == "completed" else "Captured information is saved. Some attachments or source details need attention; see the run's coverage report."), current_bytes=0, total_bytes=0, run_id=self._run_id, coverage=coverage, issues_count=len(self._issues))
        if failure:
            raise (AuthRequired if authentication_failure else UserError)(failure)
        return {"status": outcome, "run_id": self._run_id, "coverage": coverage, "issues_count": len(self._issues), **self._counts}

    def cancel(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        self.cancel()
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
