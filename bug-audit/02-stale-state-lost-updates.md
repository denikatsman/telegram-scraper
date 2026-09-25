# Audit 02: stale state, lost updates, and out-of-order async results

Audited the existing working tree on 2026-09-19, based on commit `188afe9fcfa3f9b1963edc23f5e46e143abd2632`, including the pre-existing uncommitted changes. The 49 source files in `bug-audit/launch-20260919T063437Z/source-baseline.json` matched their launch fingerprints during verification. No production code, installed app, Telegram session, or real archive data was changed. Reproductions used temporary directories, the repository's offline protocol fixtures, and Node with controlled responses and minimal DOM substitutes.

Four concrete findings follow. All are P2: observable correctness problems that should be fixed. Findings 1 and 2 concern different state boundaries: finding 1 changes the persisted reading index; finding 2 leaves the browser showing an older index despite a correct server result.

## 02-01 — Older queued Watch payloads overwrite a newer history snapshot

**Code:** [engine.py:933–946](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:933), [engine.py:520–531](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:520), [engine.py:680–682](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:680), [storage.py:213–238](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:213).

Watch registers its handlers and captures incoming messages into `pending` before running its initial history scan. After that scan, it processes every queued payload unconditionally. An update captured before a history response can therefore be applied *after* that newer history response has already updated the same post. Neither `_record()` nor `Store.upsert()` compares the incoming message's edit date, update sequence, or capture order with the saved version. `_record()` also assigns a fresh `last_observed_at` when draining the older payload, making its later processing time look like a later observation.

**Trigger and effect:** During startup catch-up, or the overflow recovery scan at lines 940–946, queue a message snapshot and then receive a more recent history snapshot for the same ID. Draining the queue rolls the reading index backward. A later update or sync may repair it, but the stale value is saved to `messages_all.json`; the problem survives reopening the archive. A later edit event is not needed for the counter-only case below: the queued message can overwrite a more recent history view count without changing the edit date.

**Verified reproduction:** Used the real `TelegramService.run("watch")`, `_history_pages()`, `Store`, and `EvidenceStore`, with `ProtocolClient` and `Message` from [tests/test_engine.py:22](/Users/xxx/GitHub/telegram-scraper/tests/test_engine.py:22) and [tests/test_engine.py:574](/Users/xxx/GitHub/telegram-scraper/tests/test_engine.py:574). A fixture `catch_up()` delivered the older message to the registered callback before history returned the newer message. No two `_process()` calls ran concurrently. After Watch reported `phase="watching"`, the job was stopped and the store was reloaded from disk.

| Case | Newer history response | Persisted current post after queue drain |
| --- | --- | --- |
| Edited text | `text="newer edit"`, `edit_date=2025-03-11T02:00:00+00:00`, `views=9` | `text="older edit"`, `edit_date=2025-03-11T01:00:00+00:00`, `views=1` |
| Same text and edit date | `text="unchanged text"`, no edit date, `views=9` | Same text and edit date, but `views=1` |

The text-edit case put the newer content into `revisions` as though it were an earlier version. Both raw observations remained in the evidence database; this finding is a regression of the current reading index, not destruction of those raw observations. Counter-only changes did not create a reading-index revision, because `_stable_raw()` excludes volatile counters.

**Correction needed:** Keep preserving all source observations, but arbitrate which observation may replace the current reading record. Queue-processing order cannot serve as source freshness. Regression coverage needs an older queued update followed by a newer history response, including equal-edit-date snapshots.

## 02-02 — The browser's library signature misses real changes and can leave stale rows indefinitely

**Code:** [app.js:198–201](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:198), [app.js:797–806](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:797), [engine.py:680–695](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:680), [engine.py:727–731](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:727), [engine.py:993–997](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:993).

`loadState()` decides whether to reload posts using only active channel, total posts, latest post date, `job.added`, and `job.updated`. The job counters restart at zero for every job. This tuple is neither a unique version of the library nor a record of every visible change.

**Verified trigger A — two completed syncs with the same counters:** Sync one edit to an existing post, then sync a second edit to that same post. If the browser does not observe the intervening running state, both results have the same signature. This can happen when a short job finishes before its explicit `refreshState()` returns, or when another window runs the job between polls. The changed `run_id` and post text do not participate in invalidation.

Three sequential jobs were run through the real `Runtime.start_job()` and `Runtime.state()` with an offline `ProtocolClient`: an initial seed followed by two edits. Both edit jobs completed successfully and produced these distinct server results:

```text
first edit:  running=false, added=0, updated=1, total=1, last_date=2025-03-11T00:00:00+00:00
second edit: running=false, added=0, updated=1, total=1, last_date=2025-03-11T00:00:00+00:00
run_id changed; post text changed; library signature did not change.
```

Executing the actual `loadState()` and `loadPosts()` function bodies in Node, with those response conditions, gave:

```json
{"case":"new_run_same_counts","serverText":"second edit","visibleText":"first edit","stateRequests":4,"postRequests":1,"online":true,"lastLibrarySignature":"main:1:2025-03-11T00:00:00+00:00:0:1","run":"run-B"}
```

The first request populated the list. Three subsequent successful state requests adopted the new run but made no new posts request.

**Verified trigger B — a video finishes downloading during Watch:** A new post increments `added` before its download. If the browser loads that row during a long download, it sees `media_missing=true`. Completing the download increments `media_downloaded`, but the post remains an addition and the branch at engine.py:729 does not increment `updated`. Watch remains running, so neither the signature nor the running-to-stopped check invalidates the row.

The real Watch job was paused in an offline client's `download_media()`, then released. These were the actual before/after values:

```text
Before: running=true, total=1, added=1, updated=0, media_downloaded=0, media_missing=true
After:  running=true, total=1, added=1, updated=0, media_downloaded=1, media_missing=false
The signature used by app.js was identical before and after.
```

The row's “Media not saved” badge can therefore remain after the file has been saved. Opening the detail dialog fetches fresh data, but routine state polling does not repair the list. Both examples remain stale until another signature-changing event, a running-to-stopped transition that the browser actually observes, a list action such as changing filters, or a page reload.

**Correction needed:** Invalidate against a library revision that changes for every user-visible post mutation, including media completion. Per-job statistics cannot substitute for that revision. A new job ID alone would address trigger A but would not address trigger B.

## 02-03 — A failed post refresh is acknowledged as current, preventing the next polls from retrying it

**Code:** [app.js:198–201](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:198), [app.js:494–503](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:494).

`loadPosts()` catches a failed request and resolves normally. When `loadState()` called it to refresh already displayed posts, `loadState()` then records the new `lastLibrarySignature` even though those posts were never rendered. `postsLoaded` stays true and `online` stays true because `/api/state` succeeded. The next successful polls see the same signature and skip `/api/posts`, leaving the older list in place after the temporary failure has cleared.

This is distinct from finding 02-02: here the signature **did change** and correctly triggered a refresh. The bug is committing that signature without a successful refresh.

**Verified reproduction:** Ran the unchanged `loadState()` and `loadPosts()` functions in Node with controlled `api()` responses:

1. Successfully load a one-post list containing `first edit`, with `job.updated=1`.
2. Return a new state with `job.updated=2` and server text `second edit`.
3. Fail only the resulting posts request once.
4. Restore successful posts responses and make three more successful state polls with the same new state.

Observed:

```json
{"case":"one_failed_post_refresh","serverText":"second edit","visibleText":"first edit","stateRequests":5,"postRequests":2,"label":"Posts couldn’t be refreshed","online":true,"lastLibrarySignature":"main:1:2025-03-11T00:00:00+00:00:0:2","run":"run-A"}
```

The two posts requests were the initial successful load and the single failed refresh. No retry occurred during the three healthy polls. The network notice was not activated, so its retry control was not exposed by this failure path.

**Correction needed:** Advance the signature only after a matching posts response has been accepted and rendered. A failed or superseded list refresh needs to leave invalidation pending so later polls can retry it.

## 02-04 — Saving an older Settings form silently overwrites another tab's newer settings

**Code:** [app.js:669–680](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:669), [app.js:694–700](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:694), [server.py:467–498](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:467), [config.py:101–125](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/config.py:101).

`fillSettings()` copies the current settings into the form when it opens. `saveSettings()` submits every editable setting, including fields the user did not change. Subsequent state polls replace `state.settings`, but do not rebase the open form. The server checks the selected channel and serializes active settings operations, but it does not check the settings version against which the form was opened. `Settings.update()` applies every submitted value over the current values.

**Trigger and effect:** Open Settings in two browser tabs for the same channel. Tab A disables media downloads and saves. Tab B, still holding the earlier form, disables recovery snapshots and saves. Both requests succeed, but tab B also re-enables media downloads. Neither simultaneous execution nor different processes is required; the writes can be strictly sequential.

**Verified reproduction:** Obtained the two form snapshots from a temporary `Runtime.state()`, built the exact full field set submitted at app.js:696, and passed both saves through `Runtime.scoped("main", Runtime.update_settings(...))`. Reopening `Settings` from disk confirmed the lost update:

```json
{"case":"stale_full_settings_form","after_A":{"download_media":false,"archive_before_sync":true},"after_B":{"download_media":true,"archive_before_sync":false},"both_requests":"accepted"}
```

The second tab intended to change `archive_before_sync`; its stale `download_media` value replaced the first tab's saved choice. The same full-form behavior applies to the other submitted settings.

**Correction needed:** Submit only fields changed relative to the form's opening snapshot, or require an expected settings revision and report a conflict before applying an outdated full form. The existing channel revision checks do not cover settings updates within the same channel.

## Verification details

All finding-specific reproductions above completed successfully. Python reproductions used `/usr/local/opt/python@3.14/bin/python3.14`, Telethon 1.42.0, the existing offline fixtures, and temporary data roots. Node used v24.21.0. The JavaScript checks executed the current function bodies extracted from `app.js`; HTTP responses and display-only DOM operations were substituted. They establish the refresh decisions and retained row data, not a live-browser visual test. No live Telegram traffic or desktop interaction was used.

Five relevant existing tests were also selected with `PYTHONDONTWRITEBYTECODE=1` and `pytest -q -p no:cacheprovider`:

- `tests/test_engine.py::test_watch_captures_startup_new_posts_and_edits_then_removes_handlers`
- `tests/test_engine.py::test_watch_preserves_each_intermediate_raw_edit_before_coalescing`
- `tests/test_engine.py::test_raw_counter_changes_are_preserved_even_without_text_edits`
- `tests/test_channels.py::test_stale_browser_cannot_read_or_modify_another_channel`
- `tests/test_app.py::test_private_settings_not_exposed_and_persist`

The latter four passed. The first test reached its existing two-second startup timeout in the grouped run and again in an isolated retry. Executing that same test with only its startup wait increased from 2 to 15 seconds **in memory**, without editing the test file or production code, passed all of its assertions. The original five-test selection is therefore not reported as a clean pass. That timing result is recorded as a validation limitation, not as an additional classified bug.

The findings do not depend on that timeout. The Watch rollback reproductions reached the watching phase, stopped cleanly, and verified the persisted records; the UI and settings reproductions checked the specific stale values described above.
