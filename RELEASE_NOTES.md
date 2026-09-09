# Channel Archive 1.1.0 — validation and handoff

Implemented September 7–8, 2026. This is a local browser application for macOS and Linux, distributed as Python source and an installable package. It is not a signed, standalone macOS application or a hosted service.

## 1.1 preservation pass

Added an append-only SQLite source store with separately durable observations, occurrence records, events and scrape receipts. Full raw history pages, message objects, related entities and serialized TL objects commit before derived processing. Telethon/app/API-layer provenance explains what the bytes represent. A process crash preserves committed source even when the reading index has not reached its next checkpoint. Exact repeat payloads reuse source storage while preserving every observation occurrence.

History uses anchored, paginated API requests with explicit scope and count reconciliation. Capture fails clearly on unsupported response shapes, stalled pagination or required serialization/storage failure. Transient requests retry the same position; floods wait cancellably. Watch preserves intermediate raw edits before queue coalescing and drains active capture callbacks before closing a run. Reruns scan history again; automatic cursor resume is not claimed.

Optional deep capture defaults on: linked discussion pages and attachments, poll results/accessible voters, permitted reaction details, full channel metadata, returned entities, alternate media, thumbnails, covers and channel-photo sizes. Downloads use immutable hash manifests; corrupt variants remain preserved while a verified replacement is written separately. Unsupported, restricted, anonymous, paid-preview and unfetched story-reference cases are explicit limitations. No claims extend to data Telegram did not return.

Archive health distinguishes simplified legacy posts from source-captured posts, retains visible interrupted/limited run receipts across restarts, and offers complete streaming source exports. Post inspection has bounded observation/occurrence previews and exact 64-bit IDs. Polls are displayed read-only. Slow filesystem health reads run outside the Telegram event loop. A failed source integrity check stays visible until a full validation succeeds.

Backups now include raw-only evidence from interrupted runs. Startup recovers hot SQLite journals only while holding the library writer lock. Read-only verification additionally checks source payload hashes/provenance and all variant receipts/files; capture limitations are reported separately from missing or damaged bytes.

Current validation: **171 automated tests and 30 subtests passed** with Python 3.14.5/Telethon 1.42.0. These include real Telethon object serialization, a subprocess crash with journal recovery, interrupted raw capture, repeated cursors, count discrepancies, corrupt stores, full/filtered streaming exports, bounded previews, variant repair, source API protection, exact IDs and a responsive Stop during slow disk access. Python compilation, JavaScript syntax and diff whitespace checks pass.

Scripted Chromium exercised the real HTTP/storage source UI with isolated fixture data: legacy counts, completed-with-limits and interrupted receipts, full export download, exact 64-bit source/album display, poll details, setting persistence and widths 320–1280. No page exceptions occurred. Expected stale-cookie HTTP403s occurred while deliberately restarting the fixture server; reloading acquired the new local session. Source screenshots and logs are under `output/playwright/` and `work/`.

The historical validation below documents the first product pass. Live Telegram acceptance remains separately gated by the saved session's authorization; offline fixtures do not prove a real channel capture.

Final 1.1 checks: the live library again passed read-only verification of **1,406 posts, 379 media files and 25 ZIP backups**, with zero issues in 135.93 seconds. The index SHA256 and media paths/sizes remain unchanged. It still contains legacy records and no new source observations. Both 1.1 distributions include all 13 current package source/static files and exclude the private archive and saved API hash. A clean virtual environment installed the 1.1 wheel, launched the app, served its packaged UI offline and shut down successfully.

At the end, the running 1.1 app attempted the configured ICT **Telegram** connection. API credentials were present, but Telegram returned `authorized: false`; the actual capture endpoint refused the request with HTTP400 and a sign-in message. **Zero new posts were scraped**; the library remains at 1,406. No login code was requested. The updated app remains available at `http://127.0.0.1:8765`. The result is saved in `work/final-live-capture-attempt.json`. X/Twitter scraping was not added; “tweets” was interpreted in the context of this Telegram-channel task.

## Product changes

The six original scripts now use one shared implementation. The browser interface provides a searchable offline library, video/photo/file views, date filters, pagination, post details, video playback and downloads, text copying, and earlier saved text versions. Settings, Telegram sign-in, sync, date-range capture, Watch, and archive verification are available in the same interface. Loading, empty, missing-media, partial-completion, cancelled, disconnected, and error states give actionable feedback. The layout supports narrow windows and keyboard navigation.

## Reliability fixes

- Unreadable, missing-with-orphaned-media, duplicate-ID, or unsupported data is rejected before writes. JSON publication is atomic, preserves unknown fields, and checks for external changes before replacing the current index.
- Source credentials were removed from all distributable scripts. Private settings and session files are excluded from Git and packages. Blank API hash fields retain the saved value. Preference-only changes preserve the connection.
- Full history and date ranges no longer have a 5,000-post cap. End dates include the whole UTC day. Edited text and replaced or removed attachments preserve prior versions and files. Saved posts are retained when Telegram deletes them.
- Missing, empty, incomplete, or checksum-mismatched media can be retried. Downloads use private temporary files, validate size, record SHA256 hashes, and publish without overwriting older files. Completed text and media are checkpointed during work and cancellation.
- One process owns each library. Channel identity prevents mixing overlapping message IDs. Private invite resolution checks existing access and never joins channels.
- Watch registers handlers before history catch-up, processes new posts and edits, bounds/coalesces pending messages, and periodically requests missed updates. Flood waits are visible and cancellable. Stopping queued work is respected; shutdown drains disk workers before releasing the library lock.
- Backups contain the full existing library. They are streamed, cancellable, uniquely named, checked for concurrent changes, and published only on completion. Verification reads ZIP members for CRC integrity, compares media bytes and cumulative message IDs, and distinguishes text changes from missing or damaged data. It checks channel identities when available. No automatic backup deletion exists.
- The HTTP server only binds to loopback, validates Host/Origin, requires a private per-port session cookie and anti-forgery token, rejects escaping/symlink media paths, and supports video byte ranges. Export obeys the same safe-read boundary. Archived text is never treated as executable HTML.

## 1.0 validation retained for provenance

- **81 automated tests and 17 subtests passed** on macOS with Python 3.14.5 and Telethon 1.42.0. Tests use temporary data and fake Telegram clients. They cover storage failures and preservation, large/date-range ingestion, edits and media changes, cancellation, login errors and two-step flow, Watch/catch-up, media checksum repair, API behavior, local HTTP boundaries, settings persistence and shutdown.
- JavaScript syntax and Python compilation pass. Frontend static checks found unique HTML IDs and resolved direct JavaScript references.
- Scripted Chromium checks exercised the actual HTTP server and local storage with a simulated Telegram adapter: first-run settings and restart persistence, hidden saved API hash, invalid-code feedback, phone/code/password sign-in, resuming two-step sign-in after reload, preference saves preserving connection, sync/list refresh, search, literal HTML safety, empty-filter recovery, Watch/Stop, verification warnings, single-day scraping, inclusive UTC date filters, server-outage recovery, and widths from 320 through 1280 pixels. Expected errors were deliberately injected for the invalid-code and server-outage checks.
- A real saved video loaded, played, and sought to 10 seconds without a browser media error. The test video was 23.44 seconds, 596 × 1280 pixels. This proves this file works in the tested Chromium browser, not that all Telegram codecs work in every browser.
- Read-only inspection of the existing library verified **1,406 posts, 379 media files, and 25 ZIP backups**. No integrity issues were reported. **27 current posts are newer than the verified backups**. Full verification took approximately 130 seconds. This checks consistency of the examined files, not completeness of remote Telegram history.
- Both wheel and source distributions were built and inspected: no private archive, settings, session, or actual credential values were included. A clean virtual environment installed the wheel without dependencies, launched its command, served the packaged UI and an empty offline library, and shut down cleanly. This is an isolated installation on the development Mac, not a clean-machine acceptance test.
- The original message-index SHA256 and original Telegram-session SHA256 match the baseline. The original media paths and file sizes also match. No real sync or archive rewrite was performed.

Local evidence is in the Git-ignored `work/` folder and `output/playwright/`. These may contain private archive excerpts or screenshots and are deliberately excluded from distributions.

## Remaining release limits

A bounded live status check using an isolated copy of the saved Telegram session returned **not authorized**. No live channel-history or fresh media-download test could run. Reconnect in Settings, then run a small date range with media downloads enabled before calling a live Telegram release accepted. Login codes and passwords were tested only through fake responses; no real code was requested.

No signed/notarized macOS bundle, Windows support, clean-Mac acceptance, Safari/Firefox playback pass, or unattended long-running live Watch soak is claimed. Browser media support varies; the original-file download remains available. The interface is intended for one local user, not exposure through a public network or reverse proxy. Large libraries require space for full backup copies; backups are never silently pruned.

The implementation pass did not create a Git commit, remote publication, or release tag. Publishing this source does not establish live Telegram acceptance. Pre-existing local removals of the old issue templates, `.gitattributes`, and Windows launcher are outside the app changes. The original BSD license was restored for redistribution attribution.
