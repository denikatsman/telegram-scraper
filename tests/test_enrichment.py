"""Offline TL fixtures for linked context and immutable media variants."""
import asyncio
import errno
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

import pytest
from telethon.tl import types
from telethon.errors import ChannelPrivateError

from telegram_scraper.enrichment import ContextCollector
from telegram_scraper.engine import _json_value
from telegram_scraper.storage import StoreError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def channel(id=10, group=False):
    return types.Channel(id=id, title="Fixture channel", photo=types.ChatPhotoEmpty(),
                         date=NOW, broadcast=not group, megagroup=group, access_hash=123)


def message(id=1, *, peer=10, **kwargs):
    return types.Message(id=id, peer_id=types.PeerChannel(peer), date=NOW,
                         message="Fixture post", **kwargs)


def page(messages=(), chats=(), users=()):
    return types.messages.Messages(messages=list(messages), topics=[], chats=list(chats), users=list(users))


def updates():
    return types.Updates(updates=[], users=[], chats=[], date=NOW, seq=1)


def document(id=100, size=4, **kwargs):
    return types.Document(id=id, access_hash=900, file_reference=b"\x00\xffref", date=NOW,
                          mime_type="video/mp4", size=size, dc_id=2, attributes=[], **kwargs)


def photo(id=200, sizes=()):
    return types.Photo(id=id, access_hash=901, file_reference=b"photoref", date=NOW,
                       sizes=list(sizes), dc_id=2)


class Client:
    def __init__(self):
        self.responses = defaultdict(deque)
        self.calls = []
        self.payloads = {}
        self.downloads = []
        self.download_hook = None

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append(request)
        if not self.responses[name]:
            raise AssertionError(f"Unexpected fixture request: {name}")
        result = self.responses[name].popleft()
        if isinstance(result, Exception):
            raise result
        return result

    async def download_file(self, location, *, file, file_size=None, dc_id=None, progress_callback=None):
        key = (location.id, location.thumb_size)
        self.downloads.append(key)
        payload = self.payloads[key]
        if isinstance(payload, Exception):
            raise payload
        file.write(payload)
        if self.download_hook:
            self.download_hook()
        if progress_callback:
            await progress_callback(len(payload), file_size or len(payload))
        return file


def collector(tmp_path, client=None, *, download=True, cancelled=lambda: False, recorder=None):
    client = client or Client()
    observations = []

    async def record(kind, subject, value, context=None):
        binary = bytes(value) if hasattr(value, "to_dict") else None
        observations.append({"kind": kind, "subject": subject, "value": value,
                             "payload": _json_value(value), "tl_bytes": binary,
                             "context": context})

    async def request(factory):
        return await factory()
    return ContextCollector(client, request, recorder or record, cancelled, download, tmp_path), client, observations


def test_plain_message_does_not_make_unrelated_requests(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        result = await capture.capture_message(channel(), message())
        assert result["status"] == "complete"
        assert client.calls == [] and client.downloads == []
        assert rows[-1]["kind"] == "message_context_receipt"
        assert rows[-1]["context"]["parent_channel_id"] == 10
    asyncio.run(run())


def test_comments_continue_after_short_page_preserve_tl_peers_and_download_primary(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        parent = message(replies=types.MessageReplies(replies=3, replies_pts=1, comments=True, channel_id=20, max_id=90))
        attachment = types.MessageMediaDocument(document=document(500))
        newest = message(90, peer=20, media=attachment, from_id=types.PeerUser(88))
        first = page([newest, message(80, peer=20)], [channel(20, True)], [types.User(88, first_name="Fixture author")])
        client.responses["GetRepliesRequest"].extend([first, page([message(70, peer=20)]), page()])
        client.payloads[(500, "")] = b"test"
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "complete"
        assert result["counts"]["comments"] == 3
        assert result["counts"]["comment_pages"] == 3
        assert [r.offset_id for r in client.calls] == [0, 80, 70]
        assert all(r.max_id == 91 and r.limit == 100 for r in client.calls)
        comment_row = next(row for row in rows if row["kind"] == "comment_page")
        assert comment_row["value"] is first and comment_row["tl_bytes"] == bytes(first)
        assert comment_row["payload"]["users"][0]["first_name"] == "Fixture author"
        assert comment_row["payload"]["messages"][0]["from_id"]["user_id"] == 88
        variant = result["media_variants"][0]
        assert variant["context"]["parent_message_id"] == 1
        assert variant["context"]["message_id"] == 90
        assert variant["context"]["peer_id"] == 20
        assert (tmp_path / variant["media_file"]).read_bytes() == b"test"
    asyncio.run(run())


def test_repeated_comment_page_records_partial_instead_of_looping(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        parent = message(replies=types.MessageReplies(replies=4, replies_pts=1, comments=True))
        repeated = page([message(20, peer=20), message(10, peer=20)])
        client.responses["GetRepliesRequest"].extend([repeated, repeated])
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "partial"
        assert result["counts"]["comments"] == 2
        assert len(client.calls) == 2
        assert result["errors"][0]["code"] == "repeated_page"
    asyncio.run(run())


def test_comment_access_failure_is_recorded_and_primary_variants_still_capture(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        parent = message(replies=types.MessageReplies(replies=1, replies_pts=1),
                         media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoCachedSize("s", 10, 10, b"jpeg")])))
        client.responses["GetRepliesRequest"].append(ChannelPrivateError(None))
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "partial"
        assert result["errors"][0]["area"] == "comments"
        assert "private fixture path" not in result["errors"][0]["message"]
        assert result["counts"]["variants_downloaded"] == 1
    asyncio.run(run())


def poll_message(public=True):
    return message(media=types.MessageMediaPoll(
        poll=types.Poll(id=11, question=types.TextWithEntities("Question", []),
                        answers=[types.PollAnswer(types.TextWithEntities("A", []), b"\x00")], public_voters=public),
        results=types.PollResults(total_voters=2)))


def test_public_poll_pages_all_votes_and_preserves_binary_options(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        client.responses["GetPollResultsRequest"].append(updates())
        first = types.messages.VotesList(count=2, votes=[types.MessagePeerVote(types.PeerUser(1), b"\x00\xff", NOW)],
                                         chats=[], users=[types.User(1)], next_offset="next")
        last = types.messages.VotesList(count=2, votes=[types.MessagePeerVote(types.PeerUser(2), b"\x01", NOW)], chats=[], users=[])
        client.responses["GetPollVotesRequest"].extend([first, last])
        result = await capture.capture_message(channel(), poll_message())
        assert result["status"] == "complete" and result["counts"]["poll_voters"] == 2
        requests = [r for r in client.calls if type(r).__name__ == "GetPollVotesRequest"]
        assert [r.offset for r in requests] == [None, "next"]
        assert all(r.option is None for r in requests)
        row = next(r for r in rows if r["kind"] == "poll_voter_page")
        assert row["tl_bytes"] == bytes(first)
        assert row["payload"]["votes"][0]["option"] == {"encoding": "base64", "data": "AP8="}
    asyncio.run(run())


def test_anonymous_poll_does_not_request_voter_identities(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        client.responses["GetPollResultsRequest"].append(updates())
        result = await capture.capture_message(channel(), poll_message(False))
        assert [type(r).__name__ for r in client.calls] == ["GetPollResultsRequest"]
        assert result["limitations"][0]["code"] == "anonymous_poll"
    asyncio.run(run())


def test_poll_repeated_cursor_stops_with_partial_status(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        client.responses["GetPollResultsRequest"].append(updates())
        repeated = types.messages.VotesList(9, [types.MessagePeerVote(types.PeerUser(1), b"a", NOW)], [], [], "same")
        client.responses["GetPollVotesRequest"].extend([repeated, repeated])
        result = await capture.capture_message(channel(), poll_message())
        assert result["status"] == "partial" and result["errors"][0]["code"] == "repeated_cursor"
    asyncio.run(run())


def reacting_message(can_see=True):
    return message(reactions=types.MessageReactions(
        results=[types.ReactionCount(types.ReactionEmoji("👍"), 2)], can_see_list=can_see))


def test_channel_reaction_totals_do_not_imply_identity_access(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        client.responses["GetMessagesReactionsRequest"].append(updates())
        result = await capture.capture_message(channel(), reacting_message())
        assert [type(r).__name__ for r in client.calls] == ["GetMessagesReactionsRequest"]
        assert result["limitations"][0]["area"] == "reaction_identities"
    asyncio.run(run())


def test_group_reaction_identity_pages_until_last_cursor(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        client.responses["GetMessagesReactionsRequest"].append(updates())
        reaction = types.MessagePeerReaction(types.PeerUser(1), NOW, types.ReactionEmoji("👍"))
        first = types.messages.MessageReactionsList(2, [reaction], [], [types.User(1)], "page2")
        second = types.messages.MessageReactionsList(2, [reaction], [], [])
        client.responses["GetMessageReactionsListRequest"].extend([first, second])
        result = await capture.capture_message(channel(group=True), reacting_message())
        assert result["status"] == "complete" and result["counts"]["reaction_pages"] == 2
        assert result["counts"]["reaction_entries"] == 2
        assert len([r for r in rows if r["kind"] == "reaction_identity_page"]) == 2
    asyncio.run(run())


def test_media_variants_download_alternates_thumbs_cover_and_reuse_hashes(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        main = document(1, thumbs=[types.PhotoSize("s", 10, 10, 3)])
        alternate = document(2, size=5)
        cover = photo(3, [types.PhotoSizeProgressive("p", 30, 30, [2, 6]), types.PhotoStrippedSize("i", b"rawstripped")])
        parent = message(media=types.MessageMediaDocument(document=main, alt_documents=[alternate], video_cover=cover))
        client.payloads = {(1, "s"): b"abc", (2, ""): b"video", (3, "p"): b"abcdef"}
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "complete"
        assert result["counts"]["variants_discovered"] == 5
        assert result["counts"]["variants_downloaded"] == 4
        assert (1, "") not in client.downloads
        for item in result["media_variants"]:
            if item["state"] == "primary_archive":
                continue
            file = tmp_path / item["media_file"]
            assert file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
        count = len(client.downloads)
        repeated = await capture.capture_message(channel(), parent)
        assert repeated["counts"]["variants_reused"] == 4
        assert len(client.downloads) == count
        assert not list((tmp_path / "telegram_data/media/variants").glob("*.partial"))
    asyncio.run(run())


def test_disabled_media_preserves_inventory_and_explicit_skips(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path, download=False)
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoSize("x", 100, 100, 5)])))
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "partial" and not client.downloads
        assert result["media_variants"][0]["state"] == "disabled"
        assert next(r for r in rows if r["kind"] == "media_variant_inventory")["payload"]["variants"][0]["expected_bytes"] == 5
        assert not (tmp_path / "telegram_data").exists()
    asyncio.run(run())


def test_incomplete_variant_never_publishes_and_reports_failure(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoSize("x", 100, 100, 5)])))
        client.payloads[(200, "x")] = b"bad"
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "partial" and result["counts"]["variants_failed"] == 1
        assert not list((tmp_path / "telegram_data/media/variants").glob("*.bin"))
        assert not list((tmp_path / "telegram_data/media/variants").glob("*.partial"))
    asyncio.run(run())


def test_cancellation_cleans_partial_file_and_is_not_a_success_receipt(tmp_path):
    async def run():
        cancelled = False
        def stop():
            nonlocal cancelled
            cancelled = True
        capture, client, rows = collector(tmp_path, cancelled=lambda: cancelled)
        client.download_hook = stop
        client.payloads[(200, "x")] = b"abcde"
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoSize("x", 100, 100, 5)])))
        with pytest.raises(asyncio.CancelledError):
            await capture.capture_message(channel(), parent)
        assert not list((tmp_path / "telegram_data/media/variants").glob("*.partial"))
        assert not any(r["kind"] == "message_context_receipt" for r in rows)
    asyncio.run(run())


def test_symlink_variant_directory_is_rejected_without_outside_write(tmp_path):
    async def run():
        outside = tmp_path / "outside"
        outside.mkdir()
        media = tmp_path / "telegram_data/media"
        media.mkdir(parents=True)
        (media / "variants").symlink_to(outside, target_is_directory=True)
        capture, client, _ = collector(tmp_path)
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoCachedSize("s", 10, 10, b"jpeg")])))
        result = await capture.capture_message(channel(), parent)
        assert result["status"] == "partial" and not list(outside.iterdir())
        assert result["counts"]["variants_failed"] == 1
    asyncio.run(run())


def test_corrupt_file_is_preserved_and_retry_publishes_verified_replacement(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoCachedSize("s", 10, 10, b"jpeg")])))
        first = await capture.capture_message(channel(), parent)
        path = tmp_path / first["media_variants"][0]["media_file"]
        path.write_bytes(b"corrupt")
        second = await capture.capture_message(channel(), parent)
        assert second["status"] == "complete" and second["counts"]["variants_downloaded"] == 1
        assert path.read_bytes() == b"corrupt"
        repaired = tmp_path / second["media_variants"][0]["media_file"]
        assert repaired != path and repaired.read_bytes() == b"jpeg"
        assert "-recovered-" in repaired.name
        third = await capture.capture_message(channel(), parent)
        assert third["counts"]["variants_reused"] == 1
        assert third["media_variants"][0]["media_file"] == repaired.relative_to(tmp_path).as_posix()
        assert not list(path.parent.glob("*.partial"))
    asyncio.run(run())


def test_observation_storage_failure_does_not_return_complete(tmp_path):
    async def run():
        async def failing_record(*args, **kwargs):
            raise OSError("fixture storage failure")
        capture, _, _ = collector(tmp_path, recorder=failing_record)
        with pytest.raises(StoreError):
            await capture.capture_message(channel(), message())
    asyncio.run(run())


def test_raw_page_storage_failure_is_fatal_even_if_limitation_record_would_work(tmp_path):
    async def run():
        calls = []
        async def failing_record(kind, *args, **kwargs):
            calls.append(kind)
            if kind == "comment_page":
                raise StoreError("fixture observation commit failed")
        capture, client, _ = collector(tmp_path, recorder=failing_record)
        client.responses["GetRepliesRequest"].append(page())
        with pytest.raises(StoreError):
            await capture.capture_message(channel(), message(replies=types.MessageReplies(1, 1)))
        assert calls == ["comment_page"]
    asyncio.run(run())


def test_disk_full_is_fatal_and_partial_download_is_removed(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        parent = message(media=types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoSize("x", 100, 100, 5)])))
        client.payloads[(200, "x")] = OSError(errno.ENOSPC, "fixture disk full")
        with pytest.raises(OSError) as exc:
            await capture.capture_message(channel(), parent)
        assert exc.value.errno == errno.ENOSPC
        assert not list((tmp_path / "telegram_data/media/variants").glob("*.partial"))
    asyncio.run(run())


def test_cancelling_disk_work_waits_for_worker_before_returning(tmp_path):
    async def run():
        capture, _, _ = collector(tmp_path)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def work():
            entered.set()
            release.wait(5)
            finished.set()
        operation = asyncio.create_task(capture._disk(work))
        await asyncio.to_thread(entered.wait, 3)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and not finished.is_set()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished.is_set()
    asyncio.run(run())


def test_channel_photo_sizes_are_saved_with_channel_scope_and_no_message_id(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        result = await capture.capture_channel_photo(channel(), photo(sizes=[types.PhotoCachedSize("s", 8, 8, b"jpeg")]))
        assert result["status"] == "complete" and result["counts"]["variants_downloaded"] == 1
        assert result["media_variants"][0]["context"]["parent_message_id"] is None
        assert result["media_variants"][0]["context"]["relation"] == "channel_profile_photo"
        assert any(row["kind"] == "channel_profile_photo" and row["tl_bytes"] for row in rows)
        assert client.calls == []
    asyncio.run(run())


def test_absent_channel_photo_is_recorded_as_absent_without_false_failure(tmp_path):
    async def run():
        capture, client, rows = collector(tmp_path)
        result = await capture.capture_channel_photo(channel(), types.PhotoEmpty(0))
        assert result["status"] == "complete" and not result["limitations"]
        assert rows[0]["kind"] == "channel_profile_photo" and rows[0]["tl_bytes"]
        assert not client.calls and not client.downloads
    asyncio.run(run())


def test_unsupported_media_is_not_silently_reported_as_complete(tmp_path):
    async def run():
        capture, client, _ = collector(tmp_path)
        result = await capture.capture_message(channel(), message(media=types.MessageMediaUnsupported()))
        assert result["status"] == "partial" and result["limitations"][0]["code"] == "unsupported_media"
        assert not client.downloads
    asyncio.run(run())
