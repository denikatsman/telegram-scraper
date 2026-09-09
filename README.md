# Telegram Scraper

Project, package and command: `telegram-scraper`. The Python import uses `telegram_scraper` because Python identifiers cannot contain hyphens.

A local app for saving and revisiting posts, videos, images, and files from Telegram channels and groups. Keep separate channel archives under one project folder and one Telegram login. Browse and search your saved library in a Mac app or browser, play downloaded videos, sync history, capture a date range, or keep watching for new posts and edits.

Your archive stays on your computer. The interface runs at a local address and does not use a hosted service, analytics, external fonts, or a CDN. Telegram is contacted only when you connect or start a Telegram operation.

The repository is [denikatsman/telegram-scraper](https://github.com/denikatsman/telegram-scraper). This project's local home is `/Users/xxx/GitHub/telegram-scraper`: its code, post index, downloaded media, settings, session, backups and preserved comparison source all live inside that folder. The GitHub repository contains the maintained code and documentation. Scraped posts, media, credentials, generated apps and the original ICT Viper reference stay local.

## Start the app

On macOS, open **telegram-scraper.app** inside the project, or its shortcut in Applications. The actual app stays inside the project alongside its archive. The setup card walks you through API details, choosing your channel and signing into Telegram. You can browse an existing library before connecting. No Terminal window is needed for everyday use.

The Mac app starts its local server on an available port and closes it safely when you quit. If the same library is already open in a browser server, the app reuses that server and leaves it running when you close the window. **File → Show Archive Folder** opens your data folder; **⌘,** opens Settings.

Opening `telegram_scraper/static/index.html` directly shows a launch screen with **Open Telegram Scraper**. The HTML file needs the running app to access your library; it cannot run the scraper by itself. The launch button opens the installed Mac app and never starts a scrape automatically.

### Build the Mac app once

Requires macOS 12 or newer, Python 3.10 or newer and Xcode command-line tools. From this checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python tools/build_macos_app.py --install-shortcut
open "telegram-scraper.app"
```

The bundle contains the interface and backend code, and points to the Python installation and library folder selected when it was built. The Mac app remembers the library with a macOS folder bookmark so it can follow ordinary Finder moves and renames. If the folder is unavailable, it stops instead of creating a replacement empty archive. Keep the selected Python environment in place; rebuild after changing Python or updating the source. To select another library, add `--data-dir /path/to/library` to the build command. No settings, Telegram session, posts or media are copied into the app. This is a local build, not a self-contained or notarized installer for other Macs.

### Browser or command line

On macOS or Linux, install the Python requirements above, then run `python main.py`. On macOS, `Launch.command` opens this project's Mac app if it has been built; otherwise it starts the browser interface on an unused port using this project's archive. For the browser fallback, keep its Terminal window open and press **Ctrl+C** there to stop safely. Running `python main.py` directly normally uses `http://127.0.0.1:8765`.

If that port is busy:

```sh
python main.py serve --port 0
```

For command-line installation, `python -m pip install .` also installs the `telegram-scraper` command. Installed copies use `~/.local/share/telegram-scraper`; source checkouts use their project folder. Use `--data-dir /path/to/library` before the command, or set `TELEGRAM_SCRAPER_HOME`, to choose another location. Upgrades still recognize the previous storage override and fallback folder, and the previous login filename, so existing libraries and saved accounts remain available.

## Connect a channel

1. Click **Add API details** (or open **Settings**). Enter your own API ID and API hash from [Telegram's API development tools](https://my.telegram.org/apps). This app signs into your Telegram account; a BotFather bot token cannot be used.
2. Enter the channel's `@username`, a channel or message link, or a numeric ID. For private channels, your Telegram account must already have access. The app does not join channels for you.
3. Save settings, then click **Connect Telegram**. The app checks your saved session and shows phone-number sign-in if needed. Telegram may send the code inside Telegram rather than by SMS. If enabled on your account, your two-step verification password is requested next.
4. Click **Sync now**. You can browse saved posts while it runs. **Stop** keeps completed posts and downloads; another sync checks history again and retries missing attachments.

An existing local library is available before signing in. API credentials are stored privately in `.telegram-scraper.json`; passwords and login codes are not saved there. The reusable Telegram login is stored in `telegram-scraper.session`. Keep both files private. They, your media, backups, and local work files are excluded from Git and distribution packages.

Each archive belongs to one channel, so overlapping Telegram message IDs cannot mix libraries. An old archive without channel metadata needs its original configured channel identity before sync can safely attach it. This checkout's existing configuration has been preserved locally.

## Add and switch channels

Click **+ Add channel**, paste its channel or message link, and select **Add channel**. Use the **Channel archive** selector to switch between saved channels. Your selection survives an app restart; API details and the Telegram session are shared. Finish or stop a running capture before switching channels. The app runs one capture at a time.

The first archive stays in the original `telegram_data/` and `archives/` folders. Additional archives live under `channels/<local-id>/`, each with its own posts, media, source database and backups. All of these folders remain inside the project and are excluded from Git. Changing channels never moves or merges saved posts. A missing or damaged channel folder is reported instead of being replaced with an empty archive.

Supported inputs include `@channelname`, `t.me/channelname`, public post links such as `t.me/channelname/123`, private post links such as `t.me/c/1234567890/123`, public preview links under `t.me/s/`, invite links under `t.me/+...` or `t.me/joinchat/...`, and corresponding `tg://resolve`, `tg://privatepost` and `tg://join` links. Topic/message paths and ordinary message options follow [Telegram's documented link formats](https://core.telegram.org/api/links#message-links). A **message link selects the containing channel**, not a single-post capture. Choose **Sync** for accessible channel history or **Fetch a date range** for a period.

Equivalent username links reuse the same archive. A private invite and a public username can refer to the same channel without looking alike; these different identifiers are not automatically merged. Private message IDs are resolved through channels the signed-in account can already access. Links for people, bots, sticker packs, proxies or other Telegram actions do not start a scrape.

For command-line use, run commands against the project root; they use the channel most recently selected in the app. A different `--data-dir` starts an independent workspace with its own account settings.

## Everyday use

- **Library:** search post text or IDs, filter by media type or UTC date, and open a post to read it or play its downloaded video. Dates on posts display in your local timezone. The date filter and scraper range use whole UTC calendar days, including the last selected day.
- **Sync:** checks all accessible history, saves new posts, records edits, and retries missing or incomplete files. There is no 5,000-message cap. It preserves previously saved posts even if Telegram later deletes them.
- **Date range:** saves a selected period. An empty end date means the same day as the start date.
- **Watch:** catches up first, then saves incoming posts and edits until stopped. Keep the app running and the computer awake with an internet connection.
- **Verify archive:** reads saved media and backup contents, checks ZIP integrity, compares previously archived posts and media, and reports problems. Text changes are reported separately because they can be legitimate Telegram edits. A passing check shows consistency of the files examined; it does not prove Telegram history is complete or identify the cause of a change.
- **Export posts:** downloads the current saved post index, including stored metadata. Media files remain in the library's media folder.
- **Archive health:** separates legacy posts from captured source records, shows available files and persistent scrape receipts, and links to complete source exports. Each post has a source inspector and downloads for additional captured media.

Videos play when their format is supported by your browser. Use the file's download link to open other formats in a local player. Missing media stays visible and can be retried by syncing with media downloads enabled.

## What a scrape preserves

New captures save the full fields of each returned Telegram object and Telethon's serialized TL representation **before** processing the post or downloading its media. Binary fields in JSON use an explicit base64 representation. Serialized TL objects are not the original encrypted network packets. The source database retains distinct observations, including counter/reaction changes that would not create a visible text revision, and records each observation's run and capture time.

History is requested in pages without a message cap. Each scan records its scope, initial top message ID, Telegram's initial count, page responses, returned users/chats, durable progress and final outcome. Full-history counts are reconciled; date-range scans do not mistake the channel-wide count for the range's expected size. Repeated cursors, unsupported response types, failed requests and count discrepancies prevent an unqualified completion claim. Arrivals after the initial anchor belong to the next scan or Watch.

With **Capture linked discussions and source details** enabled (the default), the app also requests full channel metadata, returned related entities, paginated linked comments and replies, poll results and accessible voter pages, reaction snapshots and accessible reaction-identity pages. It inventories and downloads supported photo sizes, video alternatives, document thumbnails, covers, comment attachments and channel-avatar sizes. Each additional download has an immutable receipt with its size, SHA256 hash and source relationship. This deeper capture can take substantially longer and use more disk space than downloading the main video alone.

This is an archive of **what Telegram returns to the signed-in account**, not a guarantee of every message that has ever existed. Deleted or inaccessible messages cannot be reconstructed. History can change during a scan; Telegram does not provide an atomic snapshot. Anonymous voters, restricted reaction identities, paid previews, unsupported binary forms and story references that are not fetched are explicitly recorded as limitations. The app never casts votes, buys content or joins channels. These boundaries follow the returned API objects; see Telegram's [history method](https://core.telegram.org/method/messages.getHistory), [discussion API](https://core.telegram.org/api/discussion) and [file representations](https://core.telegram.org/api/files).

Your existing simplified records are **legacy records** until a new sync enriches them. The app cannot recover fields that the old scripts did not save and Telegram no longer exposes. Read-only browsing does not manufacture source metadata for old posts.

**Download source observations** exports all saved raw objects, occurrence metadata, run events and coverage receipts. A post's **source preview** limits both observations and their occurrence histories; use **Export all source data** for every saved entry. JSON exports retain 64-bit Telegram identifiers exactly. Use an integer-preserving JSON reader for analysis. Exports contain private channel/account metadata, but not your API hash, phone login code or two-step password.

## Storage and recovery

The original ICT Viper source is preserved unchanged under `reference/ict-viper/`. The local comparison found no additional saved content or features worth importing. It is excluded from Git and packaging and is never imported by the app.

```text
telegram-scraper.app/           Generated Mac app; Applications contains a shortcut
reference/ict-viper/           Preserved comparison source
.telegram-scraper.json          Private settings
.telegram-scraper-ui.json       Private running-app connection; removed on shutdown
.telegram-scraper-channels.json Private channel list and selected archive
telegram-scraper.session        Private Telegram login session
channels/<local-id>/            Additional archives, each with telegram_data/ and archives/
telegram_data/
  messages_all.json             Saved posts, metadata and prior edits
  channel.json                  Channel identity, established on sync
  evidence.sqlite3              Immutable source observations and scrape receipts
  media/                        Downloaded attachments
    variants/                   Additional media and immutable index receipts
archives/                       Full snapshots taken before sync
```

Settings are saved when you press Save. Posts are checkpointed during syncing and on cancellation. A failed or unreadable message index is never treated as an empty library and overwritten. Completed files are published from separate temporary downloads, without overwriting old attachments. One running app owns a library at a time.

Source observations commit independently before derived post processing. Following a sudden process crash, the reading index may lag behind the source database; committed raw pages remain in the complete source export. A later sync scans history again and reuses verified downloads; it does not silently assume the interrupted scan was complete. Unfinished receipts remain visibly interrupted. Startup recovers interrupted SQLite transactions under the library lock. Unsupported or damaged source databases stop new capture rather than being replaced.

**Back up before sync** is on by default. Each snapshot includes the existing post index, source database, media and variant receipts, including raw-only evidence from a prior interrupted run. Large libraries need enough free space for another full copy. Snapshots may take time. They are completed before history/content capture starts; the new attempt's initial receipt is already committed and quiescent during copying. Failed or interrupted snapshots are not presented as usable backups. The app does not automatically delete old backups. You can switch off future snapshots in Settings, but keep another backup of important archives.

To recover a damaged library: stop the app, preserve the current folder, and extract a known-good backup into a **separate** folder. Check its `messages_all.json`, `evidence.sqlite3` and media before replacing anything. If a SQLite journal is present after a crash, keep it with its database. Do not delete old backups solely because a consistency check passed.

For a fresh machine, copy the library folder and install the app. Legacy relative media paths are supported; paths pointing outside the chosen library are rejected. Keep a separate copy of original exports if exact original JSON formatting matters; subsequent saves retain fields but normalize order and formatting.

## Command line

```sh
python main.py login
python main.py sync
python main.py range --start 2025-01-01 --end 2025-01-31
python main.py watch
python main.py verify
python main.py check
python main.py --data-dir /path/to/another-library serve
```

The original `Login.py`, `Sync.py`, `Choose-Date-Range.py`, `Real-time.py`, `Archive-check.py`, and `Simple-check.py` names remain as compatibility entry points. `Simple-check.py` now shows exactly the first five **saved** posts, without fetching thousands of messages from Telegram. Running a check never creates a backup or changes the library.

If Telegram limits requests, the app displays the wait and continues when allowed. If a session expires or channel access changes, reconnect in Settings. If an attachment is unavailable, the post remains saved and the result identifies the failed download. The app can only archive history and media Telegram makes accessible to your account.

## Development

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

Tests use temporary libraries and fake Telegram clients. They do not sign in, message anyone, or mutate your actual archive. See [RELEASE_NOTES.md](RELEASE_NOTES.md) for the current validation record and limits.

This project uses [Telethon](https://docs.telethon.dev/en/stable/) and retains the original BSD 2-Clause license. It is an independent tool, not affiliated with Telegram or the Inner Circle Trader.
