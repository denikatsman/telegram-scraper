import asyncio
import threading
from types import SimpleNamespace
import pytest
from telethon.tl import types
from telegram_scraper.evidence import EvidenceStore, EvidenceError
from telegram_scraper.engine import UserError
from test_engine import service, Message, Client, ProtocolClient, DAY


class CountMessage(Message):
    def __init__(self, views, text="same", edit_date=None):
        super().__init__(1, text=text, edit_date=edit_date)
        self.views = views
    def to_dict(self):
        return {**super().to_dict(), "views": self.views}


@pytest.mark.parametrize("live_after_start", [False, True])
@pytest.mark.parametrize("hold_raw", [False, True])
def test_actual_history_acquisition_orders_equal_version_counters(tmp_path, live_after_start, hold_raw):
    async def run():
        page_started, page_release, watching = asyncio.Event(), asyncio.Event(), asyncio.Event()
        raw_started, raw_release = threading.Event(), threading.Event()
        class Evidence(EvidenceStore):
            def append_observation(self, *args, **kwargs):
                if hold_raw and (kwargs.get("context") or {}).get("source") == "watch":
                    raw_started.set()
                    assert raw_release.wait(5)
                return super().append_observation(*args, **kwargs)
        class Delayed(ProtocolClient):
            async def __call__(self, request):
                result = await super().__call__(request)
                if type(request).__name__ == "GetHistoryRequest" and request.limit != 1 and request.offset_id == 0:
                    page_started.set()
                    await page_release.wait()
                return result
        client = Delayed([CountMessage(1 if live_after_start else 9)])
        engine, store, _ = service(tmp_path, client, report=lambda v: watching.set() if v.get("phase") == "watching" else None)
        engine.evidence = Evidence(tmp_path)
        engine.CATCH_UP_INTERVAL = 3600
        receiver = None
        async def live():
            nonlocal receiver
            receiver = asyncio.create_task(client.handlers[0][0](SimpleNamespace(message=CountMessage(9 if live_after_start else 1))))
            if hold_raw:
                assert await asyncio.to_thread(raw_started.wait, 5)
            else:
                await receiver
        if not live_after_start:
            client.after_anchor = live
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(page_started.wait(), 5)
        if live_after_start:
            await live()
        page_release.set()
        if hold_raw:
            await asyncio.sleep(0)
            raw_release.set()
        await asyncio.wait_for(watching.wait(), 5)
        assert store.records[1]["raw"]["views"] == 9
        observations = engine.evidence.message_observations(123, 1)
        assert {o["payload"]["views"] for o in observations} == {1, 9}
        assert next(o for o in observations if o["observation_id"] == store.records[1]["source_observation_id"])["payload"]["views"] == 9
        engine.cancel()
        await asyncio.wait_for(task, 5)
    asyncio.run(run())


def test_reconciliation_overlap_is_bounded_and_preserves_new_live(tmp_path):
    async def run():
        reread_started, reread_release, watching = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Reread(ProtocolClient):
            calls = 0
            async def get_messages(self, entity, ids):
                self.calls += 1
                reread_started.set()
                await reread_release.wait()
                return CountMessage(2)
        client = Reread([CountMessage(1)])
        async def live():
            await client.handlers[0][0](SimpleNamespace(message=CountMessage(9)))
        # Emit during a page request, after the request-start marker.
        base = client.__class__.__call__
        async def request(self, req):
            result = await base(self, req)
            if type(req).__name__ == "GetHistoryRequest" and req.limit != 1 and req.offset_id == 0:
                await live()
            return result
        Reread.__call__ = request
        engine, store, _ = service(tmp_path, client, report=lambda v: watching.set() if v.get("phase") == "watching" else None)
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(reread_started.wait(), 5)
        await client.handlers[0][0](SimpleNamespace(message=CountMessage(12)))
        reread_release.set()
        await asyncio.wait_for(watching.wait(), 5)
        assert store.records[1]["raw"]["views"] == 12
        assert client.calls == 1
        assert any(i["stage"] == "watch_ordering" for i in engine._issues)
        engine.cancel()
        await task
    asyncio.run(run())


def test_enqueue_during_checkpoint_is_processed_without_another_wakeup(tmp_path):
    async def run():
        watching, queued = asyncio.Event(), False
        client = Client([Message(1)])
        engine, store, _ = service(tmp_path, client, report=lambda v: watching.set() if v.get("phase") == "watching" else None)
        real_save = engine._save
        async def checkpoint():
            nonlocal queued
            if engine._dirty and not queued:
                queued = True
                await client.handlers[0][0](SimpleNamespace(message=Message(2)))
            await real_save()
        engine._save = checkpoint
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 5)
        assert 2 in store.records
        engine.cancel()
        await task
    asyncio.run(run())


def test_watch_owns_one_disconnection_wrapper_across_100_wakes(tmp_path):
    async def run():
        class Disconnected(Client):
            reads = 0
            def __init__(self):
                super().__init__()
                self.future = asyncio.get_running_loop().create_future()
            @property
            def disconnected(self):
                self.reads += 1
                return asyncio.shield(self.future)
        client = Disconnected()
        engine, _, _ = service(tmp_path, client)
        actual = engine._wait_for_update
        calls = 0
        async def cycles(client, wake):
            nonlocal calls
            calls += 1
            if calls >= 100:
                engine.cancel()
            wake.set()
            await actual(client, wake)
        engine._wait_for_update = cycles
        for _ in range(2):
            calls = 0
            assert (await engine.run("watch"))["status"] == "cancelled"
            await asyncio.sleep(0)
            assert not client.future.cancelled()
            assert len(client.future._callbacks or []) <= 1  # asyncio's shared exception logger
        assert client.reads == 2
    asyncio.run(run())


def test_watch_raw_failure_during_context_is_error_not_stop(tmp_path, monkeypatch):
    from telegram_scraper.enrichment import ContextCollector
    async def run():
        class Broken(EvidenceStore):
            def append_observation(self, *args, **kwargs):
                if (kwargs.get("context") or {}).get("source") == "watch":
                    raise EvidenceError("synthetic raw append failure")
                return super().append_observation(*args, **kwargs)
        client = ProtocolClient([Message(1)])
        engine, _, _ = service(tmp_path, client, capture_context=True)
        engine.evidence = Broken(tmp_path)
        async def context(self, entity, message, **kwargs):
            await client.handlers[0][0](SimpleNamespace(message=Message(2)))
            self._check()
        monkeypatch.setattr(ContextCollector, "capture_message", context)
        with pytest.raises(UserError, match="synthetic raw append failure"):
            await engine.run("watch")
        receipt = engine.evidence.runs()[0]
        assert receipt["status"] == "error" and "raw append" in receipt["context"]["failure"]
    asyncio.run(run())


def test_protocol_sequence_only_orders_comparable_live_streams():
    from telegram_scraper.engine import TelegramService
    newer = TelegramService._newer
    raw = {"edit_date": None, "views": 1}
    prior = {"edit_date": None, "views": 9}
    left = {"source": "watch", "scope": "channel:1", "pts": 20, "ordinal": 1}
    right = {"source": "watch", "scope": "channel:1", "pts": 10, "ordinal": 2}
    assert newer(raw, left, prior, right) == (True, False)
    assert newer(raw, left, prior, {**right, "scope": "channel:2"}) == (False, True)
    assert newer(raw, {**left, "source": "history"}, prior, right) == (False, True)
    assert newer({**raw, "edit_date": DAY.isoformat()}, left, prior, right)[0]


def test_history_retry_takes_a_new_start_boundary_after_live_counter(tmp_path):
    async def run():
        watching = asyncio.Event()
        class Retried(ProtocolClient):
            failed = False
            async def __call__(self, request):
                if type(request).__name__ == "GetHistoryRequest" and request.limit != 1 and not self.failed:
                    self.failed = True
                    await self.handlers[0][0](SimpleNamespace(message=CountMessage(9)))
                    raise ConnectionError("synthetic retry")
                return await super().__call__(request)
        client = Retried([CountMessage(1)])
        engine, store, _ = service(tmp_path, client, report=lambda v: watching.set() if v.get("phase") == "watching" else None)
        engine.RETRY_DELAYS = (0,)
        task = asyncio.create_task(engine.run("watch"))
        await asyncio.wait_for(watching.wait(), 5)
        assert store.records[1]["raw"]["views"] == 1  # do not synthesize max counters
        rows = engine.evidence.message_observations(123, 1)
        orders = {r["payload"]["views"]: r["occurrences"][0]["context"]["acquisition"]["ordinal"] for r in rows}
        assert orders[1] > orders[9]
        engine.cancel()
        await task
    asyncio.run(run())
