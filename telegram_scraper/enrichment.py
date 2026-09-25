"""Read-only contextual capture for a channel message.

Raw replies, voters, reactions, and their returned peer objects are observations,
not channel posts. No send, vote, join, mark-read, or participant-enumeration API
is used here. Each page is committed before processing its attachments.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid

from .storage import StoreError
from .media import descriptor as media_descriptor


class ContextCollector:
    """Capture available context without treating omissions as successful capture.

    ``request`` is the engine's retry/cancellation wrapper accepting a zero-arg
    awaitable factory. ``record(kind, subject_id, object, context=...)`` must
    persist an untouched TLObject (including its exact serialized bytes) or a
    JSON-compatible local receipt before returning. Persistence errors propagate.
    ``cancelled`` may return True or raise the caller's cancellation exception.
    """

    PAGE_SIZE = 100

    def __init__(self, client, request, record, cancelled, download_media: bool, root: Path):
        self.client = client
        self.request = request
        self.record = record
        self.cancelled = cancelled
        self.download_media = bool(download_media)
        self.root = Path(root).resolve()

    def _check(self):
        if self.cancelled():
            raise asyncio.CancelledError()

    @staticmethod
    def _fatal_storage(exc):
        return isinstance(exc, (StoreError, PermissionError)) or isinstance(exc, OSError) and exc.errno in {
            errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS, getattr(errno, "EDQUOT", -1)}

    async def _disk(self, function, *args):
        """Never leave a disk worker running after releasing the archive lock."""
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Repeated Stop/shutdown cancellation must not detach the worker.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                worker.exception()  # Retrieve any disk exception before stopping.
            raise

    @staticmethod
    def _id(peer):
        for name in ("channel_id", "chat_id", "user_id", "id"):
            value = getattr(peer, name, None)
            if value is not None:
                return int(value)
        return None

    @staticmethod
    def _summary():
        return {"status": "complete", "counts": {
            "comment_pages": 0, "comments": 0, "poll_result_pages": 0,
            "poll_voter_pages": 0, "poll_voters": 0, "reaction_pages": 0,
            "reaction_entries": 0, "reaction_snapshots": 0,
            "variants_discovered": 0, "variants_downloaded": 0,
            "variants_reused": 0, "variants_failed": 0,
        }, "limitations": [], "errors": [], "media_variants": []}

    async def _observe(self, kind, subject, value, context):
        self._check()
        # Do not pre-convert: the engine records bytes(TLObject) before JSON.
        try:
            await self.record(kind, str(subject), value, context=dict(context))
        except asyncio.CancelledError:
            raise
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError("Source context could not be recorded. Capture stopped to avoid claiming unsaved data.") from exc

    async def _issue(self, summary, subject, area, code, message, context, *, error=False):
        self._check()
        item = {"area": area, "code": code, "message": message}
        summary["errors" if error else "limitations"].append(item)
        summary["status"] = "partial"
        await self._observe("context_limitation", subject, item, context)

    async def _attempt(self, summary, subject, area, context, operation):
        self._check()
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._check()
            from .engine import AuthRequired, friendly_error
            if isinstance(exc, AuthRequired) or self._fatal_storage(exc):
                raise
            await self._issue(summary, subject, area, type(exc).__name__, friendly_error(exc), context, error=True)

    async def capture_message(self, entity: Any, message: Any, *, primary_receipt=None) -> dict:
        self._check()
        channel_id = self._id(entity) or self._id(getattr(message, "peer_id", None))
        context = {"channel_id": channel_id, "parent_channel_id": channel_id, "parent_message_id": int(message.id),
                   "peer_id": self._id(getattr(message, "peer_id", None)) or channel_id,
                   "message_id": int(message.id)}
        subject = f"{channel_id}:{message.id}"
        summary = self._summary()
        await self._attempt(summary, subject, "comments", context,
                            lambda: self._comments(entity, message, subject, context, summary))
        await self._associated(entity, message, subject, context, summary, include_primary=False, primary_receipt=primary_receipt)
        await self._observe("message_context_receipt", subject, summary, context)
        return summary

    async def capture_channel_photo(self, entity: Any, photo: Any) -> dict:
        """Capture all sizes of the full channel photo, when Telegram returned it."""
        from types import SimpleNamespace
        from telethon.tl.types import MessageMediaPhoto, Photo, PhotoEmpty, ChatPhotoEmpty
        self._check()
        summary = self._summary()
        channel_id = self._id(entity)
        context = {"channel_id": channel_id, "parent_channel_id": channel_id,
                   "parent_message_id": None, "peer_id": channel_id,
                   "message_id": None, "relation": "channel_profile_photo"}
        subject = f"{channel_id}:profile-photo:{getattr(photo, 'id', 'absent')}"
        if isinstance(photo, Photo):
            await self._observe("channel_profile_photo", subject, photo, context)
            holder = SimpleNamespace(media=MessageMediaPhoto(photo=photo))
            await self._attempt(summary, subject, "channel_profile_photo", context,
                                lambda: self._variants(holder, subject, context, summary, include_primary=True))
        elif photo is not None:
            await self._observe("channel_profile_photo", subject, photo, context)
            if not isinstance(photo, (PhotoEmpty, ChatPhotoEmpty)):
                await self._issue(summary, subject, "channel_profile_photo", "photo_bytes_not_exposed",
                                  "Telegram returned profile photo metadata without downloadable photo sizes.", context)
        await self._observe("channel_profile_photo_receipt", subject, summary, context)
        return summary

    async def _associated(self, entity, message, subject, context, summary, *, include_primary, primary_receipt=None):
        await self._attempt(summary, subject, "poll", context,
                            lambda: self._poll(entity, message, subject, context, summary))
        await self._attempt(summary, subject, "reactions", context,
                            lambda: self._reactions(entity, message, subject, context, summary))
        await self._attempt(summary, subject, "media_variants", context,
                            lambda: self._variants(message, subject, context, summary, include_primary=include_primary, primary_receipt=primary_receipt))

    async def _comments(self, entity, message, subject, context, summary):
        from telethon.tl.functions.messages import GetRepliesRequest
        replies = getattr(message, "replies", None)
        if replies is None or not (getattr(replies, "comments", False) or int(getattr(replies, "replies", 0) or 0)):
            return
        # max_id is exclusive. Anchor to the latest reply advertised by this
        # message; if absent, anchor to the first returned page instead.
        advertised_max = int(getattr(replies, "max_id", 0) or 0)
        anchor = advertised_max + 1 if advertised_max else 0
        offset = 0
        seen = set()
        while True:
            self._check()
            page = await self.request(lambda: self.client(GetRepliesRequest(
                peer=entity, msg_id=int(message.id), offset_id=offset,
                offset_date=None, add_offset=0, limit=self.PAGE_SIZE,
                max_id=anchor, min_id=0, hash=0)))
            page_context = {**context, "offset_id": offset, "exclusive_max_id": anchor,
                            "method": "messages.getReplies"}
            await self._observe("comment_page", subject, page, page_context)
            summary["counts"]["comment_pages"] += 1
            messages = getattr(page, "messages", None)
            if messages is None:
                await self._issue(summary, subject, "comments", "unexpected_response",
                                  "Telegram did not return an enumerable reply page.", page_context, error=True)
                return
            if not messages:
                return
            ids = [int(m.id) for m in messages if int(getattr(m, "id", 0) or 0) > 0]
            if not ids:
                await self._issue(summary, subject, "comments", "missing_cursor",
                                  "The reply page contained no usable message IDs.", page_context, error=True)
                return
            if not anchor:
                anchor = max(ids) + 1
            new_ids = {value for value in ids if value < anchor} - seen
            if not new_ids:
                await self._issue(summary, subject, "comments", "repeated_page",
                                  "Telegram repeated a reply page. The thread was not marked complete.", page_context, error=True)
                return
            chats = {self._id(chat): chat for chat in getattr(page, "chats", [])}
            for reply in messages:
                self._check()
                reply_id = int(getattr(reply, "id", 0) or 0)
                if reply_id not in new_ids:
                    continue
                new_ids.remove(reply_id)
                seen.add(reply_id)
                peer = getattr(reply, "peer_id", None)
                peer_id = self._id(peer)
                reply_context = {**context, "peer_id": peer_id, "message_id": reply_id,
                                 "relation": "comment"}
                summary["counts"]["comments"] += 1
                # Comments have their own primary attachments. They stay out of
                # the channel index but their media and nested poll data survive.
                reply_entity = chats.get(peer_id) or peer or entity
                await self._associated(reply_entity, reply, f"{subject}:reply:{peer_id}:{reply_id}",
                                       reply_context, summary, include_primary=True)
            next_offset = min(ids)
            if offset and next_offset >= offset:
                await self._issue(summary, subject, "comments", "nonadvancing_cursor",
                                  "Reply pagination stopped advancing. More replies may remain.", page_context, error=True)
                return
            offset = next_offset
            # Short pages are not conclusive: one more read establishes an empty
            # page even when Telegram returns fewer than the requested 100 items.

    async def _poll(self, entity, message, subject, context, summary):
        from telethon.tl.functions.messages import GetPollResultsRequest, GetPollVotesRequest
        media = getattr(message, "media", None)
        poll = getattr(media, "poll", None)
        if poll is None:
            return
        page = await self.request(lambda: self.client(GetPollResultsRequest(peer=entity, msg_id=int(message.id))))
        await self._observe("poll_results", subject, page, {**context, "method": "messages.getPollResults"})
        summary["counts"]["poll_result_pages"] += 1
        if not bool(getattr(poll, "public_voters", False)):
            await self._issue(summary, subject, "poll_voters", "anonymous_poll",
                              "Telegram does not expose voter identities for this anonymous poll.", context)
            return
        offset = None
        seen = set()
        while True:
            self._check()
            page = await self.request(lambda: self.client(GetPollVotesRequest(
                peer=entity, id=int(message.id), limit=self.PAGE_SIZE, option=None, offset=offset)))
            await self._observe("poll_voter_page", subject, page,
                                {**context, "offset": offset, "method": "messages.getPollVotes"})
            votes = getattr(page, "votes", None)
            if votes is None:
                await self._issue(summary, subject, "poll_voters", "unexpected_response",
                                  "Telegram did not return an enumerable voter page.", context, error=True)
                return
            summary["counts"]["poll_voter_pages"] += 1
            summary["counts"]["poll_voters"] += len(votes)
            next_offset = getattr(page, "next_offset", None)
            if not next_offset:
                return
            if not votes or next_offset == offset or next_offset in seen:
                await self._issue(summary, subject, "poll_voters", "repeated_cursor",
                                  "Voter pagination stopped advancing. More votes may remain.", context, error=True)
                return
            seen.add(next_offset)
            offset = next_offset

    async def _reactions(self, entity, message, subject, context, summary):
        from telethon.tl.functions.messages import GetMessagesReactionsRequest, GetMessageReactionsListRequest
        reactions = getattr(message, "reactions", None)
        if reactions is None:
            return
        page = await self.request(lambda: self.client(GetMessagesReactionsRequest(peer=entity, id=[int(message.id)])))
        await self._observe("reaction_snapshot", subject, page,
                            {**context, "method": "messages.getMessagesReactions"})
        summary["counts"]["reaction_snapshots"] += 1
        broadcast = bool(getattr(entity, "broadcast", False))
        group = bool(getattr(entity, "megagroup", False)) or type(entity).__name__ in {"Chat", "ChatForbidden"}
        if broadcast or not group or not bool(getattr(reactions, "can_see_list", False)):
            await self._issue(summary, subject, "reaction_identities", "not_exposed_for_this_peer",
                              "Reaction totals were requested. Telegram does not expose a supported identity list for this channel or account.", context)
            return
        offset = None
        seen = set()
        while True:
            self._check()
            page = await self.request(lambda: self.client(GetMessageReactionsListRequest(
                peer=entity, id=int(message.id), limit=self.PAGE_SIZE, offset=offset)))
            await self._observe("reaction_identity_page", subject, page,
                                {**context, "offset": offset, "method": "messages.getMessageReactionsList"})
            entries = getattr(page, "reactions", None)
            if entries is None:
                await self._issue(summary, subject, "reaction_identities", "unexpected_response",
                                  "Telegram did not return an enumerable reaction page.", context, error=True)
                return
            summary["counts"]["reaction_pages"] += 1
            summary["counts"]["reaction_entries"] += len(entries)
            next_offset = getattr(page, "next_offset", None)
            if not next_offset:
                return
            if not entries or next_offset == offset or next_offset in seen:
                await self._issue(summary, subject, "reaction_identities", "repeated_cursor",
                                  "Reaction pagination stopped advancing. More reactions may remain.", context, error=True)
                return
            seen.add(next_offset)
            offset = next_offset

    def _inventory(self, message, *, include_primary=False):
        """Enumerate actual TL media objects, including nested/alternate media."""
        from telethon.tl import types
        media = getattr(message, "media", None)
        primary_document = getattr(media, "document", None)
        objects = []
        visited = set()

        def walk(value, path):
            if value is None or isinstance(value, (str, bytes, int, float, bool)):
                return
            if id(value) in visited:
                return
            visited.add(id(value))
            if isinstance(value, (types.Document, types.Photo)):
                objects.append((path, value))
                return
            if isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, f"{path}.{index}")
            elif hasattr(value, "__dict__"):
                for key, child in vars(value).items():
                    if not key.startswith("_"):
                        walk(child, f"{path}.{key}")
        walk(media, "media")
        variants = []
        unique = set()

        def add(obj, role, size=None, primary=False):
            size_type = getattr(size, "type", "") if size is not None else ""
            identity = f"{type(obj).__name__}:{obj.id}:{type(size).__name__ if size is not None else 'original'}:{size_type}"
            if identity in unique:
                return
            unique.add(identity)
            expected = getattr(size, "size", None) if size is not None else getattr(obj, "size", None)
            progressive = getattr(size, "sizes", None)
            if progressive:
                expected = max(progressive)
            embedded = getattr(size, "bytes", None) if size is not None else None
            if embedded is not None:
                expected = len(embedded)
            mime = getattr(obj, "mime_type", None) if size is None else None
            if type(size).__name__ == "VideoSize":
                mime = "video/mp4"
            if type(size).__name__ in {"PhotoStrippedSize", "PhotoPathSize"}:
                mime = "application/octet-stream"
            variants.append({"identity": identity, "role": role, "object_id": int(obj.id),
                             "descriptor": media_descriptor(obj, size),
                             "size_type": size_type, "constructor": type(size).__name__ if size is not None else type(obj).__name__,
                             "expected_bytes": expected, "mime_type": mime,
                             "width": getattr(size, "w", None), "height": getattr(size, "h", None),
                             "primary_managed_elsewhere": primary and not include_primary,
                             "_object": obj, "_size": size, "_embedded": embedded})
        for path, obj in objects:
            if isinstance(obj, types.Document):
                add(obj, path, primary=obj is primary_document)
                for size in (getattr(obj, "thumbs", None) or []) + (getattr(obj, "video_thumbs", None) or []):
                    add(obj, f"{path}.thumbnail", size)
            else:
                for size in (getattr(obj, "sizes", None) or []) + (getattr(obj, "video_sizes", None) or []):
                    add(obj, f"{path}.photo_size", size)
        return variants

    async def _variants(self, message, subject, context, summary, *, include_primary, primary_receipt=None):
        media = getattr(message, "media", None)
        constructor = type(media).__name__
        if constructor == "MessageMediaUnsupported":
            await self._issue(summary, subject, "media_variants", "unsupported_media",
                              "Telegram returned media that this API version cannot decode. Its raw source record is retained.", context)
        if constructor == "MessageMediaStory" and getattr(media, "story", None) is None:
            await self._issue(summary, subject, "media_variants", "story_reference_not_fetched",
                              "This post references a separate Telegram story. The reference is saved; the story was not requested.", context)
        if constructor == "MessageMediaPaidMedia" and any(type(item).__name__ == "MessageExtendedMediaPreview" for item in getattr(media, "extended_media", [])):
            await self._issue(summary, subject, "media_variants", "paid_media_preview_only",
                              "Telegram returned a preview for paid media. No purchase was made and the locked original was not downloaded.", context)
        variants = self._inventory(message, include_primary=include_primary)
        if not variants:
            return
        public = [{k: v for k, v in item.items() if not k.startswith("_")} for item in variants]
        await self._observe("media_variant_inventory", subject, {"variants": public}, context)
        summary["counts"]["variants_discovered"] += len(variants)
        for item in variants:
            self._check()
            descriptor = {k: v for k, v in item.items() if not k.startswith("_")}
            descriptor["context"] = dict(context)
            if primary_receipt and item["descriptor"] == primary_receipt.get("descriptor"):
                from .media import safe_primary_path
                path = safe_primary_path(self.root, primary_receipt.get("path"))
                try:
                    valid = path is not None and await self._disk(self._checksum, path) == (primary_receipt["sha256"], primary_receipt["size"])
                except (OSError, ValueError, KeyError):
                    valid = False
                if valid:
                    summary["media_variants"].append({**descriptor, "state": "primary_archive", "primary_receipt": primary_receipt,
                                                      "media_file": primary_receipt["path"]})
                    summary["counts"]["variants_reused"] += 1
                    continue
            elif item["primary_managed_elsewhere"] and not primary_receipt:
                # Compatibility for callers without a primary downloader. No
                # saved bytes are claimed by this metadata-only legacy marker.
                summary["media_variants"].append({**descriptor, "state": "primary_archive"})
                continue
            if not self.download_media:
                summary["media_variants"].append({**descriptor, "state": "disabled"})
                await self._issue(summary, subject, "media_variants", "downloads_disabled",
                                  f"The {item['constructor']} variant is inventoried but media downloads are disabled.",
                                  {**context, "variant": descriptor})
                continue
            size = item["_size"]
            if size is not None and item["_embedded"] is None and not getattr(size, "size", None) and not getattr(size, "sizes", None):
                summary["media_variants"].append({**descriptor, "state": "metadata_only"})
                await self._issue(summary, subject, "media_variants", "no_downloadable_bytes",
                                  f"The {item['constructor']} object contains structural metadata, not a downloadable file.",
                                  {**context, "variant": descriptor})
                continue
            try:
                receipt = await self._save_variant(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._check()
                from .engine import AuthRequired, friendly_error
                if isinstance(exc, AuthRequired) or self._fatal_storage(exc):
                    raise
                summary["counts"]["variants_failed"] += 1
                summary["media_variants"].append({**descriptor, "state": "failed", "error_type": type(exc).__name__})
                await self._issue(summary, subject, "media_variants", type(exc).__name__, friendly_error(exc),
                                  {**context, "variant": descriptor}, error=True)
                continue
            summary["counts"]["variants_reused" if receipt["reused"] else "variants_downloaded"] += 1
            descriptor.update(receipt, media_file=receipt["path"], state="reused" if receipt["reused"] else "downloaded")
            summary["media_variants"].append(descriptor)
            await self._observe("media_variant_file", subject, descriptor, context)

    def _directories(self):
        current = self.root
        for part in ("telegram_data", "media", "variants", "index"):
            current = current / part
            if current.is_symlink():
                raise ValueError("The media variant folder cannot be a symbolic link.")
            current.mkdir(exist_ok=True)
            if not current.is_dir() or current.is_symlink():
                raise ValueError("The media variant folder is not a safe directory.")
        return current.parent, current

    def _checksum(self, path):
        digest = hashlib.sha256()
        total = 0
        if path.is_symlink():
            raise ValueError("A saved media variant is a symbolic link.")
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                self._check()
                digest.update(chunk)
                total += len(chunk)
        return digest.hexdigest(), total

    def _cached(self, index, directory, key, expected):
        for manifest in index.glob(f"{key}-*.json"):
            self._check()
            if manifest.is_symlink():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                digest = data["sha256"]
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    continue
                recorded_path = data.get("path", "")
                name = Path(recorded_path).name
                if not re.fullmatch(re.escape(digest) + r"(?:-recovered-[0-9a-f]{32})?\.bin", name):
                    continue
                path = directory / name
                if path.relative_to(self.root).as_posix() != recorded_path:
                    continue
                actual, size = self._checksum(path)
                if actual == digest and size > 0 and size == data["size"] and (expected is None or size == expected):
                    return {"path": path.relative_to(self.root).as_posix(), "sha256": digest, "size": size, "reused": True}
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    async def _save_variant(self, item):
        from telethon.tl import types
        directory, index = self._directories()
        expected = item["expected_bytes"]
        key = hashlib.sha256(item["identity"].encode("utf-8")).hexdigest()
        cached = await self._disk(self._cached, index, directory, key, expected)
        self._check()
        if cached:
            return cached
        descriptor = None
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".variant-", suffix=".partial", dir=directory)
            temporary = Path(name)
            with os.fdopen(descriptor, "w+b") as file:
                descriptor = None
                if item["_embedded"] is not None:
                    file.write(item["_embedded"])
                else:
                    obj = item["_object"]
                    location_type = types.InputPhotoFileLocation if isinstance(obj, types.Photo) else types.InputDocumentFileLocation
                    location = location_type(id=obj.id, access_hash=obj.access_hash,
                                             file_reference=obj.file_reference, thumb_size=item["size_type"])
                    async def progress(current, total):
                        self._check()
                    # Reset the temporary file on engine-managed network retries.
                    async def download():
                        self._check()
                        file.seek(0)
                        file.truncate(0)
                        return await self.client.download_file(location, file=file,
                            file_size=expected, dc_id=getattr(obj, "dc_id", None), progress_callback=progress)
                    await self.request(download)
                file.flush()
                os.fsync(file.fileno())
            self._check()
            digest, size = await self._disk(self._checksum, temporary)
            self._check()
            if size <= 0 or expected is not None and size != int(expected):
                raise ValueError("The downloaded media variant is incomplete or has an unexpected size.")
            receipt = await self._disk(self._publish, temporary, directory, index, key, item["identity"], digest, size)
            return {**receipt, "reused": False}
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _publish(self, temporary, directory, index, key, identity, digest, size):
        self._check()
        destination = directory / f"{digest}.bin"
        recovered_from = None
        try:
            os.link(temporary, destination)
        except FileExistsError:
            valid = False
            try:
                actual_digest, actual_size = self._checksum(destination)
                valid = actual_digest == digest and actual_size == size
            except (ValueError, FileNotFoundError):
                pass
            if not valid:
                # Preserve the damaged file and every older receipt. A retry can
                # still publish a verified replacement without overwriting it.
                recovered_from = destination.relative_to(self.root).as_posix()
                destination = directory / f"{digest}-recovered-{uuid.uuid4().hex}.bin"
                os.link(temporary, destination)
        receipt = {"identity": identity, "path": destination.relative_to(self.root).as_posix(),
                   "sha256": digest, "size": size}
        if recovered_from:
            receipt["recovered_from"] = recovered_from
        manifest = index / f"{key}-{digest}.json"
        if manifest.exists() or manifest.is_symlink():
            identical = False
            if not manifest.is_symlink():
                try:
                    identical = json.loads(manifest.read_text()) == receipt
                except (ValueError, OSError):
                    pass
            if not identical:
                manifest = index / f"{key}-{digest}-{uuid.uuid4().hex}.json"
        fd, filename = tempfile.mkstemp(prefix=".index-", suffix=".partial", dir=index)
        staged = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(receipt, file, ensure_ascii=False, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(staged, manifest)
            except FileExistsError:
                if manifest.is_symlink() or json.loads(manifest.read_text()) != receipt:
                    raise StoreError("A media variant manifest changed during publication. Capture stopped without overwriting it.")
            for folder in (directory, index):
                fd = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            staged.unlink(missing_ok=True)
        return receipt
