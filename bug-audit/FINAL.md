# Telegram Scraper — verified bug synthesis

Verified on **2026-09-19**. This is the primary list of confirmed bugs for later fix planning. The eight original reports remain unchanged as supporting evidence. No fixes or implementation plan are included.

## Reviewed version, coverage, and result

**26 distinct confirmed bugs: 1 P1 and 25 P2.** The 27 original findings have these dispositions: **26 Confirmed, 1 Merged into another confirmed finding, 0 Rejected, 0 Unverified**. Every original finding is accounted for in the disposition table below.

The reviewed project is `/Users/xxx/GitHub/telegram-scraper`. Its state is commit `188afe9fcfa3f9b1963edc23f5e46e143abd2632` plus the **nine modified tracked files and four untracked files** recorded at launch. All **49 source/project files**, including the saved login, preview-media, and installation changes, matched the launch SHA-256 values and file modes during this synthesis. There were no additional source files outside `bug-audit/`. Thus the current code being assessed is the original audited working state; this conclusion does not rely on the branch name or commit alone.

- [Launch source and prompt baseline](/Users/xxx/GitHub/telegram-scraper/bug-audit/launch-20260919T063437Z/source-baseline.json), aggregate fingerprint `242ed2ba644b15c5289ed13be6cd20403eb6f72a8e57a2b0e9f3cc881ae007c1`.
- [Audit task and report index](/Users/xxx/GitHub/telegram-scraper/bug-audit/AUDIT-TASKS.md).
- [Preserved synthesis probes](/Users/xxx/GitHub/telegram-scraper/bug-audit/evidence/synthesis-probes.py) and [observed results, environment, and original-report hashes](/Users/xxx/GitHub/telegram-scraper/bug-audit/evidence/synthesis-probe-results.json).

All eight expected reports were readable, nonempty, and presented completed findings. Coverage is limited to their reported race, stale-state, retry, atomicity, invariant, lifecycle, persistence, and error-handling defects and the paths needed to assess them. This is not an additional general audit or a certification that no other bugs exist.

**Verification standard.** Each retained finding was checked against its current callers, guards, failure path, and state changes. Agreement between reports was not used as proof. “Reused reproduction” below means an executed result recorded in the preserved original audit, checked for applicability to the unchanged code; it does not mean that execution was repeated here. “New reproduction” means a case executed during this synthesis. Code locations below refer to the verified working files, not clean-commit versions.

**Severity.** P1 denotes a serious silent failure of recovery protection that warrants prompt attention. P2 denotes a concrete correctness, availability, resource, or reporting defect with the conditions stated in its entry. Severity reflects demonstrated effects, not hypothetical loss of unrelated data. Within P2, numbering is for stable reference rather than a prescribed implementation order.

## Confirmed bugs

<a id="ts-001"></a>
### TS-001 — P1: Recovery snapshots silently omit unreadable subtrees

**Severity justification:** A backup can be published as complete while excluding existing files whose directory could not be read. Trusting that backup for recovery can leave those bytes unavailable; no error warns the user that protection failed.

**Code:** [storage.py:346](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:346), first traversal; [storage.py:389](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:389), validation traversal; [storage.py:404](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:404), publication; [engine.py:1037](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1037), pre-sync snapshot caller.

**Conditions and path:** An existing `telegram_data` subtree raises a directory-enumeration error. Both `os.walk(..., followlinks=False)` calls omit an `onerror` handler, so both skip the same subtree. Their file-set comparison still agrees, and `snapshot()` publishes the ZIP normally.

**Expected versus actual / impact:** A snapshot that cannot inspect all intended source files should fail before publication. Instead it can return a complete-looking archive without the unreadable subtree. A later verification also passes when the omitted file has no current top-level receipt or earlier backup establishing its presence. The source bytes are not deleted by snapshot creation; the demonstrated defect is silent incomplete recovery coverage.

**Root cause:** Enumeration failure is treated as an empty directory in both inventory passes.

**Verification and origin:** Independently traced both walks and publication. **New reproduction** `unreadable_subtree` confirmed a real `PermissionError` as UID 501, a published ZIP containing only `messages_all.json`, and `verify_ok=true` with no issues. Reused [08-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:7) establishes the same result.

**Suggested fix direction:** Propagate traversal errors from both passes and prevent publication of an incompletely enumerated snapshot.

<a id="ts-002"></a>
### TS-002 — P2: First channel-photo capture can create an archive that refuses to reopen

**Severity justification:** An ordinary first capture or Stop can block further capture until the archive is repaired. The demonstrated cases concern a new archive and do not establish loss of existing posts; this narrows audit 04-01's P1 rating to P2.

**Code:** [engine.py:1049](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1049), photo before history; [engine.py:585](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:585), dirty-only save; [enrichment.py:430](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:430), directory creation; [storage.py:170](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:170), missing-index guard; [server.py:530](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:530), capture rejection.

**Conditions and path:** A fresh archive has no message index and context/media capture is enabled. Photo capture runs first and creates `media/variants/index`. Either (a) the photo succeeds but an empty channel or first date range yields no posts, or (b) Stop interrupts the photo before any post is processed. `_dirty` remains false, so final `_save()` creates no index. The next load interprets even the remaining empty `variants` directory as orphaned saved media.

**Expected versus actual / impact:** Empty or cancelled initialization should leave a reopenable archive. Instead it reports that `messages_all.json` must be restored, although that file never existed; subsequent sync and app reopening cannot recover through the normal capture path.

**Root cause:** Independent channel-media publication is allowed before the empty message-index state is durably established, while the loader assumes any media-directory content requires an existing index.

**Verification and origins:** **New reproductions** `first_photo_empty_sync` and `first_photo_cancelled` ran the copied production engine with offline fixtures. They returned `completed` and `cancelled` respectively, processed zero posts, created no index, and both failed a fresh `Store.load()`. Reused [04-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:5) and [07-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:5) additionally document cancellation/retry and the empty-date-range trigger. These are merged because the state defect and correction are the same.

**Suggested fix direction:** Establish a durable empty index before independent media publication, or explicitly recognize a valid source-only initialization state while retaining the missing-index protection for established archives.

<a id="ts-003"></a>
### TS-003 — P2: Missing modern source history is silently treated as an uninitialized database

**Severity justification:** A real loss or incomplete restore is accepted as healthy and then extended, obscuring the missing evidence and invalidating the continuity of saved source references.

**Code:** [evidence.py:271](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py:271), absence accepted; [evidence.py:303](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py:303), prepare; [evidence.py:317](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/evidence.py:317), initialization; [integrity.py:156](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/integrity.py:156), integrity caller; [engine.py:1012](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1012), next capture.

**Conditions and path:** A modern message index references source observations, but `evidence.sqlite3` is missing and no SQLite journal sidecar remains. Validation returns `ok=true, exists=false` without checking those references. Capture calls `prepare()` and then `begin_run()`, which initializes a new database.

**Expected versus actual / impact:** A modern archive with missing source history should be identified and require recovery. Instead a source integrity check can pass and a new observation sequence begins while the old index survives. Earlier TL bytes, observations, occurrences, and receipts are absent. The app does not cause the initial file loss; the defect is failing to distinguish it from a genuine legacy/new archive. Actual erroneous reassignment of a particular old ID was not required or demonstrated.

**Root cause:** Database existence is assessed without a durable archive-format marker or cross-check of message source references.

**Verification and origin:** Current absence, validation, and creation paths checked. Reused [07-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:27): after removing only the fixture database, verification passed; the next database had zero observations and one new run, despite an index and a backup containing the original history.

**Suggested fix direction:** Distinguish modern evidence-bearing archives from legacy ones, reject unexpected database absence, and validate persisted source references before further capture.

<a id="ts-004"></a>
### TS-004 — P2: Integrity verification omits attachments retained only in revision history

**Severity justification:** A previously downloaded, still-referenced attachment can be absent locally and from the available backup while the check reports success.

**Code:** [engine.py:548](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:548) and [engine.py:564](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:564), retained media receipts; [storage.py:481](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:481), local checks; [storage.py:577](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:577), ZIP checks; [storage.py:587](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:587), earlier-backup comparison.

**Conditions and path:** An attachment is replaced or removed, moving its path/size/hash into `previous_media` and revisions. The physical file later disappears, and no retained earlier ZIP contains it. Both record loops check only top-level `media_file`; cumulative checks have no earlier bytes to compare.

**Expected versus actual / impact:** Recorded historical receipts should detect a missing saved file. Instead the archive and its first later snapshot can pass with that file absent. This is reachable with snapshots disabled or an attachment captured and retired between snapshots, including within one Watch run.

**Root cause:** Media-receipt traversal excludes revision/history fields. The source/variant verifier does not fill this gap for primary attachments.

**Verification and origin:** Checked ingestion retention and both verification loops, including the earlier-ZIP safeguard. Reused [07-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:63): real ingestion retained both historical references, but deleting the fixture attachment and taking the first snapshot yielded `ok=true`, zero checked media, and no issues.

**Suggested fix direction:** Validate deduplicated file reads against receipts from current records, revisions, and `previous_media`, for both local files and each backup.

<a id="ts-005"></a>
### TS-005 — P2: ZIP verification accepts missing or invalid archived source databases

**Severity justification:** A recovery archive can be declared verified despite being unable to restore its recorded source evidence.

**Code:** [storage.py:547](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:547), member reads; [storage.py:564](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:564), required-content check; [storage.py:594](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:594), archive counted; [storage.py:473](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:473), separate live-database verification.

**Conditions and path:** A structurally valid ZIP has valid CRCs but either contains invalid SQLite bytes, omits the database despite source-bearing messages, or contains an invalid database without any message index. Only the index and channel JSON are decoded; the database's presence can satisfy the content requirement without its payload being validated.

**Expected versus actual / impact:** Backup verification should establish that required source state is readable and consistent. Instead all three layouts can pass while the live database is healthy. This concerns invalid payloads inside intact ZIP containers, not CRC evasion by arbitrary compressed-byte damage.

**Root cause:** Container/member reading is substituted for archived-database schema, integrity, checksum, and reference validation.

**Verification and origin:** **Three new reproductions** (`invalid_database`, `missing_database`, `source_only_invalid_database`) each passed `ZipFile.testzip()` and `Store.verify()`, with one checked archive and no issues. Reused [07-05](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:79). Distinct from TS-003, which concerns the live database and capture initialization.

**Suggested fix direction:** Validate an isolated copy of each archived database and require it for source-bearing indexes; retain deliberate support for genuine legacy and valid source-only backups.

<a id="ts-006"></a>
### TS-006 — P2: A streaming read error can produce an apparently successful corrupted download

**Severity justification:** A client can accept the declared byte length and HTTP success while saving response-header bytes in place of file data. Existing source bytes are not modified.

**Code:** [server.py:830](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:830), response starts; [server.py:846](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:846), streaming; [server.py:738](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:738), outer error response; [app.js:1068](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:1068), export consumer.

**Conditions and path:** A media/export file opens successfully, headers with HTTP 200/206 and `Content-Length` are sent, and a later read raises. `do_GET()` writes a second HTTP response to the same stream. If the remaining advertised body length fits within that second response, the client reads a full-length body without a truncation error.

**Expected versus actual / impact:** An error after response commitment should terminate an incomplete transfer. Instead it can silently corrupt a downloaded media file or export while retaining the first success status. This affects shared `send_file()` callers; the observed reproduction used media, not every export route separately.

**Root cause:** Exception handling does not distinguish failures before headers from failures inside an already-started response.

**Verification and origin:** Traced the streaming and error boundaries. Reused [08-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:32): a real loopback response returned HTTP 200 and all 1,048,676 declared bytes, with the final 100 bytes beginning `HTTP/1.0 500 Internal Server Error` after an injected second-read EIO.

**Suggested fix direction:** Track response commitment and close failed streams after headers; send structured error responses only before commitment.

<a id="ts-007"></a>
### TS-007 — P2: A corrupt DEFLATE member aborts verification instead of identifying its backup

**Severity justification:** One ordinary corruption case suppresses the structured result and prevents independent later backups from being checked.

**Code:** [storage.py:549](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:549), decompression; [storage.py:597](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:597), per-backup handler; [storage.py:627](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:627), outer handler; [server.py:566](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:566), generic job failure.

**Conditions and path:** A ZIP member contains an invalid DEFLATE block. Reading raises `zlib.error`, which neither verification exception list catches. Execution leaves the archive loop and the runtime replaces it with a generic operation failure.

**Expected versus actual / impact:** The corrupt ZIP should produce a named issue while later independent backups continue. Instead no verification result survives and later backups are unexamined. This is separate from TS-005's failure to validate semantically invalid database contents in otherwise readable ZIPs.

**Root cause:** The per-backup corruption boundary omits the decompressor's exception type.

**Verification and origin:** **New reproduction** `invalid_deflate` changed only the synthetic first member's block-type bits: `zlib.error: ... invalid block type` escaped and `02-good.zip` was never reached. Reused [08-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:90) additionally verifies the runtime's generic error and absent result.

**Suggested fix direction:** Handle decompressor corruption at the per-backup boundary, retain a named issue, and continue independent checks.

<a id="ts-008"></a>
### TS-008 — P2: Legacy message archives cannot establish their channel identity through supported setup

**Severity justification:** An existing old-format archive remains browseable but cannot continue or enrich its capture without manual metadata intervention.

**Code:** [storage.py:289](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:289), legacy binding; [server.py:479](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:479), settings guard; [config.py:12](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:12), defaults; [config.py:101](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:101), allowed settings; [engine.py:1034](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1034), binding caller.

**Conditions and path:** A legacy index has records but no `channel.json`. With no channel setting, the runtime rejects entering the original channel because existing records activate its no-rebinding guard. Even if the channel is already configured, `bind_channel()` requires a matching `legacy_channel`; defaults/setup do not create it and `Settings.update()` rejects it. The test helper injects it directly at [test_engine.py:117](/Users/xxx/GitHub/telegram-scraper/tests/test_engine.py:117).

**Expected versus actual / impact:** A controlled legacy-import path should establish the verified original peer. Instead the normal setup/sync flow cannot satisfy its own prerequisite. Add channel creates a different archive and does not migrate these records. A pre-existing manually provisioned matching `legacy_channel` avoids this trigger.

**Root cause:** The safety guard requires migration metadata that the supported configuration flow cannot establish.

**Verification and origin:** Checked the configuration, binding, runtime, and CLI call paths and searched production references to `legacy_channel`; no writing migration path was present. Reused all three cases in [07-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:45): empty-settings rejection, configured-channel binding rejection, and rejection of the missing field itself.

**Suggested fix direction:** Provide controlled original-channel establishment for legacy imports while preserving the prohibition against mixing unrelated channels.

<a id="ts-009"></a>
### TS-009 — P2: Older queued Watch messages can replace newer history results

**Severity justification:** The current reading index can regress persistently, including text and counters, although raw observations remain retained.

**Code:** [engine.py:933](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:933), scan before queue drain; [engine.py:520](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:520), record replacement; [engine.py:680](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:680), unconditional upsert; [storage.py:213](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:213), merge/revision policy.

**Conditions and path:** Startup catch-up, or overflow recovery, queues an older observation of a post before history returns a newer version of the same ID. History applies the newer result; the later queue drain applies the older result without freshness arbitration. Processing assigns a new `last_observed_at` to that older payload.

**Expected versus actual / impact:** Preserving every observation should not make an older one current solely because it was processed later. Reopening the saved index can show older text or counters, and the newer text can appear as a prior revision. Equal-edit-date counter snapshots also trigger the defect, so an edit-date-only correction is insufficient. Both raw observations survive.

**Root cause:** Queue processing order is used as current-state authority without comparing observation freshness across history and updates.

**Verification and origin:** Traced the sequential path; concurrency inside `_process()` is not necessary. Reused [02-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:7): the real Watch flow persisted older edited text and separately regressed views from 9 to 1 with an unchanged edit date.

**Suggested fix direction:** Preserve source observations while arbitrating which can replace current derived state, covering equal-edit-date snapshots and history/update ordering.

<a id="ts-010"></a>
### TS-010 — P2: A Watch source-write failure can be finalized as user cancellation

**Severity justification:** A received post's failed raw capture can lose its actionable failure cause from the final status and durable run receipt.

**Code:** [engine.py:911](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:911), stored failure; [engine.py:189](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:189), failure-aware check; [engine.py:1047](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1047), collector callback; [enrichment.py:43](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:43), cancellation conversion; [engine.py:1058](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1058), outcome classification.

**Conditions and path:** While the collector awaits a context disk operation, a Watch callback fails to persist another raw observation. The receiver stores `_watch_failure` and sets `_cancel`. The collector subsequently consults only `_cancel.is_set`, raises `CancelledError`, and `run()` finalizes cancellation without consulting the stored failure.

**Expected versus actual / impact:** The operation should expose the evidence-write failure. Instead it can return normally with `status=cancelled`, `stopped=true`, `failure=null`, and no run issue naming the cause; no user Stop is required. An interrupted-processing event can still exist, so the claim is loss of the actual cause and incorrect terminal classification, not absence of every interruption record.

**Root cause:** The collector bypasses the engine's failure-aware cancellation check, and final cancellation classification does not restore that distinction.

**Verification and origin:** Checked both cancellation mechanisms and receipt finalization. Reused [08-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:58): barrier-controlled context capture plus a failed Watch append produced the cancelled receipt and no raw observation for the incoming post.

**Suggested fix direction:** Propagate the failure-aware check to the collector or consult `_watch_failure` before finalizing a cancellation.

<a id="ts-011"></a>
### TS-011 — P2: Catalogue rollback deletes a folder after the catalogue already references it

**Severity justification:** An I/O failure leaves a durable channel entry unusable, and ordinary retry/restart cannot repair it through Add channel.

**Code:** [storage.py:134](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:134), replace before directory flush; [channels.py:66](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:66), memory/digest update; [channels.py:90](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:90), allocation and rollback; [channels.py:53](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:53), missing-folder guard.

**Conditions and path:** Adding a channel creates an empty folder and publishes a new catalogue. The following directory fsync fails. `_atomic_json()` raises after `os.replace`; `_save()` leaves memory/digest old, and `add()` removes the folder as if publication had never occurred.

**Expected versus actual / impact:** Failure handling should preserve a coherent old or new state. Instead the disk catalogue points at a deleted directory. The live digest also becomes stale. After restart, re-adding that same channel returns the existing key without recreating its folder, and selection fails.

**Root cause:** Rollback does not distinguish failure before the visible commit point from failure after it.

**Verification and origin:** Verified the exact publication, exception, and re-add paths. Reused [04-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:36), which injected only the post-replacement directory-flush EIO and observed the dangling catalogue entry and failed selection.

**Suggested fix direction:** Reconcile publication state before rollback and keep the folder, on-disk catalogue, and in-memory digest consistent when durability confirmation fails.

<a id="ts-012"></a>
### TS-012 — P2: Failed client shutdown leaves newly saved credentials paired with the old client

**Severity justification:** A subsequent successful settings retry can leave the visible/persisted API identity different from the identity the client actually uses.

**Code:** [server.py:487](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:487), settings and identity comparison; [engine.py:1099](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1099), close; [engine.py:384](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:384), client reuse; [config.py:138](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:138), settings publication.

**Conditions and path:** API credentials change and are saved, then disconnect raises before clearing the service/client. Retrying the same new settings compares against the already-updated settings, sees no identity change, and only updates `service.settings`. The constructed Telethon client's identity is unchanged.

**Expected versus actual / impact:** Success should mean the active client corresponds to saved credentials, or the UI should explicitly remain disconnected until replacement succeeds. Instead old credentials and a stale authorized flag can survive both the failure and a successful retry. This is an API-client configuration mismatch, not proof that Telegram switched or exposed a different user account.

**Root cause:** Settings publication precedes fallible client replacement, and retries use settings equality rather than the retained client's identity.

**Verification and origin:** Traced save, exception, and retry; inspected installed Telethon's SQLite session close, which commits and can raise. Reused [04-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:64): a real offline client retained API ID 111 after saved/service settings became 222.

**Suggested fix direction:** Make client invalidation/replacement converge after disconnect failure, based on actual client identity and explicit connection state.

<a id="ts-013"></a>
### TS-013 — P2: Final installation-receipt failure reports failure after the app swap has committed

**Severity justification:** The caller and recovery metadata misrepresent the installed state. The old app remains retained; this does not establish lost app bytes or a changed running process.

**Code:** [install-app.py:150](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:150), swap; [install-app.py:171](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:171), rollback handler; [install-app.py:185](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:185), final receipt outside handler; [install-app.py:247](/Users/xxx/GitHub/telegram-scraper/tools/install-app.py:247), exit status; [build_macos_app.py:91](/Users/xxx/GitHub/telegram-scraper/tools/build_macos_app.py:91), checked subprocess.

**Conditions and path:** Candidate exchange and verification succeed, the previous app moves to recovery, then the final recovery-receipt write fails, for example ENOSPC. That write is outside rollback protection. Installed-receipt creation and running-process reporting have not happened; the CLI exits 1.

**Expected versus actual / impact:** The outcome should accurately distinguish a committed installation with incomplete metadata from an installation that did not occur, or maintain the intended rollback guarantee. Instead the destination contains the new app while the earlier receipt still says `preparing` and the caller sees generic failure. The already-running process is not replaced by changing the bundle path.

**Root cause:** The app's commit boundary and the required receipt/reporting boundary are inconsistent.

**Verification and origin:** Independently confirmed from executable control flow, without relying on delivery-document promises. Reused [04-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:96), whose prior disposable signed-bundle probe observed new installed signature, retained old signature, `preparing` receipt, and no installed receipt. **No build, signing, or installation was run in this synthesis.**

**Suggested fix direction:** Define and report the post-swap state explicitly and make required receipt failures recoverable without falsely implying the destination stayed unchanged.

<a id="ts-014"></a>
### TS-014 — P2: An already-accepted job request can start new work after shutdown drains jobs

**Severity justification:** A request can be acknowledged and then abandoned with an unfinished run receipt when the event loop closes. Previously committed-record corruption was not demonstrated.

**Code:** [server.py:524](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:524), admission; [server.py:583](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:583), shutdown; [server.py:610](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:610), daemon handlers; [server.py:761](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:761), request-body wait; [cli.py:145](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/cli.py:145), close order.

**Conditions and path:** An authenticated job POST is accepted before listener closure but supplies its body while `_close()` awaits `service.close()`. Shutdown has already passed its busy/task drain. `start_job()` has no closing guard, admits the job, and the engine clears cancellation. Shutdown then finishes without draining that newly created task and closes the loop.

**Expected versus actual / impact:** Shutdown should reject further mutations and drain all admitted work. Instead a client can receive success for a job still marked running when teardown finishes, leaving its normal completion/cancellation path unexecuted.

**Root cause:** Listener closure is treated as sufficient admission closure, despite already-active request handlers and no runtime closing state.

**Verification and origin:** Traced the await/admission window and public loop close. Reused [RC-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/01-race-conditions.md:50): real loopback HTTP returned 200; `_close()` finished with a pending job, `running` evidence receipt, cleared cancellation, and shut-down executor. The probe inspected this boundary and then drained its fixtures; it did not establish earlier-data corruption.

**Suggested fix direction:** Close mutation admission before the first shutdown await and drain all admitted operations before executor/loop teardown.

<a id="ts-015"></a>
### TS-015 — P2: Retrying a timed-out first login leaks prior connection transports

**Severity justification:** Repeated qualifying login timeouts retain sockets and transport tasks until independent failure or process exit.

**Code:** [engine.py:405](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:405), unprotected connection await; [server.py:87](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:87), timeout cancellation; [server.py:500](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:500), authentication lifecycle; [engine.py:1099](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1099), later close. Dependency: [Telethon sender connect](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:123) and [handshake path](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:228).

**Conditions and path:** TCP connects for a new session, but initial auth-key negotiation stalls until the runtime timeout cancels it. The app does not disconnect the partial transport. Telethon still reports not connected; retry replaces its sender's transport reference. The prior transport tasks retain the old connection, and later service close reaches only the newest one.

**Expected versus actual / impact:** Cancellation should dispose of partially established transport resources before retry. Instead each repeated timeout can add a socket and two tasks. The finding is specific to that connection-establishment window, not every ordinary login failure.

**Root cause:** Cancellation cleanup is missing around partial connection establishment; the dependency does not clean this particular cancellation path automatically.

**Verification and origin:** Current app code and installed Telethon 1.42.0 connection creation, handshake, and disconnect implementations checked. Reused [LC-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/06-lifecycle-cleanup.md:82), including its preserved runnable loopback probe: three shortened timeouts left three sockets/six tasks, and closing the service left two/four. No live outage or production resource total is inferred.

**Suggested fix direction:** Drain partial-connection disconnect cleanup on failure/cancellation before retry or replacement, while preserving cancellation propagation.

<a id="ts-016"></a>
### TS-016 — P2: Watch retains an additional disconnection waiter on every wait cycle

**Severity justification:** Long-lived connected Watch sessions accumulate unbounded wrapper futures/callbacks; Stop alone does not release them.

**Code:** [engine.py:963](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:963), waiter creation/cleanup; installed [Telethon disconnected property](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:208).

**Conditions and path:** Each wait reads `client.disconnected`, which already returns a fresh shield of the persistent sender future. The app shields that returned future again and cancels only the outer shield in `finally`. The inner wrapper remains attached until the underlying connection ends.

**Expected versus actual / impact:** Completed waits should release their per-wait resources while preserving the shared connection future. Instead timer/message wakeups accumulate retained wrappers and callbacks across Watch stop/restart. In normal Watch, waits occur in the same job task; the separate finished-task retention counts in the original probe are a measurement device, not a claim of one production job task leaked per wakeup.

**Root cause:** A nested shield creates an intermediate waiter with no owner responsible for cancelling it.

**Verification and origin:** Independently inspected both shield layers and cleanup against Python 3.14.5 / Telethon 1.42.0. Reused [LC-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/06-lifecycle-cleanup.md:7): 100 completed waits retained 200 callbacks; resolving the underlying future released them. Its full probe remains in the original report.

**Suggested fix direction:** Give the returned waiter an explicit bounded lifetime without cancelling the sender's actual disconnection future.

<a id="ts-017"></a>
### TS-017 — P2: Every generated primary-attachment download link corrupts its library parameter

**Severity justification:** The ordinary Download file action fails even for a healthy saved attachment and correctly selected archive.

**Code:** [app.js:56](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:56), URL helper; [app.js:872](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:872), source URL; [app.js:888](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:888), download URL; [server.py:664](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:664) and [server.py:145](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:145), library extraction/guard.

**Conditions and path:** A detail dialog renders a saved primary attachment. `archiveURL()` has already appended `?library=...`; the link then appends another `?download=1`. Query parsing treats that suffix as part of the library value, so the server rejects it.

**Expected versus actual / impact:** Download should return the attachment for the selected library. Instead `/api/media/1?library=main?download=1` returns HTTP 400, as does the child-archive equivalent. The preview URL works. Refreshing cannot fix URL construction, and the video-player fallback points to the same broken action. Variant links are outside this claim.

**Root cause:** Query parameters are assembled with an unconditional second question mark.

**Verification and origin:** Checked renderer, URL helper, parser, and selection guard. Reused [05, finding 1](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:5): actual renderer output failed through a real temporary server for main and child archives; replacing only the separator with `&` returned the exact file bytes.

**Suggested fix direction:** Compose the download parameter with `URL.searchParams` while keeping the library guard intact.

<a id="ts-018"></a>
### TS-018 — P2: An existing archive cannot repair a changed channel username

**Severity justification:** A still-accessible saved channel becomes unsyncable through that archive's supported UI/API after its configured locator stops resolving.

**Code:** [engine.py:464](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:464), resolution; [server.py:479](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:479), locator immutability; [storage.py:279](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:279), saved ID guard.

**Conditions and path:** A bound archive uses an old username. Resolution retries that configured string, without using its saved channel ID. The numeric fallback only applies when the configuration was already a marked numeric ID. Settings rejects a different normalized locator before resolving whether it denotes the same saved channel.

**Expected versus actual / impact:** Identity should remain fixed while a verified locator may change. Instead the old archive cannot continue; adding the new locator creates another archive. The confirmed executed case is username change. The invite-link branch also depends on its original locator by code trace, but no additional live invite scenario is claimed.

**Root cause:** The safety boundary confuses immutable Telegram peer identity with an immutable human-readable locator.

**Verification and origin:** Resolution and update guards checked; installed Telethon's string-resolution behavior is consistent with the path. Reused [05, finding 2](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:32): only `@oldname` was attempted; both a new username and numeric ID resolving to saved peer 123 were rejected as repairs.

**Suggested fix direction:** Resolve using bound identity or allow locator repair after verifying it resolves to the existing peer; do not allow unrestricted rebinding.

<a id="ts-019"></a>
### TS-019 — P2: Accepted basic groups use an incompatible full-metadata request

**Severity justification:** An accepted source type repeatedly produces incomplete capture and warning status even when its history is accessible.

**Code:** [engine.py:505](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:505), acceptance; [engine.py:337](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:337), unconditional channel request; [engine.py:1041](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1041), caller; [engine.py:1076](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1076), warning outcome.

**Conditions and path:** The resolved peer is a titled Telethon `Chat` (basic group). `_entity()` accepts it, but `_channel_context()` sends `channels.GetFullChannelRequest`. Telethon cannot cast its `InputPeerChat` to `InputChannel` and raises before sending the metadata RPC. The engine records an issue and continues without `_full_channel`.

**Expected versus actual / impact:** Accepted groups should receive the appropriate metadata request. Instead full group metadata and its photo path are missed; otherwise successful history traversal ends in warning with `capture_complete=false`. Channels and supergroups use `Channel` and are not affected by this particular cast.

**Root cause:** Metadata acquisition is not dispatched by peer type.

**Verification and origin:** Checked acceptance and the installed request's actual `resolve()` implementation. Reused [05, finding 3](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:69): real request resolution failed with the stated cast error, while `GetFullChatRequest(chat_id=456)` resolved under the same offline fixture.

**Suggested fix direction:** Select full-chat versus full-channel metadata requests from the resolved peer type and preserve the existing raw/photo capture paths.

<a id="ts-020"></a>
### TS-020 — P2: Detailed capture acquires the same primary preview video or photo twice

**Severity justification:** Qualifying initial captures waste bandwidth and storage, potentially doubling a large primary attachment's transfer and saved copies.

**Code:** [engine.py:713](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:713), primary acquisition; [engine.py:734](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:734), collector follows; [enrichment.py:315](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:315), primary selection; [enrichment.py:363](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:363), inventory; [enrichment.py:395](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:395), skip decision.

**Conditions and path:** Downloads and detailed capture are enabled. A preview's primary document is nested in `media.webpage.document`, but inventory marks only `media.document` as already managed. Photo sizes are never marked as the primary download. After the primary succeeds, the variant collector requests that same location again and searches only its own cache.

**Expected versus actual / impact:** Distinct variants should be acquired while reusing an already-verified matching primary file. Instead affected preview videos and selected photo sizes are downloaded and allocated twice; backups include both. Subsequent syncs can reuse both copies. This is not a claim that every thumbnail or alternate variant is redundant.

**Root cause:** Primary and variant acquisition do not share the same exact media-object/size identity and file receipt.

**Verification and origin:** Checked inventory flags and acquisition order. Reused [03, finding 1](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:3): actual Telethon selection methods made one transfer for a direct-document control, but two for the same preview-document location and two for the selected photo location, creating equal-byte copies.

**Suggested fix direction:** Reuse the primary receipt for the matching object/size while continuing to save genuinely distinct variants.

<a id="ts-021"></a>
### TS-021 — P2: Crash replay after primary publication keeps another complete duplicate on each retry

**Severity justification:** Repeated interruptions can amplify storage and network use for already-completed large transfers; existing copies are preserved rather than overwritten.

**Code:** [engine.py:624](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:624), verified bytes; [engine.py:631](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:631), publication; [engine.py:633](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:633), suffix allocation; [engine.py:699](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:699), retry decision; [engine.py:727](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:727), index checkpoint.

**Conditions and path:** The process terminates after `os.link()` publishes a verified attachment but before its record is durably updated. The saved record still lacks a usable file receipt. Next sync downloads again; an existing destination always causes another suffix, without comparing completed bytes or reconciling prior publication.

**Expected versus actual / impact:** Replay should safely recognize a completed identical acquisition. Instead two interrupted attempts plus a successful retry can leave three full copies, with the index referencing only the last. This was process termination, not a simulated power-loss durability claim.

**Root cause:** Primary publication has no durable replay/reconciliation receipt, and filename conflict handling does not test verified equality.

**Verification and origin:** Publication-to-checkpoint interval and retry lookup checked. Reused [03, finding 2](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:23): two subprocess exits immediately after the real link plus one normal run produced three distinct inodes and one SHA-256.

**Suggested fix direction:** Reconcile durable media identity/receipt information on retry and compare verified existing candidates before suffix allocation; preserve genuinely conflicting files.

<a id="ts-022"></a>
### TS-022 — P2: Alternate supported locators for the same channel create duplicate archives

**Severity justification:** An apparently duplicate-safe Add channel action can create an empty second archive and repeat history/media capture for the same peer.

**Code:** [channels.py:76](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:76), string deduplication; [server.py:187](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:187), creation/selection; [engine.py:1031](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1031), later resolution; [storage.py:279](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:279), folder-local identity guard; [links.py:32](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/links.py:32), private ID normalization.

**Conditions and path:** A channel is already archived under a username. The user adds a supported numeric/private message link resolving to that same peer. Normalized strings differ, so a new folder is committed before resolution. Binding later checks only that folder, not other saved archives' peer IDs.

**Expected versus actual / impact:** Once peer identity is known, adding another representation should resolve to the existing archive. Instead two archives bind to the same ID, store duplicate media, and subsequently update independently. Same-string/case/public-link normalization does work; the defect crosses locator representations requiring identity resolution.

**Root cause:** Catalogue uniqueness is based only on normalized locator strings, without reconciliation against bound peer identities.

**Verification and origin:** Checked normalization, add, selection, and binding; existing duplicate handling establishes the intended deduplication behavior. Reused [03, finding 3](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:45): `@fixture` and `https://t.me/c/123/42` created two archives bound to ID 123 and separately downloaded identical media.

**Suggested fix direction:** Reconcile resolved IDs before committing duplicate capture, reopening the existing archive while preserving already-created data. This differs from TS-018's repair of one archive's obsolete locator.

<a id="ts-023"></a>
### TS-023 — P2: An older Settings form silently overwrites another tab's saved changes

**Severity justification:** Sequential successful saves can undo a user's saved capture preferences without an explicit conflict.

**Code:** [app.js:669](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:669), form snapshot; [app.js:696](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:696), whole-form submission; [server.py:467](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:467), serialized update; [config.py:105](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:105), value replacement.

**Conditions and path:** Two tabs open Settings for the same archive. A saves a changed preference. B edits a different field in its older form and submits every editable value. State polling does not rebase that open form, and the server has no expected settings revision.

**Expected versus actual / impact:** Untouched stale fields should not silently replace newer choices. Instead B can re-enable downloads that A disabled while saving its own backup preference. The writes can be strictly sequential, so operation serialization and channel guards do not prevent it. A blank hash is retained specially; this is not a claim that every field, including the hidden hash, is overwritten identically.

**Root cause:** Full-form writes have neither dirty-field semantics nor optimistic version checking.

**Verification and origin:** Checked form population/submission and the server/config merge. Reused [02-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:89): both scoped saves succeeded and reopening settings showed B had restored `download_media=true` while disabling snapshots.

**Suggested fix direction:** Submit fields changed from the opening snapshot or reject stale whole-form updates using a settings revision.

<a id="ts-024"></a>
### TS-024 — P2: Watch clears a notification that arrives during its checkpoint

**Severity justification:** Already-received work can remain absent from the reading index and attachment processing while Watch announces it is up to date.

**Code:** [engine.py:919](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:919), enqueue/notification; [engine.py:944](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:944), drain/save/clear sequence; [engine.py:963](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:963), fallback wait.

**Conditions and path:** A Watch callback completes raw capture and enqueues a post while the outer loop awaits a dirty checkpoint. When save returns, the loop clears the event without checking the queue again, then waits. A dirty text-only batch with context capture disabled reaches this window.

**Expected versus actual / impact:** Watch should not enter its wait with queued work. Instead processing waits for another event or the catch-up timer, up to roughly 60 seconds. Stop during this interval leaves the post out of the saved reading index until later capture; its already-committed raw observation remains intact.

**Root cause:** Notification reset follows an await that permits new work to be enqueued, with no post-await queue check.

**Verification and origin:** Traced producer and consumer ordering. Reused [RC-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/01-race-conditions.md:7): a checkpoint barrier left post 2 raw-saved but unindexed with 59.63 seconds left; a subsequent-event control immediately processed posts 2 and 3.

**Suggested fix direction:** Clear before queue inspection or recheck after checkpoint so the wait cannot discard a notification for queued work.

<a id="ts-025"></a>
### TS-025 — P2: The browser's library signature misses genuine post and media changes

**Severity justification:** Healthy state polling can leave visible rows stale indefinitely until another invalidating action occurs.

**Code:** [app.js:198](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:198), signature; [app.js:797](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:797), job refresh; [engine.py:692](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:692), counters before download; [engine.py:727](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:727), media completion; [engine.py:995](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:995), per-run reset.

**Conditions and path:** The signature uses channel, count, latest post date, and per-job added/updated counts. Two completed edit jobs can have identical tuples if the browser misses the intervening running state. Separately, a new Watch post can render while its download is pending; completion changes the file state and `media_downloaded`, but not that signature or running status.

**Expected versus actual / impact:** Visible mutations should invalidate rows. Instead old text or a “Media not saved” badge can remain despite correct server state. Manual reload/filter actions, a later signature change, or an observed running-to-stopped transition can repair it; opening detail also fetches fresh data. “Indefinitely” means repeated unchanged-signature polls alone do not repair it.

**Root cause:** Per-job statistics and collection summaries are used as a library content version even though they neither uniquely identify nor cover visible mutations.

**Verification and origin:** Checked signature inputs against engine mutations. Reused [02-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:26): real runtime jobs produced equal signatures with changed text; a controlled Watch download changed missing-media state without changing the signature; extracted JS kept stale rows through repeated polls.

**Suggested fix direction:** Invalidate with a library revision covering every visible mutation, including media completion. A run ID alone covers only the repeated-job trigger.

<a id="ts-026"></a>
### TS-026 — P2: Failed post refreshes are marked current and not retried by later polls

**Severity justification:** A transient list-fetch failure can leave old rows visible after connectivity is healthy again.

**Code:** [app.js:198](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:198), refresh and signature assignment; [app.js:494](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:494), swallowed list error.

**Conditions and path:** Rows are already loaded and a changed state signature correctly triggers `loadPosts({preserve:true})`. The posts request fails, but `loadPosts()` catches it and resolves. `loadState()` records the new signature anyway; `postsLoaded` and `online` remain true. Subsequent successful state polls skip the unchanged-signature refresh.

**Expected versus actual / impact:** An unsuccessful refresh should remain pending for retry. Instead older rows persist and the state-network retry control is not activated because the state request itself succeeded. The signature changed correctly here, unlike TS-025; these defects require separate corrections.

**Root cause:** The caller commits the refresh version without receiving confirmation that the matching posts response was accepted and rendered.

**Verification and origin:** Independently traced error swallowing and unconditional assignment. Reused [02-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:64): an initial successful list, one failed refresh, and three healthy polls made only two posts requests and retained the older text.

**Suggested fix direction:** Advance the signature only after a successful accepted refresh; failed or superseded refreshes must retain pending invalidation.

## Disposition of every original finding

For reports 03 and 05, `03-01` / `05-01` denote their original numbered finding 1, and so on. These aliases do not create additional findings. “Confirmed” rows are the canonical representatives of the 26 distinct bugs; the one merged row contributes its trigger/evidence to an existing ID.

| Original report / finding | Disposition | Final ID | Assessment or narrowing |
|---|---|---|---|
| [01 / RC-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/01-race-conditions.md:7) | Confirmed | [TS-024](#ts-024) | Lost notification delays derived processing; raw observation survives. |
| [01 / RC-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/01-race-conditions.md:50) | Confirmed | [TS-014](#ts-014) | Already-accepted request crosses shutdown admission boundary; no claim of older-data corruption. |
| [02 / 02-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:7) | Confirmed | [TS-009](#ts-009) | Current index regresses; both source observations remain. |
| [02 / 02-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:26) | Confirmed | [TS-025](#ts-025) | Both identical-job-counter and media-completion triggers retained. |
| [02 / 02-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:64) | Confirmed | [TS-026](#ts-026) | Correct invalidation is acknowledged despite failed refresh; distinct from TS-025. |
| [02 / 02-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/02-stale-state-lost-updates.md:89) | Confirmed | [TS-023](#ts-023) | Sequential stale whole-form writes suffice; blank-hash retention is unaffected. |
| [03 / 03-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:3) | Confirmed | [TS-020](#ts-020) | Duplicate matching primary acquisition; distinct variants remain legitimate. |
| [03 / 03-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:23) | Confirmed | [TS-021](#ts-021) | Crash replay duplicate allocation; no assertion about power-loss durability. |
| [03 / 03-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/03-retries-idempotency.md:45) | Confirmed | [TS-022](#ts-022) | Cross-representation peer deduplication gap, not failure of same-string normalization. |
| [04 / 04-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:5) | Confirmed | [TS-002](#ts-002) | Canonical initialization defect; P1 reduced to P2 for demonstrated new-archive lockout. |
| [04 / 04-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:36) | Confirmed | [TS-011](#ts-011) | Requires a flush failure after catalogue replacement, not just any save error. |
| [04 / 04-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:64) | Confirmed | [TS-012](#ts-012) | API-client identity mismatch; no inferred cross-account disclosure. |
| [04 / 04-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/04-atomicity-partial-failure.md:96) | Confirmed | [TS-013](#ts-013) | Installed path changes despite failure; prior app retained, running process not replaced. |
| [05 / 05-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:5) | Confirmed | [TS-017](#ts-017) | Primary attachment Download file URL; preview and separate variant URLs excluded. |
| [05 / 05-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:32) | Confirmed | [TS-018](#ts-018) | Username repair reproduced; invite analogue supported only by branch trace. |
| [05 / 05-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/05-state-invariants.md:69) | Confirmed | [TS-019](#ts-019) | Basic groups only; channel/supergroup request remains appropriate. |
| [06 / LC-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/06-lifecycle-cleanup.md:7) | Confirmed | [TS-016](#ts-016) | Unbounded wrappers/callbacks; probe task counts are not production per-wakeup job counts. |
| [06 / LC-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/06-lifecycle-cleanup.md:82) | Confirmed | [TS-015](#ts-015) | Initial-handshake timeout window, with installed dependency behavior checked. |
| [07 / 07-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:5) | Merged into another confirmed finding | [TS-002](#ts-002) | Empty-success/date-range trigger shares the initialization invariant and correction with 04-01. |
| [07 / 07-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:27) | Confirmed | [TS-003](#ts-003) | Detect/recover missing source history; does not attribute initial file loss to the app. |
| [07 / 07-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:45) | Confirmed | [TS-008](#ts-008) | Missing supported metadata-establishment path; manually provisioned metadata can avoid it. |
| [07 / 07-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:63) | Confirmed | [TS-004](#ts-004) | Requires no retained earlier ZIP proving the missing historical bytes. |
| [07 / 07-05](/Users/xxx/GitHub/telegram-scraper/bug-audit/07-persistence-migrations.md:79) | Confirmed | [TS-005](#ts-005) | Archived database payload validation, separate from live database absence. |
| [08 / 08-01](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:7) | Confirmed | [TS-001](#ts-001) | P1 retained for silently incomplete recovery snapshots; source bytes not deleted. |
| [08 / 08-02](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:32) | Confirmed | [TS-006](#ts-006) | Successful corrupt body requires remaining length to fit appended error bytes. |
| [08 / 08-03](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:58) | Confirmed | [TS-010](#ts-010) | Actual cause lost from terminal receipt; interruption events may still exist. |
| [08 / 08-04](/Users/xxx/GitHub/telegram-scraper/bug-audit/08-error-handling-recovery.md:90) | Confirmed | [TS-007](#ts-007) | Decompression exception aborts the check; distinct from semantic SQLite validation. |

**Merge boundaries:** TS-002 is the only merge. Similar-looking pairs remain separate because they have different defective boundaries and corrections: primary/variant acquisition versus crash replay (TS-020/021), obsolete locator repair versus cross-archive peer deduplication (TS-018/022), live evidence absence versus archived database validation (TS-003/005), and signature incompleteness versus acknowledging a failed refresh (TS-025/026). The input reports' different ratings for the shared initialization defect are resolved by the P2 assessment above.

## Unresolved findings — excluded from the confirmed count

**No original finding remains Unverified or Rejected.** All 27 are accounted for above; there is no extra count for a narrowed consequence or an alternate trigger.

The following are **verification limits, not additional confirmed bugs**:

- Some original grouped test selections hit their short Watch/download startup deadlines. Audit 01 reports the overflow test timing out before an in-memory wait-adjusted run passed; audit 02 similarly needed a larger startup wait; audit 06's two timed-out tests passed after isolated repetition with Telethon pre-imported. Their causes were not established here. These are not converted into extra defects or described as clean initial suite passes.
- No live Telegram account, actual user archive, installed application, or desktop UI was exercised. Protocol behavior is supported by offline fixtures and the inspected installed Telethon 1.42.0 implementation. JavaScript observations reused Node executions of actual function bodies with controlled responses/minimal DOM substitutes, not a visual browser run.
- The installer finding uses an independently checked code trace and the original report's disposable-bundle result. No new build, install, signing, or launcher action was performed.
- Process-exit and injected I/O results do not establish physical power-loss behavior. These findings also do not establish that any particular real user archive has suffered their effects, or that its Telegram capture is complete.

## Verification record and reproduction command

The preserved probe copies the 49 launch-baseline files into an isolated temporary source tree, verifies their hashes, imports that copy, and uses fresh synthetic libraries. It does not copy real settings, sessions, archives, or generated bundles. Bytecode writes are disabled. All eight original report hashes and all baseline source hashes/modes are checked again after execution.

Executed from `/Users/xxx/GitHub/telegram-scraper`:

```sh
/usr/local/opt/python@3.14/bin/python3.14 -B bug-audit/evidence/synthesis-probes.py > bug-audit/evidence/synthesis-probe-results.json
```

**Final execution: exit 0, seven asserted cases passed**, using Python 3.14.5 and Telethon 1.42.0. The durable JSON records these observations:

| Case | Conditions | Observed result | Final finding |
|---|---|---|---|
| `first_photo_empty_sync` | Fresh library, cached channel photo, zero history posts | Completed with no index; next load refused | TS-002 |
| `first_photo_cancelled` | Fresh library, stop during photo transfer | Cancelled; empty variant directories remained, no index; next load refused | TS-002 |
| `unreadable_subtree` | Real directory permission failure, synthetic historical attachment | Incomplete ZIP published; verification passed without issues | TS-001 |
| `invalid_database` | CRC-valid ZIP, modern index, invalid SQLite bytes | Backup counted as verified | TS-005 |
| `missing_database` | CRC-valid ZIP, modern index, database omitted | Backup counted as verified | TS-005 |
| `source_only_invalid_database` | CRC-valid ZIP, invalid database, no message index | Backup counted as verified | TS-005 |
| `invalid_deflate` | Reserved DEFLATE block type in first of two ZIPs | `zlib.error` escaped; later good ZIP unexamined | TS-007 |

Two initial probe-harness errors were corrected before this final run: normalizing macOS `/var` versus `/private/var` temporary paths, and removing an unsupported test call argument (the observation obtains its channel from the run). Those were harness errors, not additional product findings.

For the remaining findings, their essential triggering sequences and observed outputs are summarized in the entries and preserved in the linked original reports. Their surrounding existing-test results were reused as context, not rerun or presented as direct proof of a defect. No full baseline suite was repeated. Repository writes for this synthesis are limited to this document and its evidence files; the original reports and production working state were preserved.
