# Error-handling and recovery audit

Four verified findings in the current working tree, audited on 2026-09-19. The checkout is based on `188afe9fcfa3f9b1963edc23f5e46e143abd2632` and includes the pre-existing uncommitted changes recorded at audit launch. All 49 files in `bug-audit/launch-20260919T063437Z/source-baseline.json` still matched their recorded SHA-256 values after verification.

Reproductions used Python 3.14.5, Telethon 1.42.0, disposable libraries, and offline Telegram fixtures. HTTP checks used a disposable loopback server. No production source, installed app, real archive, account, or other audit report was changed.

## 08-01 — [P1] Backup creation silently omits unreadable directories and publishes the incomplete ZIP

**Location:** [storage.py:346](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:346), first traversal at lines 346–352; the same omission affects the verification traversal at lines 388–397. The ZIP is published at lines 402–408.

Both calls to `os.walk(self.data_dir, followlinks=False)` omit `onerror`. Consequently, a directory-enumeration `PermissionError` or other `OSError` is swallowed by `os.walk`: that subtree contributes no files, and `snapshot()` continues. The second traversal silently omits the same subtree, so `current_files == set(source_stats)` still passes. A completed `.zip` is returned even though a subtree could not be inspected or backed up.

This defeats the recovery snapshot that `TelegramService.run()` takes before syncing at [engine.py:1037](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1037). In particular, historical or otherwise unreferenced attachments can be omitted without the subsequent archive check identifying the missing coverage. Their bytes may be the only remaining local copy of earlier media.

**Verified reproduction:** In a temporary library, save one text post and put a file at `telegram_data/media/preserved-old-attachments/old-video.mp4`. Remove directory access with `chmod(0)` on `preserved-old-attachments`, confirm that direct enumeration raises `PermissionError`, then call the actual `Store.snapshot()` and `Store.verify()`. Restore permissions in `finally` before removing the temporary library. No filesystem failure was mocked; this ran as macOS user ID 501.

Observed result:

```json
{
  "directory_read": "PermissionError",
  "members": ["messages_all.json"],
  "verify_ok": true,
  "verify_issues": []
}
```

The snapshot returned a normal `telegram_data_archive_PRE_SYNC_*.zip` path. The media file was absent from the ZIP, and no backup error was raised. The successful verification result above applies to the reproduced case where that older attachment is not referenced by the current post index or an earlier backup; the snapshot omission itself also occurs for referenced attachments.

**Required behavior:** Propagate directory traversal errors from both walks and fail the snapshot before publishing it. An unreadable subtree must not be treated as an empty subtree.

## 08-02 — [P2] A file-read error after HTTP headers can become a successful, corrupted download

**Location:** [server.py:738](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:738), GET exception responses at lines 738–741; [server.py:846](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:846), committed response headers and streaming reads at lines 846–855.

`send_file()` sends HTTP 200/206 and `Content-Length` before reading the file body. If `file.read()` subsequently raises an `OSError`, `do_GET()` catches it and calls `self.error(..., 500)`. That writes a second HTTP status line, headers, and JSON onto the same connection after the first response has already started. Those bytes are part of the original download body from the client's perspective; they cannot change its original HTTP status.

When the bytes still owed by the first response fit inside the appended error response, the client receives the entire declared length without any truncated-response exception. It accepts HTTP 200 while saving corrupted bytes. This affects media, post-index exports, and prepared source exports because they share `send_file()`. The post export UI checks `response.ok` and then accepts the body as a blob at [app.js:1068](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:1068); it does not detect this corruption.

**Verified reproduction:** Serve an actual temporary media file containing `1,048,676` bytes. Wrap only that file's `Path.open("rb")` result so the first read returns the normal 1 MiB chunk and the second read raises `OSError(errno.EIO, ...)`. Request `/api/media/1` with the disposable server's session cookie using `http.client.HTTPConnection`, then call `response.read()` normally.

Observed result:

```json
{
  "status": 200,
  "declared_bytes": 1048676,
  "received_bytes": 1048676,
  "matches_source": false,
  "corrupt_tail_prefix": "HTTP/1.0 500 Internal Server Error\r\n"
}
```

`response.read()` returned successfully. The final 100 bytes of the apparent media download were the beginning of the server's second HTTP response. The source file remained unchanged.

**Required behavior:** After response headers have been committed, terminate the failed transfer instead of sending another HTTP response into its body. Failures before headers may still return a structured error response.

## 08-03 — [P2] A Watch evidence-write failure is downgraded to a user cancellation during related-context capture

**Location:** [engine.py:1047](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1047), collector cancellation callback; [enrichment.py:43](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:43), `_check()` at lines 43–45; [engine.py:1058](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1058), terminal exception classification at lines 1058–1067.

The Watch receiver catches a failed raw-observation write, stores the actual exception in `self._watch_failure`, and sets `self._cancel` at [engine.py:911](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:911). The engine's own `_check_cancel()` gives that stored exception priority over cancellation. However, `ContextCollector` receives only `self._cancel.is_set`, so its `_check()` converts the same signal into an ordinary `asyncio.CancelledError` without consulting the failure.

If an incoming Watch observation fails while the collector is awaiting a disk-backed context observation, the next collector cancellation check takes this path. `run()` then records `status="cancelled"`, `stopped=true`, and `failure=null`; it returns normally. The cause of the failed observation is absent from the durable run issues and failure field, and the final message says the operation stopped with captured information saved. No user Stop action is necessary.

**Verified reproduction:** Use the existing `ProtocolClient`, `Message`, and `service` fixtures from `tests/test_engine.py`, with related-context capture enabled and a message containing a document thumbnail. Set `download_media=False` to avoid any media network activity. Use an `EvidenceStore` subclass that:

1. Pauses the current history post's `media_variant_inventory` append using a thread event.
2. Raises `EvidenceError("Injected watch observation could not be committed")` only for a new message observation whose context has `source="watch"`.

Start `engine.run("watch")`. Once the inventory append is paused, invoke the actually registered Watch callback with message 2, wait for it to store `_watch_failure`, and release the inventory append. The real collector then reaches its next `_check()`. No call to `engine.cancel()` is made.

Observed result from the engine and the real SQLite run receipt:

```json
{
  "stored_exception": "Injected watch observation could not be committed",
  "returned": "cancelled",
  "durable_status": "cancelled",
  "context": {"failure": null, "stopped": true},
  "issues": [],
  "new_watch_post_saved": false
}
```

The final message was: `Stopped. Captured observations, completed posts and downloads are saved; this run is incomplete.` The incoming post had no saved raw observation. This finding concerns loss of the failure cause and incorrect terminal reporting; the overlap with asynchronous execution is necessary to reach that error path.

**Required behavior:** Preserve `_watch_failure` through the collector's cancellation checks or reclassify cancellation against that failure before finalizing the run. The durable receipt and app must report the evidence-write error.

## 08-04 — [P2] Corrupt DEFLATE data escapes the per-backup handler and aborts the whole archive check

**Location:** [storage.py:597](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:597), per-backup exception handler at lines 597–598; compressed member reads at lines 549–556. The outer handler at lines 627–631 does not catch this error either.

`Store.verify()` reads every ZIP member to detect damaged backups, but its exception list does not include `zlib.error`. Invalid DEFLATE bytes raise that exception directly; it does not inherit from any of the listed `OSError`, `ValueError`, `RuntimeError`, `EOFError`, or ZIP exception classes. Consequently, this ordinary backup-corruption case escapes `verify()` instead of becoming an issue associated with that backup.

The remaining backups are never checked, and the structured verification result is discarded. Through the app, `_run_job()` catches the exception generically at [server.py:566](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:566), reports a connection/disk-space troubleshooting message, and provides no verification result identifying the damaged archive.

**Verified reproduction:** Create two valid ZIPs, `01-corrupt.zip` and `02-good.zip`, using `ZIP_DEFLATED` and the same valid `messages_all.json`. In the first ZIP, locate the start of the first member's compressed bytes from the local header (`30 + filename_length + extra_length`) and set the first byte's DEFLATE block-type bits to the reserved value (`byte |= 6`). Call the actual `Store.verify(report=...)`.

Observed result:

```json
{
  "exception": "zlib.error",
  "message": "Error -3 while decompressing data: invalid block type",
  "later_good_backup_checked": false
}
```

The progress callback reached `Checking backup 1 of 2: 01-corrupt.zip` and never reached the second ZIP. Running the same library through the real `Runtime.start_job({"mode": "verify"})` produced:

```json
{
  "status": "error",
  "running": false,
  "has_verification_result": false,
  "message": "The operation could not finish. Your saved archive is kept. Check your connection and available disk space, then retry."
}
```

**Required behavior:** Catch decompressor corruption errors at the per-backup boundary, retain an issue naming the corrupt ZIP, and continue checking independent backups.

## Verification context

Each finding above was reproduced against the unchanged production implementation. The existing focused checks below also passed: **17 passed, 104 deselected**. Those passing checks cover adjacent behavior; the reproductions above exercise cases they do not cover.

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/local/opt/python@3.14/bin/python3.14 -m pytest -q -p no:cacheprovider \
  tests/test_storage.py tests/test_app.py tests/test_engine.py tests/test_enrichment.py \
  -k 'snapshot or crc_damage or verification or media_streaming_ranges or export_is_download or watch_stop_drains or cancellation_cleans_partial or raw_history_exhausted_retries or raw_persistence_failure or observation_storage_failure'
```
