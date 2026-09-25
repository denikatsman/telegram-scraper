**Partial-failure and atomicity audit — verified findings**

Audited on 2026-09-19 against the current working tree, including its pre-existing uncommitted changes; base commit `188afe9fcfa3f9b1963edc23f5e46e143abd2632`. All four findings below were reproduced. Production code, the user's archive, Telegram account, and installed app were not changed. The reproductions used disposable directories, offline Telegram fixtures, and, for the installer, signed disposable app bundles under a temporary home.

**04-01 · [P1] Stopping the first channel-photo download leaves a library that subsequent syncs refuse to open**

Code: [engine.py:1049](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1049) (photo capture precedes history), [enrichment.py:430](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:430) and [enrichment.py:477](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:477) (directory creation and download), [storage.py:168](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:168) (subsequent load refusal).

On a new channel archive with the default context/media settings, `TelegramService.run()` captures the profile photo before processing any history messages. `_save_variant()` creates `telegram_data/media/variants/index` before beginning the transfer. If the user stops that transfer, its temporary file is removed, but these directories remain. No message has been upserted, so `_dirty` is still false and the final `_save()` at `engine.py:1070` does not create `messages_all.json`.

The next run calls `Store.load()`. Its missing-index guard treats **any entry** in `media/`, including the empty `variants` directory created by this same interrupted operation, as orphaned saved media. It raises an error instructing the user to restore a message index that never existed. Reopening the app does not recover: `Runtime.__init__()` records the same `library_error`, and `start_job()` rejects subsequent capture jobs at `server.py:530–531`.

Reproduction performed:

1. Create a fresh temporary root and a real `TelegramService` with `capture_context=True` and `download_media=True`.
2. Supply an offline client returning a concrete Telethon `Channel` and a full-channel response containing a `Photo` with one four-byte `PhotoSize`.
3. Inside the fixture's `download_file()`, write four bytes, call `service.cancel()`, and invoke the supplied progress callback. The normal cancellation path returns `status="cancelled"`.
4. Disable that cancellation hook and run sync again; also construct a new `Runtime` for the same root.

Observed and asserted:

```text
first_status: cancelled
messages_index_exists: false
remaining_media_entries:
  telegram_data/media/variants
  telegram_data/media/variants/index
remaining_download_files: none
history_calls: 0
retry_error: Saved media exists but messages_all.json is missing. Restore the message file before syncing.
reopened_library_error: Saved media exists but messages_all.json is missing. Restore the message file before syncing.
```

This is a failed initialization transaction across the message index and media directory tree. An ordinary Stop operation prevents further capture in a previously usable new archive until its files are manually repaired.

**04-02 · [P2] A failure after catalogue replacement deletes the newly referenced channel directory**

Code: [storage.py:134](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:134) (replacement precedes directory flush), [channels.py:66](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:66) (catalogue memory updates only on return), [channels.py:90](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:90) (unconditional rollback of the new folder).

`Channels.add()` creates the channel folder and then calls `_save()`. `_save()` publishes the catalogue through `_atomic_json()`, which first executes `os.replace(temporary, path)` and then calls `_fsync_directory(path.parent)`. If that directory flush raises an I/O error, the new catalogue is already visible on disk, but `_atomic_json()` reports failure and `Channels._save()` does not update its in-memory values or digest.

`Channels.add()` handles every exception as if the catalogue had not been published and removes the new, still-empty channel folder. The resulting on-disk catalogue references a directory that the failed operation itself deleted. The live catalogue also retains its previous digest, so further mutations fail the external-change check until restart.

Reproduction performed with the real file replacement and only the subsequent flush fault injected:

```python
with patch("telegram_scraper.storage._fsync_directory",
           side_effect=OSError(errno.EIO, "injected directory fsync failure")):
    catalogue.add("@secondchannel", "@original")
```

After catching the expected `StoreError`, the persisted catalogue contained the new channel, the live catalogue did not, and the referenced directory was absent. Constructing a fresh `Channels` instance and adding the same channel again returned its existing key with `added=False`; selecting that key still failed:

```text
disk_catalogue_has_new_channel: true
live_catalogue_has_new_channel: false
referenced_channel_folder_exists: false
readd_after_restart: same_id=true, added=false
select_error: This channel's archive folder is missing. Restore it inside the project before opening it.
```

The rollback does not account for `_atomic_json()` failing after its visible commit point. Retrying or restarting cannot recreate the deleted folder through the channel-add flow. The existing catalogue failure test substitutes a failure for the entire `_atomic_json()` call, before any publication, so it does not exercise this case.

**04-03 · [P2] A failed disconnect commits new API credentials while retaining the client created with the old credentials**

Code: [server.py:487](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:487) (settings commit followed by fallible client shutdown), [engine.py:1099](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1099) (client cleared only after disconnect succeeds), [engine.py:384](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:384) (existing client reused without checking its API identity).

`Runtime._update_settings()` saves the new `api_id`/`api_hash` to disk and updates `Settings.values` before awaiting `self.service.close()`. If disconnect raises, the lines clearing `self.service` and the authorization state are skipped. The settings file now contains the new identity while the retained service and client still use the old one.

Saving the same new credentials again does not repair the transition. `old_identity` is now read from the already-updated settings, so it equals `new_identity`; the retry takes the preference-update branch and merely replaces `service.settings`. The existing Telethon client, whose `api_id` was fixed at construction, is retained. Later `_connected_client()` calls reuse it.

Reproduction performed with a real Telethon 1.42.0 `TelegramClient` and a temporary SQLite session, without connecting to Telegram:

1. Save fixture credentials with `api_id=111`, create a client with that identity, and attach it to the runtime's real `TelegramService`. Set the runtime's simulated connection state to authorized.
2. Patch that session's `close()` to raise `sqlite3.OperationalError("injected disk I/O error")`, then update settings to `api_id=222` and a different valid fixture hash.
3. The real Telethon disconnect path reaches the failing session close. Restore `close()` and submit the same new settings again.

Observed and asserted:

```text
after_failed_update:
  saved_api_id: 222
  service_api_id: 111
  actual_Telethon_client_api_id: 111
  authorized_flag: true
after_successful_retry:
  saved_api_id: 222
  service_api_id: 222
  actual_Telethon_client_api_id: 111
  same_client_retained: true
  authorized_flag: true
```

This is a concrete partial commit of the settings/client replacement operation. A failed save action followed by a successful retry leaves the displayed and persisted credentials different from the credentials used by the client. The injected failure is at a real fallible boundary: the installed Telethon SQLite session commits its database in `close()`.

**04-04 · [P2] A final installation-receipt failure leaves the new app installed while reporting a failed installation**

Code: [install-app.py:150](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:150) (app swap), [install-app.py:171](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:171) (rollback boundary), [install-app.py:185](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:185) (receipt write outside that boundary), [install-app.py:247](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:247) (exception becomes exit status 1).

The installer atomically swaps the candidate into Applications, verifies it, moves the previous app into recovery, and removes the staging directory inside its rollback-protected block. It then writes the final `update.json` **outside** that block. If that write fails, for example with `ENOSPC`, the exception escapes without rolling back the app or reporting that installation already committed. The CLI returns failure and the build wrapper propagates it through `subprocess.run(..., check=True)` at `build_macos_app.py:91–93`.

The earlier recovery receipt still says `status="preparing"`; creation of the installed receipt and reporting of running processes have not happened. The actual application has nevertheless changed. This contradicts the delivery contract that build failures leave the installed app intact, and means the failure result and recovery metadata do not describe the installed state.

Reproduction performed with two disposable, genuinely ad-hoc-signed app bundles, a temporary home, and the real `codesign` verification, `ditto` copy, and Darwin atomic exchanges. No app was launched. Only the final receipt write was fault-injected:

```python
def fail_final_record(path, record):
    if path.name == "update.json" and record["status"] == "installed":
        raise OSError(errno.ENOSPC, "injected final receipt failure")
    original_write_record(path, record)
```

Observed and asserted after `install_app()` raised:

```text
install_raised_ENOSPC: true
installed_signature_equals_new_build: true
retained_previous_signature_equals_original: true
recovery_receipt_status: preparing
installed_receipt_exists: false
```

The prior app remains recoverable; this finding does not claim that its bytes are lost. The bug is that the app replacement and its required delivery receipts can commit only partly, while the caller receives an undifferentiated failure and no indication that the new app is already active at the installation path.

**Verification scope**

The four focused reproductions above passed their assertions using `/usr/local/opt/python@3.14/bin/python3.14`. Nine existing tests around catalogue failure, failed/cancelled channel switches, preference saves, cancelled primary/context downloads, raw-before-derived persistence, channel-photo capture, and observation/occurrence transaction rollback also passed: `9 passed, 23 deselected in 4.04s`. These existing tests do not cover the four reproduced boundaries. No production changes or actual app update were made as part of this audit.
