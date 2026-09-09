# Channel Archive 1.3.0 — validation and handoff

## 1.3 Multiple channels and pasted Telegram links

The app now has Add channel and a channel selector. Each channel keeps its own posts, media, source database and backups inside the same project. The existing archive stays in place; additional archives use `channels/<local-id>/`. A private catalogue remembers the selection. All channels share one Telegram account session, including an unfinished login challenge. Capture remains one channel at a time, and switching is disabled while an operation runs.

Channel usernames, public channel/post/preview links, private post and invite links, topic paths and corresponding `tg://` links select the containing channel. Sync and Date range determine capture scope. Equivalent username links reuse their archive. Resolving a private message ID can consult the signed-in account's existing dialogs; it never joins a channel. Other Telegram actions and links that resolve to people or bots are rejected.

The project and active archive have separate writer locks. Switching validates the destination before publishing the selection, and cancellation drains disk work before releasing its lock. Missing folders and damaged indexes are never replaced with empty archives. Requests and download URLs carry their channel selection so an older tab cannot accidentally read or modify a different channel with the same message ID. Settings retain a channel's identity while sharing account credentials and saving preferences.

Validation: **230 tests and 30 subtests passed**. Offline protocol and browser checks cover shared login, isolated overlapping message IDs, selection persistence, duplicate links, invalid links, failed/cancelled switches, stale requests, archive locks, guided phone/code/two-step setup and 320–1280-pixel layouts. Real Telegram sign-in and new live captures remain unverified until the account is connected; fixture login is not a live account test.

## 1.2.2 One project folder and renamed repository

The maintained checkout now lives at `/Users/xxx/GitHub/telegram-scraper`, backed by `denikatsman/telegram-scraper` on GitHub. The whole library moved with the checkout: saved posts, original media, ZIP backups, settings and the Telegram session remain physically inside the project. These private files stay out of Git and distribution packages. The seven original ICT Viper files are preserved unchanged in the ignored `reference/ict-viper/` folder for a later comparison; none were imported into the app.

The generated Mac app now lives inside the project. Its Applications entry is a Finder shortcut to that app. The build tool can install or refresh the shortcut while preserving an earlier installed app inside `work/app-backups/`. `Launch.command` opens the project's native app when available; its browser fallback explicitly uses this project's library and an available port.

Validation: **176 tests and 30 subtests passed**. Read-only verification passed for **1,406 posts, 379 media files and 25 ZIP backups**, with zero integrity issues. The move retained the original file identities and sizes; saved settings, session, post-index and reference-source hashes were preserved. The existing notice that **27 current posts are newer than the verified backups** remains. This move did not sign into Telegram or scrape new content.

Earlier local removals of the obsolete Windows launcher, issue templates and `.gitattributes` are included in the consolidated repository state. The sections below retain the historical scope of previous releases.

## 1.2.1 Launching from a file and moving the library

Opening the HTML file directly previously lost its stylesheet and script because the asset URLs pointed to the filesystem root. Assets now use relative paths. A direct-file preview shows a styled launch screen with an Open Channel Archive action and avoids unavailable controls or API requests. The installed Mac app registers that launch link; it only opens the window and cannot supply credentials, change the selected library or start a scrape. The source launcher also chooses an available port.

The Mac bundle now stores a macOS bookmark for the chosen library. It follows ordinary folder moves and renames on reopening, and fails clearly when the folder is unavailable instead of creating an empty replacement. The Python environment still needs to remain at its configured location. This pass found the existing library had moved to the Desktop, reconnected the installed app there, and verified the original post index, settings and Telegram session were unchanged.

Validation: **176 tests and 30 subtests passed**. Scripted Chromium opened the actual `file://` entry with styles, a launch action, no API requests or page exceptions, and no overflow at 320, 768 or 1280 pixels. An isolated native bundle reopened a moved library and refused a missing library without recreating either old path. Launch Services resolved the launch link to the installed app. The updated app served the existing **1,406-post** library from its new location. Live Telegram login and capture still require account sign-in.

## 1.2 Mac app and guided setup

Implemented September 9, 2026. A native macOS window now starts and displays the local interface without a Terminal window. The app uses an available port, so another local tool cannot occupy its address. It reuses an authenticated server only for the same library; quitting stops an owned server safely and leaves a reused server running. Private discovery files are excluded from Git and packages and removed on shutdown.

The setup card shows the three steps: API details, channel and Telegram sign-in. Settings explain where to obtain an API ID/hash and why a BotFather bot token cannot be used. Sign-in controls stay disabled until credentials are saved, and guide the user through phone number, code and optional two-step password. Saving preferences are collapsed to keep login accessible; the dialog's Close button stays visible while scrolling. The Mac app includes Settings, Show Archive Folder, standard keyboard editing and native export/download dialogs. Completed download replacements preserve the previous destination file.

Validation: **176 automated tests and 30 subtests passed**, including real backend start, offline first run, private server discovery, second-window reuse, shutdown/relaunch and damaged-settings preservation. Scripted Chromium passed guided setup, invalid-code feedback, resumed two-step login, saved settings, fixture sync, Watch/Stop and widths 320–1280 without page errors. Native WebKit opened the real bundled interface on an isolated empty library and shut down without creating a Telegram session. A second native check clicked Export posts, completed a native download, retained an exact 64-bit ID and preserved the previous destination file. Native reuse kept the original server running when the window closed. A clean virtual environment installed the 1.2 wheel and served its packaged UI offline; both package formats and the Mac bundle excluded saved credentials and personal data.

The bundle is built for the development Mac, uses its selected Python installation and library folder, and contains no personal settings, Telegram session, posts or media. It has a local ad-hoc signature, not Developer ID signing or notarization. It is not a self-contained installer for another Mac. No real login code was requested during this GUI pass; live Telegram sign-in and capture remain unverified. Earlier API credentials are saved locally, but the last live check required account authorization.

The earlier source commit was pushed to the owner's fork. Git now uses that fork as `origin` and the original project as `upstream`; the branch comparison base is `origin/main`. The large comparison against the original project was distinct from uncommitted work. Pre-existing deletions of `.gitattributes`, the issue templates and the Windows launcher remain outside this GUI change.

The remaining sections retain the earlier implementation and validation history. The browser interface is also available on macOS and Linux as Python source and an installable package.

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

At the end, the running 1.1 app attempted the configured ICT **Telegram** connection. API credentials were present, but Telegram returned `authorized: false`; the actual capture endpoint refused the request with HTTP400 and a sign-in message. **Zero new posts were scraped**; the library remains at 1,406. No login code was requested. That historical run used `http://127.0.0.1:8765`; the Mac app now chooses its own available port. The result is saved in `work/final-live-capture-attempt.json`. X/Twitter scraping was not added; “tweets” was interpreted in the context of this Telegram-channel task.

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

No Developer ID signed or notarized macOS bundle, Windows support, clean-Mac acceptance, Safari/Firefox playback pass, or unattended long-running live Watch soak is claimed. Browser media support varies; the original-file download remains available. The interface is intended for one local user, not exposure through a public network or reverse proxy. Large libraries require space for full backup copies; backups are never silently pruned.

That first implementation pass did not create a Git commit, remote publication, or release tag; the source was subsequently committed and pushed to the owner's fork. Publishing this source does not establish live Telegram acceptance. Pre-existing local removals of the old issue templates, `.gitattributes`, and Windows launcher are outside the app changes. The original BSD license was restored for redistribution attribution.
