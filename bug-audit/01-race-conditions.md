# Verified race-condition findings

Audited on 2026-09-19 in `/Users/xxx/GitHub/telegram-scraper`. The reviewed source was the existing working tree at HEAD `188afe9fcfa3f9b1963edc23f5e46e143abd2632`, including changes already present when this audit began. Findings below are against those working-tree contents, not an assertion about the clean commit.

Two race conditions were reproduced using the production control flow, temporary archives, the repository's offline Telegram client fixtures, and explicit synchronization barriers. No live Telegram account, existing archive, installed app, or production source was changed.

## RC-01 — [P2] Watch clears a new message's notification after saving its checkpoint

**Primary location:** [telegram_scraper/engine.py:944](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:944), especially lines 947–952. The producer enqueues the message and sets the notification at lines 919–927; the fallback timer is created at lines 963–965, using the 60-second interval at line 143.

**Trigger:** A new Telegram message or edit finishes its raw capture while Watch is awaiting its outer `_save()`. This is reachable with detailed context capture disabled and a dirty text-only batch: `_process_inner()` sets `_dirty` at line 689, and the outer Watch loop flushes that batch at line 947. Slow checkpoint I/O widens the window.

The failing interleaving is:

1. Watch drains `pending` until it is empty.
2. Watch awaits `_save()`, which sends `Store.save()` to a worker thread.
3. The registered `receive()` callback finishes capturing another post, adds it to `pending`, and calls `wake.set()`.
4. The checkpoint completes. Watch does not inspect `pending` again; it executes `wake.clear()` and announces “Up to date.”
5. `_wait_for_update()` sleeps even though there is already a queued message. That message remains unprocessed until another event sets `wake`, or the periodic catch-up timer expires.

**Verified result:** The raw observation for post 2 existed, but the reading index still contained only post 1 while Watch reported its waiting phase. The timer had **59.63 seconds** remaining. Stopping before that timer left post 2 absent from the saved reading index; its raw evidence remained preserved.

```json
{
  "raw_post_2_saved": true,
  "post_2_in_reading_index": false,
  "catchup_seconds_remaining": 59.63,
  "job_still_running": true,
  "status_after_stop": "cancelled",
  "post_2_in_saved_index_after_stop": false
}
```

A second reproduction supplied a third message after the missed notification. That immediately caused both queued posts to be processed, before the timer was due:

```json
{
  "indexed_after_checkpoint": [1],
  "indexed_after_next_event": [1, 2, 3],
  "catchup_not_due": true
}
```

**Reproduction method:** Load `Client`, `Message`, and `service` from `tests/test_engine.py` with `runpy.run_path()`. Construct `Client([Message(1)])` and the ordinary fixture service, whose `capture_context` setting is false. Wrap only `store.save` with a `threading.Event` barrier before calling the original method. Start `engine.run("watch")`; when the save worker reaches the barrier, await `client.handlers[0][0](SimpleNamespace(message=Message(2)))`. Release the save worker and wait for the normal `phase="watching"` progress report. Inspect `store.records`, `engine.evidence.message_observations(123, 2)`, and `engine._next_catch_up`. The control run then invokes the same handler for `Message(3)`. All files are inside a fresh `TemporaryDirectory`; the watcher, evidence writes, checkpoint method, and timer logic are unchanged.

**Impact:** A received post can be delayed by almost a minute in search, the reading index, and attachment processing while the app says it is up to date. If Watch stops during that interval, a later sync or Watch run is needed to populate the index from Telegram. This finding does **not** claim loss of the already committed raw observation.

**Correction boundary:** Preserve the invariant that Watch cannot enter its wait with a nonempty queue. Clear the notification before inspecting/draining queued work, or recheck the queue after the awaited checkpoint without clearing a notification produced during that await. Add a regression that injects a received event specifically while the checkpoint worker is paused.

## RC-02 — [P2] An accepted job request can start a new scrape after shutdown has drained jobs

**Primary location:** [telegram_scraper/server.py:583](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:583), lines 583–597. Related admission logic is at [server.py:524](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:524), lines 524–550. HTTP handlers are daemon threads at line 610, and CLI shutdown closes the listener before calling `runtime.close()` at [telegram_scraper/cli.py:145](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/cli.py:145).

**Trigger:** A valid `/api/jobs` request has already been accepted when the app begins closing, but its body or handler reaches `start_job()` while `_close()` is awaiting `service.close()`. An existing Telegram connection supplies that asynchronous disconnect window.

The failing interleaving is:

1. A handler accepts an authenticated POST and is waiting to read its JSON body at server line 761.
2. The listening server closes. Its daemon request handler remains active.
3. `_close()` calls `stop_job()`, passes its busy checks, and passes its existing-job wait. It then awaits `service.close()` at line 593.
4. The accepted POST finishes reading its body and invokes `start_job()`. That method checks only `job["running"]`, `auth_busy`, and `library_busy`; no shutdown state prevents admission. It creates a new `_run_job` task and returns success.
5. The new `TelegramService.run()` clears the cancellation event at engine line 994, creates a run receipt, and begins its Telegram request.
6. The earlier disconnect finishes. `_close()` shuts down the default executor and returns without checking or awaiting this newly created job.
7. The public `close()` wrapper proceeds to stop and close the event loop at server lines 599–606. The newly admitted scrape has not completed the normal cancellation/final-receipt path.

**Verified result:** A real `LocalServer` request returned **HTTP 200** after shutdown began. `_close()` then completed while the scrape task remained pending, the job still reported `running`, and its evidence run was still `running`. The disk executor was already shut down, and the new run had cleared the service's cancellation flag:

```json
{
  "http_response": 200,
  "job_running_after_close": true,
  "job_task_done_after_close": false,
  "service_cancel_set_after_close": false,
  "disk_executor_shut_down": true,
  "evidence_run_status": "running"
}
```

**Reproduction method:** Use a real `Runtime`, its writer-lock context, and `LocalServer(("127.0.0.1", 0), runtime)` in a fresh `TemporaryDirectory`. Supply the test `Client` through `runtime.get_service()._client`; override only its asynchronous `disconnect()` and `is_user_authorized()` methods to wait on independent `asyncio.Event` barriers. Configure fixture credentials and a channel, and set the runtime's connection state to authorized. Send a valid job POST's headers through a loopback socket, including the server cookie, CSRF token, and correct `Content-Length`, but hold its body. A wrapper around `Handler.do_POST` signals when the handler has entered. Stop and close the listener, then schedule `runtime._close()`. Once the disconnect barrier is reached, send `{"mode":"sync"}`. Observe HTTP 200 and wait for the new job to enter its authorization request. Release the disconnect barrier; `_close()` returns with the state shown above. The harness inspects the state immediately before the public `close()` wrapper would stop the loop, then explicitly releases and drains its test tasks for cleanup.

The reproduction uses the actual HTTP admission, runtime cleanup, engine startup, and SQLite run creation. The barriers choose the ordering of two normal asynchronous operations; they do not change the application admission checks or teardown logic.

**Impact:** A scrape can be acknowledged as started during shutdown and then abandoned when the loop stops, leaving an unfinished run receipt and unprocessed work. Closing the listener alone does not provide a boundary against already accepted actions. The reproduction establishes incomplete teardown of the new job; it does **not** establish corruption of previously committed records.

**Correction boundary:** Set a runtime closing state before shutdown's first await and reject further mutating operations, including requests accepted before the listener closed. Drain the admitted jobs and operations before shutting down the executor and loop. Cover this with an HTTP-level regression that holds a POST body until disconnect has begun.

## Validation and source identity

- Interpreter: `/usr/local/opt/python@3.14/bin/python3.14`, Python **3.14.5**; Telethon **1.42.0**; pytest **9.0.3**.
- Both findings above were reproduced offline against production control flow. RC-01 also had the independent subsequent-event control shown above.
- A focused selection covering Watch, cancellation draining, channel writer locks, concurrent evidence observations, and slow export readers produced **9 passed, 1 failed, 110 deselected**. The failed test was `test_watch_overflow_rescans_history_without_losing_posts`, which exceeded its existing five-second wait; an isolated retry also timed out.
- A separate invocation of that overflow test, changing only its wait limits in memory, passed all original assertions in **1.69 seconds**. The earlier timeouts remain a validation limitation and are not reported as an additional race or attributed to either finding.
- Test and reproduction data were temporary. Bytecode writes and pytest's cache provider were disabled for test commands. No app rebuild or installation was performed because this was an audit with no app changes.

SHA-256 of the reviewed source files:

| File | SHA-256 |
| --- | --- |
| `telegram_scraper/engine.py` | `21b74741de464a5d62a86426be7c847307dcd280dd5c8861c39c6929dcd15127` |
| `telegram_scraper/server.py` | `6c507efad821a7096cd1f152ceba6c34fd2ecede27b366c1841bb5cac56b899e` |
| `telegram_scraper/cli.py` | `b8c72d51acd277ced739b5737d335a18f0792305f932d3f27854086d952c6d6b` |
| `telegram_scraper/storage.py` | `aa69bef55bab88114acc4f332cf32553f5e6a4aa5a391012e7d043189608af6d` |
| `telegram_scraper/evidence.py` | `d11fd092b3ca52a9e074d6c20b659fa856a13b1ee0d4aa4ff49c60327ac6edee` |
