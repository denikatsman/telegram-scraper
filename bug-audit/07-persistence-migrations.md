# Verified persistence and migration findings

Audited the working tree on 2026-09-19 at HEAD `188afe9fcfa3f9b1963edc23f5e46e143abd2632`, including its existing uncommitted changes. All reproductions used disposable local libraries and offline Telegram fixtures. No production code or personal archive was changed.

## 07-01 — [P2] A first capture with a channel photo and zero matching posts leaves a library that cannot reopen

**Locations:** [engine.py:1049](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1049), lines 1049–1057; [engine.py:585](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:585), lines 585–590; [storage.py:170](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:170), lines 170–173.

The engine downloads channel-photo variants before scanning messages. A fresh library has no `messages_all.json`, and `_save()` only creates it when processing a post has set `_dirty`. If the first scan has no matching posts, the photo and its manifest are saved, the run completes, and no message index is written. The next `Store.load()` rejects any contents under `media/` when the index is absent. This rejects state produced by a successful capture, even though there is no lost message index to restore.

**Verified reproduction:** Run `TelegramService.run("range", start="2025-01-01", end="2025-01-01")` against a fresh temporary library with both downloads and context capture enabled. The offline client returned a channel photo containing `PhotoCachedSize("s", 8, 8, b"jpeg")`, one anchor message dated 2025-03-11, and an empty requested date-range page. The actual engine and variant publisher returned:

```json
{"status":"completed","processed":0,"message_index_exists":false,"variant_count":1,"anchor_total":1}
```

A separate empty-channel sync reproduced the same saved state. Both a fresh `Store(root).load()` and a second sync then raised:

```text
Saved media exists but messages_all.json is missing. Restore the message file before syncing.
```

**Impact:** An ordinary empty first date range, or first sync of an empty channel with a photo, blocks subsequent capture and reopening. Stopping after the photo is saved but before the first post is checkpointed reaches the same persistence boundary. The displayed recovery instruction cannot be followed because the index never existed.

**Correction boundary:** Durably establish an empty message index for a confirmed new library before publishing independent channel media, or explicitly recognize this source-only initial state. Preserve the existing protection against genuinely missing message indexes. A regression should complete an empty first range with a photo, reopen the library, and successfully capture a later post.

## 07-02 — [P2] Losing a modern library's source database is accepted as a fresh archive and silently reinitialized

**Locations:** [evidence.py:271](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py:271), lines 271–274; [evidence.py:317](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py:317), lines 317–343; [integrity.py:156](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/integrity.py:156), lines 156–166; [engine.py:1012](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1012), lines 1012–1026.

`EvidenceStore.validate()` treats an absent database without leftover journal files as healthy, without checking whether saved posts already reference source observations. `verify_extended()` accepts that result. On the next capture, `prepare()` accepts the absence and `begin_run()` creates a new database. Neither the existing `source_observation_id`/`source_channel_id` fields nor a backup containing the original database prevents this reset.

**Verified reproduction:** Create a valid source database with one completed run and one message observation. Save a corresponding message-index record containing its source IDs and raw metadata, and create a normal `Store.snapshot()`. Remove only the temporary fixture's `evidence.sqlite3`, representing a missing file or incomplete restore. Then verify and begin a new run:

```json
{"verify_ok":true,"issues":[],"prepare_exists":false,"prepare_ok":true,"recreated_observations":0,"recreated_runs":1}
```

The message index and the ZIP containing the original database were still present throughout the reproduction.

**Impact:** The app reports a successful integrity check after losing recorded TL bytes, earlier observations, occurrence history, and capture receipts. Further capture starts a new observation-ID sequence while surviving records still reference the old one. The code does not cause the initial file loss; the defect is accepting and extending this broken persistence state without identifying the missing source history or requiring recovery.

**Correction boundary:** Distinguish a genuinely legacy archive from a modern archive with missing evidence by consulting persisted source references or a durable format marker. In the latter case, report the missing database and stop automatic initialization. Validate source references against an existing database as well, so a partial restore cannot silently connect an index to unrelated observation IDs.

## 07-03 — [P2] Legacy message archives have no supported path to establish the required channel identity

**Locations:** [storage.py:289](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:289), lines 289–301; [config.py:101](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:101), lines 101–104; [server.py:476](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:476), lines 476–484; [engine.py:1034](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1034).

An archive from the original scripts has messages but no `channel.json`. `bind_channel()` requires a nonempty `legacy_channel` matching the configured channel before creating that identity. Settings have no default or migration that establishes `legacy_channel`, and `Settings.update()` rejects that field. The engine passes an empty value when the field is absent. The legacy engine tests bypass this gap by injecting `legacy_channel` directly in the fixture at [test_engine.py:117](/Users/xxx/GitHub/telegram-scraper/tests/test_engine.py:117).

There is an earlier block when old messages are opened without a settings file: `_update_settings()` treats the presence of records as proof that the current channel setting is already authoritative, even when that setting is empty, so it rejects entering the original channel.

**Verified reproductions:**

1. Open a temporary library containing a valid old-style `messages_all.json`, with no settings or channel identity. Call the normal runtime settings update with valid API details and `channel="@fixture"`. It rejects the update with `This archive belongs to its saved channel. Use Add channel to create a separate archive for another channel.` The existing index is unchanged.
2. Create settings through `Settings.update()` with the same valid channel and credentials, then add a legacy index without `channel.json`. An offline `TelegramService.run("sync")` resolves that channel but fails with `This existing library has no saved channel identity. Reconnect its original configured channel before syncing.` No identity is created.
3. Try `Settings.update({"legacy_channel":"@fixture"})`. It rejects the only missing migration field with `Some settings were not recognized. Reload the page and try again.`

**Impact:** The app can browse the old records but cannot enrich or continue their archive through its normal setup and sync workflows. Add channel creates a separate library; it does not migrate the existing one. Continuing the legacy archive requires undocumented manual metadata edits.

**Correction boundary:** Provide a controlled way to establish the original channel during legacy import and persist its identity. Keep the protection against assigning an existing archive to an unrelated channel; merely removing the guard or automatically trusting every new channel setting would introduce a data-mixing bug.

## 07-04 — [P2] Integrity checks ignore saved attachments once they move into revision history

**Locations:** [storage.py:481](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:481), lines 481–509; [storage.py:577](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:577), lines 577–585. The preserved references are created at [engine.py:548](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:548), lines 548–554, and [engine.py:564](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:564), lines 564–571.

Replacing or removing an attachment retains its file path, size, and checksum in `previous_media` and the saved revision. Verification inspects only each current record's top-level `media_file`; it never traverses either history field. The ZIP checker has the same omission. The cumulative-media comparison only helps if an earlier retained ZIP actually contains that file.

**Verified reproduction:** Using the real ingestion code and the offline `Client`/`Message` fixtures from `tests/test_engine.py`, sync a post with a video, then sync an edit removing the attachment. Disable pre-sync snapshots for these two runs. Confirm the original path is present in both `previous_media` and `revisions`, remove that temporary fixture file, and create the first snapshot. A fresh verifier reports:

```json
{"verify_ok":true,"issues":[],"checked_media":0,"backups":1,"references_in_prior_media":1,"references_in_revisions":1}
```

**Impact:** A known downloaded file can disappear from both the current archive and its backup while the integrity check passes, despite the archive retaining the exact receipt needed to detect the loss. This affects captures with backups disabled and attachments first captured and retired between snapshots, including during one Watch session.

**Correction boundary:** Check primary-media receipts in current records, revisions, and `previous_media`, both locally and within each backup. Deduplicate physical file reads while validating every recorded size/hash. Do not depend exclusively on a preceding ZIP to establish that an older attachment should still exist.

## 07-05 — [P2] A backup's source database can be missing or unusable while the backup is counted as verified

**Locations:** [storage.py:547](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:547), lines 547–566; [storage.py:594](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:594). Current-library source validation at [storage.py:473](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:473) does not validate a ZIP's database.

The ZIP loop reads every member for CRC validation but only decodes the message index and channel identity. It never checks the schema, integrity, observation digests, or source-reference coverage of archived `evidence.sqlite3`. It also allows a source-bearing message index with no database, and the mere presence of a member named `evidence.sqlite3` allows an archive with no message index to pass the required-content check.

**Verified reproduction:** Keep a valid current library with one saved message and its valid source observation. Separately verify three well-formed ZIP fixtures with valid CRCs:

| Backup contents | Result |
| --- | --- |
| Modern index with source IDs, channel identity, and `evidence.sqlite3` containing `b"truncated SQLite state"` | `ok=True`, `issues=[]`, `checked_archives=1` |
| Same modern index and channel identity, with the database omitted | `ok=True`, `issues=[]`, `checked_archives=1` |
| Channel identity and the invalid database, with no message index | `ok=True`, `issues=[]`, `checked_archives=1` |

Each ZIP also independently passed `ZipFile.testzip()`. These fixtures exercise invalid payloads inside structurally intact archives; they do not claim that random damage to compressed ZIP bytes evades CRC checks.

**Impact:** “Archive check passed” does not establish that a backup can restore its saved source observations. A complete ZIP container can hold incomplete or unusable source state and still be presented as verified. This is separate from 07-02: here the live database is healthy and the defective copy is inside the recovery archive.

**Correction boundary:** Validate an archived source database using an isolated temporary copy, including the supported schema and evidence checksums. Require it when that archive's message records reference source observations. Accept a source-only backup only after validating its database. Continue accepting genuine legacy backups without modern source references.

## Validation

The five findings above were reproduced independently of the existing tests. The focused existing checks remained green:

```text
PYTHONDONTWRITEBYTECODE=1 /usr/local/opt/python@3.14/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_storage.py tests/test_evidence.py tests/test_naming.py
60 passed, 30 subtests passed

PYTHONDONTWRITEBYTECODE=1 /usr/local/opt/python@3.14/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_integrity.py tests/test_enrichment.py::test_channel_photo_sizes_are_saved_with_channel_scope_and_no_message_id tests/test_engine.py::test_evidence_only_crash_data_is_snapshotted_and_checkpoint_is_not_resume_claim
17 passed
```

All Telegram responses were offline fixtures; no account connection, live scrape, desktop interaction, app rebuild, or installation was performed.
