# Telegram Scraper: confirmed-bug repair plan

Repair recovery protection first, then address connection ownership and Watch ordering, followed by channel identity, media reuse, and browser state. Finish by correcting installation reporting and validating the complete installed application. This order makes later changes easier to recover and prevents related fixes from introducing competing rules for ownership, identity, or freshness.

**Status: all 26 repairs implemented, automated checks passed, version 1.3.4 installed and exact-build verified. Live Telegram and interactive desktop checks remain outstanding.** Saved and amended on **2026-09-25** after checking the external review against the current source. This document supersedes the chat-only plan of 2026-09-19. Implementation progress and current evidence are recorded in the tracker below.

## Verified starting point and boundaries

The intended synthesis completed successfully in task **“Launch eight project audits”**, synthesis turn `01a0b881-f47c-7f43-b28e-1702cd3193ea`: its recorded status is `completed`, its error is null, and its final outcome reports the completed synthesis. [FINAL.md](/Users/xxx/GitHub/telegram-scraper/bug-audit/FINAL.md) matches the SHA-256 recorded in [synthesis-validation.json](/Users/xxx/GitHub/telegram-scraper/bug-audit/evidence/synthesis-validation.json):

`9efd29a1a7eba06c433bbfa9d275e859c0555c2b8c7dac6440ade2cd2eeb8257`

The repair scope is **26 confirmed bugs: one P1 and 25 P2**. All 27 original findings were accounted for by the synthesis; one was merged, and none remain unverified. Verification limitations in the report are not additional repair requirements.

The intended project is `/Users/xxx/GitHub/telegram-scraper`, at commit `188afe9fcfa3f9b1963edc23f5e46e143abd2632` plus the nine modified tracked files and four untracked project files captured at audit launch. During the original planning pass, all 49 baseline files matched their recorded contents and modes, the Git index matched, and no additional source files were found outside `bug-audit/`. On 2026-09-25, the 49 file contents/modes and FINAL.md hash were checked again and still matched; the completed synthesis outcome was also rechecked in the same task.

Implement against that complete working state. Preserve the original reports, synthesis, evidence, saved edits, archive data, and previous application bundles. If relevant code changes before implementation, revise the affected step explicitly after checking the discrepancy.

## Review amendments and execution rules

The external review's two technical concerns are accurate and useful **code-based risks in the proposed implementation**, not newly reproduced product defects:

- **Step 7:** [The history request at engine.py:812](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:812) yields while [Watch callbacks](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:896) can receive updates. A delayed response can therefore receive a later local timestamp while carrying older values. The amended rule uses request-start boundaries, scoped protocol ordering where meaningful, and explicit handling of ambiguous counter snapshots.
- **Step 8:** [The current engine creates a run before resolving its peer](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1020), while [runtime request scoping checks archive revisions](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:149). Resolution/deduplication now belongs to an exclusive preflight before capture-run creation. A duplicate discovered by Sync offers a separate Open existing archive action; it cannot silently change the selected archive.
- **Steps 5–6:** Add a combined login-timeout, cleanup, and shutdown regression.
- **Step 15:** Preserve older successful receipt formats. [An unchanged installation currently creates no update attempt](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:130), so an update receipt cannot be universally required.
- **Execution:** Save this plan before implementation and track each confirmed ID and its verification result in the table below.

This review used targeted source inspection and existing synthesis evidence. No new failure reproduction, fixes, application build, installation, or user-data operation was performed.

The numbered order is the default review sequence; dependencies determine what must precede what. Steps 1, 4, 5, 12, and isolated installer work in step 15 can proceed without waiting for channel-identity or media work. During a separately authorized implementation task, continue through independent steps without routine per-step approval. Preserve explicit user choices for legacy identity, ambiguous recovery, and real-account/desktop checks. This plan amendment does not launch implementation.

## Ordered implementation steps

### 1. Make backup enumeration and corruption failures explicit — TS-001, TS-007

**Result:** An unreadable source subtree prevents snapshot publication. A corrupt compressed member produces a named verification issue while other backups are still checked.

**Components:** `Store.snapshot()` and `Store.verify()` in [storage.py](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py).

**Correction:**

- Give both snapshot directory traversals an error callback that raises the enumeration failure. Publish only after both complete inventories and the existing consistency checks succeed.
- Add `zlib.error` to the per-backup corruption boundary, preserving cancellation as a separate outcome.
- On snapshot failure, remove only the temporary output owned by that attempt. Retain every source file and existing backup.

**Dependencies:** None. This comes first because TS-001 is the P1 recovery defect.

**Verification:** Inject enumeration failure in each traversal separately; also exercise a genuinely unreadable synthetic directory where permissions permit. Assert failure, no new completed ZIP, and unchanged source hashes. Place the reported invalid-DEFLATE fixture before a good ZIP and assert a named issue plus successful examination of the good ZIP. Retain cancellation, symlink rejection, and exclusive-publication checks.

**Complete when:** Neither traversal can silently omit a subtree, and decompressor corruption cannot abort the remaining independent backup checks.

### 2. Establish the empty index before publishing initial channel media — TS-002

**Result:** First capture of an empty channel, an empty date range, or a cancelled channel-photo download leaves an archive that can reopen and retry.

**Components:** `TelegramService.run()`, its save boundary, and `Store` initialization.

**Correction:** After validating the existing layout and source state, durably save an empty index for a genuinely new library before channel-photo or other independent media publication. Do this explicitly; the existing message-dirty flag cannot represent initialization.

Preserve the missing-index guard for established archives. Do not automatically turn a pre-existing folder with unexplained media into an empty archive.

**Dependencies:** Step 1. Integrate its source-state check with step 3. Once step 8 is integrated, this initialization runs only after successful capture preflight; a duplicate-channel response must not create an index or media in the unresolved archive.

**Preservation and recovery:** Existing affected folders must remain intact. Document that a missing index requires either restoration or a separately approved recovery based on evidence that the archive never contained derived posts. An ambiguous historical folder must not be reset as part of this fix.

**Verification:** Convert the synthesis’s successful-empty-sync and interrupted-photo scenarios into regressions that require an on-disk empty index, successful fresh `Store.load()`, and successful retry. Add an empty-range case and an index-write failure before photo publication. Confirm that deleting the index from an established archive still blocks capture.

**Complete when:** Every supported initial outcome is reopenable, without weakening established-archive loss detection.

### 3. Verify the complete recovery state, including historical attachments — TS-003, TS-004, TS-005

**Result:** Verification detects missing source history, broken source references, missing historical attachments, and invalid source databases inside backups.

**Components:** `Store.verify()`, [EvidenceStore validation](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py), and [integrity.py](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/integrity.py).

**Correction:**

- Introduce a shared traversal of current records and retained revisions. Use it to collect source references and primary-media receipts, including `previous_media`.
- Treat persisted source-observation references as evidence that the database is required. Check this before capture can initialize a database, during runtime recovery/selection, and during verification.
- Validate referenced observations against the message ID, channel ID, observation kind, and saved raw payload where available. A numerically matching row ID alone is insufficient.
- Check every historical media receipt while deduplicating file reads. Conflicting receipts must still be assessed separately.
- For each ZIP, stream its database into a fixed path inside an isolated temporary directory. Apply the existing supported-schema, SQLite integrity, payload-checksum, and new source-reference checks to that copy.
- Preserve genuine legacy archives without source references and valid source-only backups. A database requiring journal recovery must be reported as requiring recovery, rather than silently repaired during verification.

**Dependencies:** Step 1; coordinate initialization with step 2.

**Preservation:** Verification remains read-only for the archive being reviewed. Do not recreate missing modern databases, renumber observations, rewrite old indexes, or overwrite recovery material. Do not extract arbitrary ZIP paths. Temporary database copies must be cleaned up on success, error, and cancellation.

**Verification:** Cover modern database removal; a mismatched replacement database; valid legacy data; source references appearing only in revisions; removed/replaced attachments lost before the first backup; repeated references to one file; and unsafe historical paths. Reuse all three TS-005 layouts, now requiring named failures. Include a valid source-only ZIP and healthy legacy ZIP as controls.

**Complete when:** The local archive and each backup are checked against their own required source and attachment evidence, with failures named and unrelated backups still examined.

### 4. Reconcile catalogue publication before rolling back a new folder — TS-011

**Result:** A directory-flush failure cannot leave a saved channel entry pointing to a folder the application deleted.

**Components:** `Channels._save()` and `Channels.add()` in [channels.py](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py).

**Correction:** On save failure, reread and validate the catalogue to determine whether replacement occurred. If the proposed catalogue is visible, reconcile the in-memory values and digest, retain the new folder, and report that durability confirmation failed. Remove the newly allocated empty folder only when the old catalogue is positively established and does not reference it. If publication cannot be determined, retain the folder and report the uncertainty.

This can remain local to catalogue handling; it does not require redesigning every atomic JSON writer.

**Dependencies:** None; complete before step 8 uses additional catalogue updates.

**Verification:** Inject failures before replacement, immediately after replacement, and during reconciliation reads. Check disk contents, memory/digest consistency, retained folders, restart, re-add, and selection. Continue rejecting missing or symlinked pre-existing archive folders.

**Complete when:** Failure handling leaves a coherent known state or an explicitly reported uncertain state, never a deliberately created dangling catalogue entry.

### 5. Make client cleanup and credential replacement converge — TS-015, TS-012

**Result:** Timed-out login attempts release their partial transports before retry. A successful credentials save cannot retain a client constructed with different credentials.

**Components:** `TelegramService._connected_client()` and `close()`, `Runtime._update_settings()`, authentication handling, and [Settings](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py).

**Correction:**

- Wrap connection establishment in failure/cancellation cleanup that drains disconnect before the client can be reused or replaced. Preserve the original cancellation or connection error.
- Track the credentials used to construct a client separately from mutable service settings.
- Separate settings validation from publication. For an API-identity change, first mark the connection unavailable and dispose of the old client; publish the new settings only after cleanup succeeds.
- If cleanup fails, retain ownership of the client in an unavailable state and make retry attempt cleanup again. Do not advertise authorization or permit jobs through it.
- If settings publication fails after cleanup, retain the old persisted settings and remain disconnected. A later connection may construct a fresh client from those settings.
- Preference-only saves must continue without disconnecting a healthy client.

**Dependencies:** None. This establishes the cleanup contract used by shutdown and identity resolution.

**Verification:** Use the installed Telethon implementation with a loopback handshake fixture. Repeat shortened initial-handshake timeouts and assert no accumulating transports/tasks. Include the combined cleanup/shutdown scenario in step 6. Cancel cleanup repeatedly and assert authentication remains unavailable until cleanup finishes. Inject the reported disconnect/session-close error during a credentials change, then retry; verify saved credentials, actual client identity, and connection status agree. Preserve expired-session and two-factor login behavior.

**Complete when:** Retry cannot orphan a prior connection or report success with mismatched client credentials.

### 6. Close mutation admission before shutdown begins draining — TS-014

**Result:** Requests already accepted by HTTP cannot start new work after shutdown has passed its drain boundary.

**Components:** `Runtime._close()`, job/auth/settings/channel admission, and HTTP error handling.

**Correction:** Set a monotonic closing state before the first shutdown await. Check it at every mutating entry point, including the new identity actions in step 8. Keep Stop available and make repeated close calls converge on the same shutdown operation. Drain all admitted jobs, authentication, channel work, capture preflight, and owned cleanup before stopping the executor or event loop. A preflight must recheck the closing state immediately before handing off to a capture task; no await or unreserved admission gap may separate that check from the handoff.

**Dependencies:** Step 5.

**Verification:** Hold an authenticated POST body until shutdown is awaiting disconnect, then release it. Require an explicit closing rejection, no new task or evidence run, and no successful job acknowledgement. Repeat for authentication and channel/settings mutations. Confirm that an already-running job finishes its cancellation and durable receipt before teardown. For the combined TS-015/TS-014 regression, stall an initial login handshake after transport creation, trigger the runtime timeout, hold its disconnect cleanup at a barrier, then start shutdown. Assert retries/new jobs are rejected, the event loop and library ownership remain available until cleanup drains, and releasing the barrier leaves no transport, cleanup task, job, or unfinished capture run. Repeat with an additional cancellation during cleanup. Apply the same shutdown barrier to step 8's preflight; it must not create a capture run after closing begins.

**Complete when:** No mutation can cross the closing boundary and all previously admitted work reaches its normal cleanup boundary.

### 7. Give Watch explicit freshness, wakeup, failure, and waiter ownership — TS-009, TS-024, TS-010, TS-016

**Result:** Watch does not make an observation current merely because its history response or disk write completed later. It promptly processes queued work, reports real persistence failures, and retains a bounded number of disconnection waiters.

**Components:** `_watch()`, `_history_pages()`, `_process_inner()`, `_wait_for_update()`, the retry wrapper, run finalization, and collector cancellation.

**Correction — observation ordering:**

- Assign a monotonic per-run ordinal at entry to each Watch callback, before any raw-update/message persistence await. Capture the original update's applicable sequence metadata with its stream scope. Register inflight receive markers immediately; persistence completion must not determine their order.
- Assign a request-start ordinal immediately before each app-issued history request attempt, including retries. Attach the successful attempt's start ordinal to its returned messages; keep response-received time separately for provenance. Do not assign their effective ordering ordinal when the response arrives or when individual messages are processed.
- Distinguish local arbitration from proof of Telegram freshness. For comparable content versions, retain the later edit. Use sequence values only when they describe comparable events in the same account/channel stream and govern the change being ordered. Never compare unrelated PTS scopes or interpret a page's `ChannelMessages.pts` as a per-message version for views/reactions. Equal or absent edit dates and sequence fields need the fallback below.
- For an equal-version history/Watch conflict without stronger applicable ordering evidence, prefer a live observation whose receive ordinal is later than that history request's start. A history request started after the live observation can supersede it. Thus a delayed older history response cannot displace a live update received while it was outstanding, and an older queued live observation cannot displace a later-started history read solely because the queue drains afterward.
- Apply the rule both to queue coalescing and to current-record replacement. Wait for relevant inflight raw receipts before promoting a conflicting candidate; source-write failure remains fatal. Only successfully preserved observations may become derived current state.
- If conflicting equal-version counter snapshots lack authoritative ordering, retain all raw candidates and coalesce a targeted reread for that message after the conflict. Its own request-start boundary must respect newer live updates. Limit this to one reconciliation request per conflict generation; do not create an unbounded retry loop. If the reread also overlaps or cannot settle the conflict, retain the protected live candidate, record the limitation, and wait for later ordinary observations. Do not synthesize raw data by taking maximum counters or claim an atomic Telegram snapshot.
- Record a superseded-processing outcome without promoting an older payload into current text, counters, media state, or misleading new revisions. Store acquisition time as provenance, not as an authoritative server version.

**Correction — the other three Watch boundaries:**

- Clear notifications before inspecting/draining the queue, or recheck queue/overflow after the awaited checkpoint. Never wait while work is already queued.
- Pass the engine's failure-aware cancellation check into the collector. Reassess `_watch_failure` after inflight callbacks drain and before finalizing a cancellation.
- Own one Telethon disconnection waiter for each Watch run, reuse it across wakeups, and release the returned wrapper on exit without cancelling the underlying connection future.

**Dependencies:** Steps 3, 5, and 6.

**Preserve:** Raw-first persistence, every observation/occurrence, the bounded queue, overflow recovery, periodic catch-up, Stop responsiveness, revision history, and completion of disk workers before lock release. This ordering policy does not claim that wall-clock receipt time or Telegram counters provide a total order of all server states.

**Verification:** Use deterministic barriers and actual production acquisition paths:

1. An older queued live observation arrives before a later-started history request returns newer text or views; history remains current after queue drain.
2. A history request starts with a captured response containing 1 view, its delivery is held, and a live observation with 9 views arrives before the response is released; the delayed response must not roll back the live value.
3. Reverse response delivery/processing order while keeping the same request-start/live-receipt relationship; the arbitration result must be the same.
4. Repeat equal-edit-date counter cases, genuine newer edits, missing/equal/incomparable sequence values, comparable live-update sequence values, delayed raw writes, request retry with a new start boundary, and overflow rescans. Assert preservation of both raw observations and correct source references.
5. Verify targeted reconciliation coalesces, respects a new live update, and stops retrying an unresolved conflict instead of looping or manufacturing merged raw data.

For the other defects, enqueue during checkpoint and require processing without another event or timer. Fail a raw append during context work and require an error receipt naming the cause; ordinary Stop must remain cancellation. Run 100 wake cycles and Watch stop/restart with bounded callbacks and an uncancelled underlying disconnection future.

**Complete when:** Both overlapping arrival orders and all four original Watch failure paths pass their regressions, with raw evidence retained, ambiguous ordering reported honestly, and terminal outcomes accurate.

### 8. Separate immutable peer identity from repairable channel locators — TS-008, TS-018, TS-022

**Result:** Legacy archives can deliberately establish their original channel, renamed channels can continue using the same archive, and alternate locators cannot begin duplicate capture once their peer identity is known. Sync discovering a duplicate leaves the selected archive unchanged.

**Components:** Channel resolution/binding, `Store.channel_info()/bind_channel()`, `Channels`, runtime capture admission and channel/settings actions, the CLI caller of `start_job()`, and the Settings/Add channel UI.

**Correction — identity and setup:**

- Represent identity as peer kind plus numeric ID. Extend `channel.json` additively with peer kind while retaining existing fields and IDs.
- Infer missing peer kind from trustworthy saved source objects where available. Do not equate a basic group and channel merely because their positive IDs match. Ambiguous old metadata requires explicit confirmation.
- Resolve bound archives through their saved typed identity and the authorized account's cache/dialogs when a locator fails. Permit a replacement locator only after it resolves to that same identity. Preserve the existing no-join behavior.
- Add scoped resolve/confirm actions for legacy binding. Resolution returns the peer's title, kind, ID, matching archives, and an opaque confirmation token tied to the active archive and relevant settings/index/catalogue state. Binding requires explicit confirmation that this is the original channel.
- Write the identity before its repaired locator. If locator publication fails, retain the binding and allow a retry to finish the same operation; never silently rebind.

**Correction — exact capture preflight boundary:**

1. For Sync, Date range, and Watch, `Runtime.start_job()` must reserve an exclusive, tracked preflight operation before its first network await. It keeps the existing project/archive ownership and blocks competing jobs, authentication, settings, and channel mutations. Verification jobs remain offline and do not need Telegram identity preflight.
2. Capture the expected active archive, context revision, credentials/settings identity, and catalogue state. Resolve the authorized peer and compare its typed identity with saved archive bindings. Resolution must be independent of `_run_id`: it cannot append into a previous run. Buffer any resolution-source objects for recording only if a new capture is subsequently admitted.
3. Complete duplicate detection **before** `EvidenceStore.begin_run()`, channel binding, empty-index initialization, snapshots, or capture-media writes. Account connection/cache activity may be necessary for resolution; preflight must not create or modify the candidate archive's capture data.
4. Recheck the closing/cancellation state and captured context before committing admission. On success, pass the resolved target and validated context into the capture task without an unreserved gap or another independent channel resolution. Only then begin the evidence run and perform archive mutations. Engine capture entry points must not retain the old resolve-after-`begin_run()` path or bypass validated preflight.
5. If one *other* archive already owns that identity, release preflight without starting a capture. Return an explicit result such as `capture_started: false` with `existing_archive_id`; leave the current archive, job result, index, folders, catalogue selection, and context revision unchanged. Multiple matches return the matching identifiers and require a choice; do not pick or merge one automatically.
6. The browser consumes that result and offers **Open existing archive**. That button issues a separate explicit channel-selection request using the existing selection guards and a still-current context. It does not automatically retry Sync. The CLI must also consume the no-capture result, identify the existing archive, and exit without claiming sync success or waiting on an unrelated earlier job.
7. Stop, request timeout, and shutdown cancel and drain the tracked preflight, including partial connection cleanup from step 5. Release its reservation only after cleanup; no late completion may start a capture run.

**Add channel behavior:** When connected, resolve before committing a new archive; one existing identity match still opens that archive as part of the explicit Add action. Keep its exclusive reservation through resolution and selection, using an internal selection helper rather than exposing a gap between them. Multiple pre-existing matches require a separate choice without merging/deleting archives. Preserve offline setup as unresolved configuration; its eventual capture uses the preflight and separate Open action above. Do not discard an unresolved folder automatically.

**Dependencies:** Steps 3–6, particularly catalogue reconciliation, client cleanup, and closing admission. Integrate step 2's initialization after the successful preflight boundary.

**Preservation:** Legacy message files remain byte-for-byte unchanged during binding. Keep unknown metadata, session files, archive keys, attachments, and all pre-existing duplicate archives. Continue rejecting a genuinely different peer for an established archive.

**Verification:** Cover legacy records with empty settings, already-configured legacy records, and existing manually supplied legacy metadata. Test renamed username and revoked-locator fallback, wrong-peer rejection, basic-group/channel ID collisions, username versus private-message-link deduplication, offline deferred resolution, multiple existing matches, stale confirmation tokens, and failure between identity and locator publication.

For Sync discovering a duplicate, assert zero new run/observation rows, no new index/snapshot/media/binding, unchanged selected archive/context revision, and no capture task. Click the separate Open action and assert only that request changes selection; a further Sync is explicit. Retain Add channel's existing-archive selection behavior as a separate control. Barrier-test concurrent settings/selection, timeout, Stop, and shutdown during preflight and immediately before handoff; require rejection/drain without a late run. Check CLI handling of the duplicate result.

**Complete when:** Supported setup can establish or repair identity safely, duplicate capture is stopped before any run is created, and archive selection changes only through the appropriate explicit action.

### 9. Request full metadata for the actual peer type — TS-019

**Result:** Basic groups capture their supported full metadata and photo path without the channel-cast warning.

**Components:** `TelegramService._channel_context()`.

**Correction:** Dispatch `messages.GetFullChatRequest` for basic groups and `channels.GetFullChannelRequest` for channels/supergroups. Feed both responses through the existing raw, related-entity, and photo capture paths.

**Dependencies:** Use the peer-kind handling established in step 8.

**Verification:** Exercise real Telethon request resolution with offline `Chat` and `Channel` fixtures. Assert the correct request, retained full metadata, photo capture, and absence of the incorrect cast warning. Preserve genuine access-failure warnings.

**Complete when:** Both accepted peer families use the appropriate metadata request without changing source-capture guarantees.

### 10. Make primary-media publication recoverable before the index checkpoint — TS-021

**Result:** A retry recognizes a completed attachment after process termination and avoids retaining another identical file.

**Components:** Primary download/publication in `engine.py`, narrowly scoped evidence lookup methods, and integrity verification.

**Correction:**

- Use the existing immutable evidence store for small, versioned `primary_media_intent` and `primary_media_file` receipts; retain its existing SQLite schema.
- An intent identifies the exact media object/size representation, destination, verified byte count, checksum, peer, and message. Commit it after verifying the temporary download and **before** linking the final file.
- After exclusive publication and directory flush, record completion before relying on the JSON checkpoint.
- Before downloading on retry, look up matching intents/receipts and verify the existing file’s safe path, size, and checksum. A valid published file can complete an interrupted intent and restore the index receipt without another transfer.
- If an old pre-fix filename has no trustworthy identity receipt, compare its bytes with the newly verified download before allocating a suffix. Reuse only exact matches.
- Preserve every differing or damaged file. An intent whose destination is absent is incomplete acquisition, not proof that a saved file was lost.

**Dependencies:** Steps 2, 3, and 8.

**Preservation and compatibility:** Keep existing filenames and legacy receipts readable. Do not sweep orphan files, overwrite conflicting paths, or require a bulk migration. Extend verification to understand completed primary receipts and distinguish incomplete intents.

**Verification:** Terminate subprocesses immediately before publication, immediately after the real link, after completion receipt, and before JSON save. Repeated post-link exits followed by retry must leave one correct complete file and a valid index receipt. Test receipt-write failure, changed bytes, same-size corruption, symlinks, and genuinely conflicting filenames.

**Complete when:** Replaying a completed acquisition reuses verified bytes; failed or conflicting acquisitions retain their evidence and recover safely.

### 11. Reuse matching primary media during detailed capture — TS-020

**Result:** A preview document or selected photo representation is transferred and stored once, while distinct variants are still captured.

**Components:** Primary media selection, `ContextCollector._inventory()/capture_message()` in [enrichment.py](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py), and receipt verification.

**Correction:** Share an exact media descriptor between the primary downloader and collector: object kind/ID, selected size constructor/type, and byte representation. Match Telethon’s actual document-before-photo and photo-size selection. Pass the verified primary receipt to the collector.

For an exact match, retain the existing `primary_archive` distinction with a verifiable receipt/reference and a reused count; do not allocate a second variant file. Route its integrity check through primary-media validation. Continue acquiring other sizes, thumbnails, alternate documents, and covers.

Do not equate stripped-photo source bytes with Telethon’s expanded JPEG merely because they originate from the same size object.

**Dependencies:** Step 10.

**Verification:** Count actual transfer locations and saved files for direct documents, nested preview documents, previews containing both document and photo, progressive photos, video sizes, and embedded stripped sizes. Require one acquisition for an identical representation and continued acquisition of distinct variants. Preserve disabled-download and damaged-cache behavior.

**Complete when:** Matching primary/variant representations share verified storage without suppressing distinct source material.

### 12. Correct download URLs and post-header stream failures — TS-017, TS-006

**Result:** Download file works for main and child archives. A failed stream cannot append a second HTTP response into the advertised body.

**Components:** `renderPostDetail()/archiveURL()` in [app.js](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js), plus `Handler.send_file()`, `do_GET()`, and response commitment.

**Correction:** Build the download query using `URL.searchParams`. Track response commitment per request; after headers begin, terminate a failed or prematurely exhausted stream and close the connection. Send structured errors only before commitment. Apply the boundary to media, posts export, source export, and static-file streaming without changing authorization or library scoping.

**Dependencies:** None; these two small corrections can be reviewed independently.

**Verification:** Drive actual rendered links through a temporary loopback server for main and child libraries and compare returned bytes. Inject the reported second-read EIO with only 100 advertised bytes remaining, plus range-response and premature-EOF cases. Require an incomplete/failed transfer with no embedded HTTP error response. Preserve HEAD, valid ranges, missing-file responses, and previews.

**Complete when:** Both URL construction and transfer failure handling pass their separate regressions.

### 13. Submit only settings fields the user changed — TS-023

**Result:** Saving an older form cannot overwrite another tab’s unrelated preference changes.

**Components:** `fillSettings()` and `saveSettings()`.

**Correction:** Capture normalized editable values when the form opens or is successfully refreshed. Submit only fields changed from that snapshot. Include the API hash only when a replacement was entered; retain existing blank-hash semantics. Rebase the snapshot after a successful save, not on background polling while the user edits.

Use the existing partial-update server behavior. Deliberate edits to the same field retain the current last-successful-save behavior; this fix does not introduce a general conflict-resolution interface.

**Dependencies:** Step 5’s credential transition and step 8’s channel controls.

**Verification:** Open two forms, save one preference in A and a different preference in B, then confirm both survive. Cover reverted edits, no-op save, blank hash, changed credentials, validation failure, and archive switching.

**Complete when:** Untouched stale fields are absent from the request and cannot revert newer settings.

### 14. Refresh rows using an acknowledged library revision — TS-025, TS-026

**Result:** Real post/media changes invalidate the list, and unsuccessful refreshes remain pending.

**Components:** `Store` mutation points, runtime state/posts responses, `loadState()`, and `loadPosts()`.

**Correction:**

- Add an opaque library revision comprising a store-instance epoch and mutation counter. Advance it for app-owned visible record changes, including media completion and loaded record replacement. Do not persist it into archived message data.
- Return it as `library.revision` from state and `library_revision` with the matching posts response.
- Replace the count/date/job-counter signature with the library revision and active-library context.
- Acknowledge a revision only after its posts response is accepted by the current request generation and rendered. Failed, aborted, superseded, or wrong-library responses cannot acknowledge it.
- Preserve the prior rows after transient failure and retry through normal polling; retain pagination, filters, focus, and existing channel guards.

**Dependencies:** Step 7 and the media mutations in steps 10–11.

**Verification:** Repeat two completed edit jobs with identical counters; complete media while Watch remains running; fail one posts refresh followed by healthy state polls; and race older responses against filter/channel changes. Assert eventual correct rows, retries after failure, no acknowledgement by stale responses, and no repeated fetch once the current revision renders successfully.

**Complete when:** Both missed invalidation and false acknowledgement are independently corrected.

### 15. Report committed installations with incomplete receipts accurately — TS-013

**Result:** A metadata failure after the app swap clearly reports the actual installed state and supports a safe receipt-repair retry, without invalidating older successful installations.

**Components:** [install-app.py](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py), its command-line outcome, `check_install()`, and [build_macos_app.py](/Users/xxx/GitHub/telegram-scraper/tools/build_macos_app.py).

**Correction:**

- Persist candidate/previous signatures and relevant paths in the preparation record before swapping. Add an attempt identifier to new records so installed and update receipts can be correlated explicitly.
- Retain rollback for candidate publication or verification failure.
- Once installation and previous-bundle retention succeed, classify receipt failure as `installed-metadata-incomplete`, with exit code `2` meaning attention required. Report the installed path, retained previous bundle, failed receipt locations, and restart status where available.
- Make the build wrapper distinguish that outcome from failure before installation.
- On retry with the same verified build, reconcile the preparation record against actual bundle signatures and finish missing receipts without another unnecessary swap or losing previous-version provenance.
- Extend the read-only installation check to detect incomplete required receipts. If reconciliation cannot establish the state, preserve all bundles and report the specific uncertainty.

**Receipt compatibility and applicability:**

- Accept existing successful `installed`, `unchanged`, and supported `linked-external` receipts using their existing bundle identifier, destination, signature, and relevant fields. Missing newly introduced attempt IDs, previous-signature fields, or format metadata is not by itself corruption or installation failure.
- Require an update receipt only when an actual relevant installation attempt exists, established by an explicit receipt reference or a matching preparation record. An unchanged check or supported external link with no update attempt does not need an `Updates/.../update.json`.
- Correlate attempts to this bundle/build; unrelated or superseded historical failures must not invalidate a healthy current installation. A known post-swap incomplete attempt must not be relabelled as a legacy success merely because its installed receipt is absent.
- Preserve older records and unknown fields during reconciliation. Read-only `--check` performs no receipt migration or repair; repair occurs only through the explicit installer retry. Bundle equality and receipt completeness remain distinguishable results.

**Dependencies:** Required before final real installation; otherwise independent.

**Verification:** Use disposable application bundles and an isolated `MY_UTILITIES_HOME`. Inject ENOSPC at each final receipt boundary. Verify accurate output/exit code, new installed signature, retained old signature, and successful receipt repair on retry. Preserve rollback for pre-commit failures and rejection of bundle-ID/signing-team mismatches.

Add controls for an older successful installed receipt without the new optional fields, a genuine unchanged installation without an update attempt, and a supported external-link receipt. They must remain valid without a fabricated update receipt. Also test a known incomplete attempt, an unrelated/superseded failed attempt, and read-only checking that leaves all receipts unchanged.

**Complete when:** Neither caller nor receipts falsely imply an unchanged installation after a committed swap, retry restores coherent metadata, and older valid receipt states remain supported.

## Confirmed-bug coverage and implementation tracker

All 26 corrections and their focused regressions are implemented. The combined checks, final integrated suite and exact installed-build verification pass. The results below describe automated evidence, not live Telegram or desktop acceptance.

Verification commands (run from the repository root):

- **R1:** `/usr/local/opt/python@3.14/bin/python3.14 -B -m pytest -q tests/test_repairs_*.py` — **94 passed** in 39.77 s; [combined log](implementation/combined-python.log). This includes every planned combined sequence and the real Telethon loopback shutdown barrier. An earlier run had 92 passes and one newly added fixture missing its required `mode`; that fixture was corrected and the complete group rerun. The original failure log is retained.
- **R2:** `node --test tests/app.test.cjs` — **6 passed**, no skipped/cancelled tests; [JavaScript log](implementation/javascript.log). Tests execute the production JavaScript in a controlled DOM and response harness.
- **R3:** `/usr/local/opt/python@3.14/bin/python3.14 -B -m pytest -q` — **336 passed, 30 subtests passed**, no skips, in 71.39 s; [integrated log](implementation/integrated-python.log).

Each row identifies the relevant R1 test module or R2 behavior. Existing neighboring Python tests are additionally covered by R3. All cancellation, corruption, crash and destructive probes use temporary synthetic libraries; installer tests use signed disposable bundles and isolated homes.

| Confirmed ID | Step | Implementation status | New verification result / remaining limitations |
|---|---:|---|---|
| TS-001 | 1 | Verified (automated) | R1 `test_repairs_storage.py`: both enumeration failures, real unreadable subtree, no published partial ZIP; source bytes preserved. |
| TS-002 | 2 | Verified (automated) | R1 `test_repairs_initialization.py`: empty sync/range, cancelled photo, reopen/retry/snapshot/verify, index-write failure. Existing damaged archives require deliberate recovery. |
| TS-003 | 3 | Verified (automated) | R1 `test_repairs_storage.py`: missing source DB/current or revision-only references, wrong replacement row/message/channel/kind/raw; recreation blocked. Lost historical bytes are not recreated. |
| TS-004 | 3 | Verified (automated) | R1 `test_repairs_storage.py` and `test_repairs_media.py`: historical/removed attachments, conflicting receipts, crash retry then historical and backup verification. |
| TS-005 | 3 | Verified (automated) | R1 `test_repairs_storage.py`: all three invalid/missing/source-only ZIP database layouts, valid legacy/source-only controls; temporary fixed-path copies only. |
| TS-006 | 12 | Verified (automated) | R1 `test_repairs_http.py`: EIO/early EOF for media, ranges, both exports and static files; incomplete body with no second HTTP response. |
| TS-007 | 1 | Verified (automated) | R1 `test_repairs_storage.py`: invalid DEFLATE is named and later good backup is checked. |
| TS-008 | 8 | Verified (automated) | R1 `test_repairs_identity.py` and `test_repairs_combined.py`: empty/configured/manual legacy binding, stale token, preserved index/unknown fields, checked source inference. Interactive/live confirmation remains outstanding. |
| TS-009 | 7 | Verified (automated) | R1 `test_repairs_watch.py`: actual request-start/receipt barriers in both orders, retries, scoped sequence, delayed raw writes, bounded overlapping reread. Live Watch remains outstanding; ambiguous counter ordering is recorded. |
| TS-010 | 7 | Verified (automated) | R1 `test_repairs_watch.py`: raw failure during context yields error; ordinary Stop stays cancelled. Live Watch remains outstanding. |
| TS-011 | 4 | Verified (automated) | R1 `test_repairs_catalogue.py`: before/after/unknown publication, restart/re-add/selection; folder retained whenever publication is possible. |
| TS-012 | 5 | Verified (automated) | R1 `test_repairs_lifecycle.py`: disconnect failure/retry, settings publication failure, actual client identity and preference-only save. Real account sign-in remains outstanding. |
| TS-013 | 15 | Verified (automated) | R1 `test_repairs_installer.py`: both final receipt ENOSPC boundaries, exit 2, read-only check, repair without swap, legacy receipts, superseded/unrelated attempts, rollback and ID/team rejection. Real installation and exact-build/receipt check also passed (D1 below). |
| TS-014 | 6 | Verified (automated) | R1 `test_repairs_lifecycle.py` and `test_repairs_identity.py`: held HTTP mutations, real handshake timeout/held cleanup/shutdown, additional cancellation, no late preflight handoff. |
| TS-015 | 5 | Verified (automated) | R1 `test_repairs_lifecycle.py`: three real Telethon loopback handshake timeouts, repeated cancellation, combined held cleanup/shutdown; no retained transport or orphan task. Real Telegram remains outstanding. |
| TS-016 | 7 | Verified (automated) | R1 `test_repairs_watch.py`: 100 wake cycles across two runs, bounded public wrappers/callbacks, underlying disconnect future remains uncancelled. Live Watch remains outstanding. |
| TS-017 | 12 | Verified (automated) | R1 `test_repairs_http.py` and R2: actual rendered main/child download links fetched over loopback, correct bytes and HEAD. Native download interaction remains outstanding. |
| TS-018 | 8 | Verified (automated) | R1 `test_repairs_identity.py`: saved typed-peer fallback, wrong peer/kind rejection, stale tokens and identity-before-locator retry. Live renamed/revoked locators remain outstanding. |
| TS-019 | 9 | Verified (automated) | R1 `test_repairs_identity.py`: actual Telethon Chat/Channel request resolution and retained full metadata. Real basic-group capture remains outstanding. |
| TS-020 | 11 | Verified (automated) | R1 `test_repairs_media.py`: six real Telethon descriptor/download-selection fixtures including direct/preview document, photo, progressive, video and stripped forms; exact transfer reused, distinct representations retained. Live media remains outstanding. |
| TS-021 | 10 | Verified (automated) | R1 `test_repairs_media.py`: four subprocess-exit boundaries, receipt failure, repeat replay, same-size corruption, legacy exact reuse and symlink preservation; historical/backup verification. Power-loss durability is not established. |
| TS-022 | 8 | Verified (automated) | R1 `test_repairs_identity.py`, `test_repairs_combined.py` and R2: duplicate result with zero capture writes/selection change; separate Open then explicit capture; connected Add, multiple matches, CLI no-wait, cancellation barriers. Live resolution remains outstanding. |
| TS-023 | 13 | Verified (automated) | R2: stale forms changing different fields, reverted/no-op/blank hash, failed credential validation and retry. Interactive multiwindow use remains outstanding. |
| TS-024 | 7 | Verified (automated) | R1 `test_repairs_watch.py`: enqueue during checkpoint processes without another event/timer; Stop/restart control. Live Watch remains outstanding. |
| TS-025 | 14 | Verified (automated) | R1 `test_repairs_combined.py` and R2: replacement/content/media revision, actual Watch checkpoint → browser refresh → shutdown, no-op stability. Interactive refresh remains outstanding. |
| TS-026 | 14 | Verified (automated) | R2: failed refresh preserves rows and retries next poll; aborted/superseded/wrong-library responses never acknowledge. Interactive refresh remains outstanding. |

## Validation, delivery, and completion

**Existing evidence:** Reuse the synthesis’s seven recorded reproductions and applicable original audit results as evidence of the starting failures. The original synthesis probe asserts buggy behavior and verifies the original source hashes; preserve it unchanged. Adapt its scenarios into regression tests with corrected expectations rather than changing the evidence to make it pass.

**Focused validation:** Add regressions beside the relevant storage, integrity, evidence, engine, enrichment, login, channel, and HTTP tests. Exercise actual production JavaScript with controlled responses and a minimal DOM using Node’s built-in test runner. Use synthetic libraries, isolated temporary homes, and loopback fixtures. Where practical, record the regression failing before its production correction and passing afterward. Prefer explicit barriers and startup signals over short sleeps.

**Combined validation:** After the interacting steps are integrated, run:

- Initialization → empty capture/Stop → reopen → retry → snapshot → verification.
- Legacy binding/locator repair → exclusive identity preflight → duplicate result with no capture run → explicit Open existing archive → separately requested capture.
- Login handshake timeout → held disconnect cleanup → shutdown → drained transports/tasks and rejected retries.
- Watch edit/media activity → checkpoint → browser refresh → shutdown.
- Capture preflight → Stop/timeout/shutdown before handoff → no late evidence run or archive switch.
- Primary publication crash → retry → historical-media verification → backup verification.
- Installer receipt failure → repair retry → exact installed-build check.

Then run the Python suite once for the integrated change using the project’s Python 3.14 interpreter, plus the JavaScript regressions. The breadth of these interacting changes justifies that final suite; it does not justify repeating it after every small correction. Investigate failures proportionally and distinguish timing-sensitive fixture failures from product failures.

**Delivery:** During the later authorized implementation task, after automated checks pass, build and install through the verified local delivery path:

```sh
/usr/local/opt/python@3.14/bin/python3.14 tools/build_macos_app.py
python3 tools/install-app.py "telegram-scraper.app" --name "telegram-scraper" --bundle-id local.telegram-scraper.desktop --check
```

Verify that the packaged Python/static files correspond to the repaired source, signing succeeds, the installed receipt and any applicable update-attempt receipt are coherent (with older successful receipt formats accepted), and `~/Applications/telegram-scraper.app` matches that build. Preserve the prior bundle and existing data location. Apply installer changes within this repository; propagating the shared installer to other projects requires separate scope.

A running process still uses its previously loaded code. Never quit it automatically. Report any required save/quit/reopen action. Fresh permission is required before controlling the desktop or performing live capture against the user’s Telegram account.

After reopening the verified build, check main/child attachment downloads, Settings preservation, visible row/media refresh, channel-repair feedback, and Stop/reopen behavior. Use a disposable library where possible. A live authorized-account check is needed to establish real channel/group resolution and Watch behavior; offline fixtures alone do not establish those outcomes.

**No additional user decision is needed to implement this plan.** The Watch fallback is deliberately conservative where protocol evidence cannot establish ordering; the explicit ambiguous-outcome behavior in step 7 must be retained. Ambiguous existing archive damage, uncertain legacy identity, and multiple already-existing archives require explicit choices within the recovery/binding flows; they do not block independent implementation or synthetic tests. Historical bytes already lost cannot be recreated by these corrections.

The repair effort is complete only when every mapped regression passes, interacting checks pass, preservation guarantees hold, the exact installed build and receipts verify, and meaningful live checks are either completed or explicitly reported as outstanding. An incomplete receipt, required restart, or unperformed live check must not be presented as fully verified delivery.


## Implementation log

- 2026-09-25: Before editing, all 49 baseline contents/modes and Git index matched; HEAD unchanged; FINAL.md SHA-256 unchanged. All saved work retained.
- Steps 1-3: Initial TS-001/007 regression run: 2 failed, 1 passed before correction. Focused recovery run: 91 passed, 30 subtests. Both traversal callbacks, real unreadable directory, corrupt DEFLATE, missing/replaced source DB, revision-only references, historical receipts, all three reported ZIP layouts and healthy controls covered. Empty sync/range/cancelled photo now reopen/retry/snapshot/verify. No real archives used. Initialization was subsequently moved behind step 8 preflight.

- Steps 4-6: Catalogue failure reconciliation, cleanup ownership, staged credentials publication and monotonic shutdown admission implemented. Focused suite: 65 passed. Real Telethon initial handshakes used only a loopback listener; three timeouts left no transport. Held HTTP POST bodies were rejected during shutdown. No application was quit.

- Step 7: Deterministic request-start/live-receipt tests, held raw writes, a single overlapping targeted reread, checkpoint wakeups, 100 wake cycles across two Watch runs, and fatal context/raw-write outcomes pass. Amendment: Python 3.14 shield retains a task-debugging callback until disconnection; acquire the public Telethon waiter in a loop callback (no current task), then release its wrapper at run exit. This keeps one shared asyncio exception callback without cancelling the connection future.
- Steps 8-9: Exclusive preflight, typed identity, confirm/repair actions, explicit duplicate Open action and peer-specific metadata requests implemented. Selected archive stays unchanged on duplicate; Stop/shutdown/timeout tests leave no capture run. Legacy test fixtures now explicitly bind their synthetic original channel instead of relying on implicit binding.
- Steps 10-11: Immutable primary intent/completion receipts and exact media descriptor sharing implemented. Four subprocess-exit boundaries plus completion-write failure and same-size corruption/retry checks pass. Six real-Telethon media-selection fixture cases pass. The complete R1 group and final R3 suite pass.
- Steps 12-15: HTTP response commitment, URL parameters, dirty settings fields, library revision/acknowledgement, and installer receipt reconciliation implemented. R1 and R2 pass. Signed installer fixtures also cover metadata-collection failure after swap so an incomplete outcome remains visible in saved receipts.

- Combined validation: all seven planned sequences passed in R1/R2, including real loopback handshake timeout → held cleanup → shutdown. Source inference checks the saved channel object checksum before trusting its peer kind. Version 1.3.4 and recovery guidance delivered after R3 passed. No live capture or desktop automation performed.


## Final delivery and remaining acceptance checks

- **D1:** `/usr/local/opt/python@3.14/bin/python3.14 tools/build_macos_app.py` — exit 0; signed and installed version **1.3.4** at `/Users/xxx/Applications/telegram-scraper.app`. [Build/install log](implementation/build-install.log).
- `python3 tools/install-app.py "telegram-scraper.app" --name "telegram-scraper" --bundle-id local.telegram-scraper.desktop --check` — exit 0, **verified**, exact bundle match and complete coherent installed/update receipts. [Check log](implementation/installed-check.log).
- [Delivery verification](implementation/delivery-verification.json): all **18** Python/static file hashes match working source, built bundle and installed bundle. Installed CDHash `3dc8531ee70ecac07feafe800ca310a41757be65`; original bundle identifier and ad-hoc signing team unchanged. The launcher still points to this repository/library.
- Previous installed bundle retained at `/Users/xxx/Library/Application Support/My Utilities/Updates/20260925T031859-7e11e6fa1216/telegram-scraper.app.previous`; previous source bundle retained at `/Users/xxx/GitHub/telegram-scraper/work/app-backups/telegram-scraper-previous-6dc73699.app`. Both signatures match the pre-build record. No running app/backend was found at final verification; no process was quit. Open the Applications build to review the update.
- `git diff --check` passed. [Preservation record](implementation/preservation.json): FINAL.md and all eight original audit hashes match their saved evidence; HEAD and Git index are unchanged. Saved uncommitted work remains in the checkout. No branch switch, stash, discard, commit or push was performed. No real archive was modified and no live Telegram operation or GUI automation was performed.
- **Outstanding, not passed:** interactive macOS checks for main/child downloads, Settings, row/media refresh, channel repair feedback and Stop/reopen; real authorized-account checks for renamed/revoked locator resolution, basic groups, media and Watch. These require the user's explicit authorization and are not implied by the synthetic, loopback or packaged-file results. Crash probes model process termination, not hardware power loss. Existing damaged/ambiguous archives and already-lost bytes were not repaired.

Continuation point: implementation and delivery are complete. The remaining work is the explicitly outstanding interactive/live acceptance above; do not rerun the audits or modify real archives without authorization.
