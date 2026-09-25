# State transitions and invariants — verified findings

Audited the current working tree on 2026-09-19, based on commit `188afe9fcfa3f9b1963edc23f5e46e143abd2632`, including its pre-existing uncommitted changes. These three findings were reproduced with temporary libraries and offline Telegram fixtures; no real account or archive was changed. Validation used Python 3.14 and the installed Telethon 1.42.0. No production code was modified.

## 1. [P2] Every primary-media download link corrupts its selected-library key

**Location:** [app.js:888](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:888). Related code: [archiveURL():56](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/static/app.js:56), [Runtime.check_channel():145](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:145), and [Handler.channel_key():664](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:664).

**Broken invariant:** A link produced for the selected archive must retain that archive's exact identifier. The primary attachment's preview URL satisfies this invariant, but its download URL does not.

`renderPostDetail()` first obtains `source` from `archiveURL()`, which always adds `?library=<active_channel>`. It then constructs the **Download file** link as `` `${source}?download=1` ``. The second question mark becomes part of the `library` parameter's value. `channel_key()` extracts that corrupted value, and `check_channel()` rejects it because it no longer matches the selected archive.

This affects the original archive as well as additional archives. It does not require switching channels, a stale page, concurrent activity, or a missing file. The download fails even while the same attachment's preview URL works. It also breaks the suggested download fallback for videos the browser cannot play.

**Verified reproduction:** Executed the actual `archiveURL()` and `renderPostDetail()` functions from `app.js` in Node with a minimal DOM fixture, then requested the resulting links from a real `LocalServer` serving a temporary saved attachment. A corrected query separator was used as the control.

| Selected archive | URL produced by the actual renderer | Result |
| --- | --- | --- |
| `main` | `/api/media/1?library=main?download=1` | HTTP 400 |
| `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` | `/api/media/1?library=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?download=1` | HTTP 400 |

Both failures returned:

```json
{"ok": false, "error": "The selected channel changed. Refresh the app before continuing."}
```

Changing only the second `?` to `&` returned HTTP 200 and the exact attachment bytes in both cases. Refreshing the app cannot correct the generated link.

**Correction direction:** Set the download option through `URL.searchParams`, or add it before passing the URL through `archiveURL()`. Keep the library-selection check intact. A regression check should exercise the renderer's resulting download URL against the server for both `main` and a child archive.

## 2. [P2] A saved archive cannot recover when its channel username changes

**Locations:** [_entity():464](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:464) and [_update_settings():479](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:479). Related identity guard: [Store.bind_channel():279](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:279).

**Broken invariant:** An archive bound to a Telegram identity should remain bound to that identity when its human-readable locator changes. The current implementation instead makes the original locator mandatory and effectively immutable.

Every capture run resolves the configured username again through `client.get_entity(target)`. `_entity()` does not use the ID already saved in `telegram_data/channel.json` as a fallback. Its dialog-based fallback applies only when the configured value is already a marked numeric channel ID. Consequently, a channel that remains accessible to the same account becomes unsyncable through its existing archive after its old username stops resolving.

The user cannot repair the locator through Settings: once records or `channel.json` exist, `_update_settings()` rejects any normalized channel string different from the original, even when the replacement identifies the same saved channel. Additional archives also discard the incoming channel field after this guard. **Add channel** creates a separate archive for a different normalized locator instead of repairing the original one. This leaves the original archive unable to continue syncing through the supported UI/API.

**Verified reproduction:**

1. Created a temporary archive configured as `@oldname`, bound it to channel ID `123`, and saved one post.
2. Supplied an offline client whose `@oldname` lookup raises `ValueError`, while `@newname` and marked ID `-1000000000123` resolve to the same channel ID `123`.
3. Called the real `TelegramService._entity()`. It attempted only `@oldname` and raised `UserError`; the saved ID was not attempted.
4. Called the real `Runtime.update_settings()` with each valid replacement. Both were rejected before any identity resolution could occur.

Observed values:

```text
Saved channel ID: 123
Resolution targets before failure: ['@oldname']
@newname resolves to: 123
Update to @newname: rejected
Update to -1000000000123: rejected
```

Both update attempts returned:

```text
This archive belongs to its saved channel. Use Add channel to create a separate archive for another channel.
```

The installed Telethon implementation confirms that `get_entity()` resolves string usernames again on each call; the issue is not avoided by its saved entity cache. The same missing fallback exists for archives configured with invite links, because `_entity()` checks the original invite on every run, although the executed reproduction above used a username change.

**Correction direction:** Preserve the existing channel-identity boundary while allowing the source locator to change. Resolve a bound archive using its saved identity, or offer a locator-repair operation that verifies the replacement resolves to the already-bound peer before saving it. Do not solve this by allowing unrestricted rebinding of existing posts.

## 3. [P2] Accepted ordinary groups are passed to a channel-only metadata request

**Location:** [_channel_context():337](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:337). Related acceptance and completion paths: [_entity():505](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:505), [run():1041](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1041), and [run():1076](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1076).

**Broken invariant:** Every accepted source peer type must be compatible with the request used to capture that peer's metadata. Telegram basic groups (`telethon.tl.types.Chat`) and channels/supergroups (`Channel`) require different full-metadata requests.

`_entity()` accepts an ordinary group because it has a title, and the product explicitly supports groups. However, `_channel_context()` unconditionally constructs `channels.GetFullChannelRequest(entity)`. For a real `Chat`, Telethon resolves the entity to `InputPeerChat` and then fails while converting it to `InputChannel`. This happens locally before the full-metadata RPC can be sent.

The engine records a `full_channel` issue and continues without `_full_channel`. Thus an otherwise successful basic-group scan always finishes with `status="warning"` and `capture_complete=false`; it never captures the available full group metadata or reaches the full-group-photo capture path. Reconnecting or retrying cannot change the incompatible request type. This finding concerns basic groups; supergroups use `Channel` and do not trigger this conversion failure.

**Verified reproduction:** Ran the real `TelegramService.run("sync")` with a real Telethon `Chat(id=456, ...)`, configuration `channel="-456"`, and an offline client. The client executed the installed request classes' real `resolve()` methods before returning an empty, valid `messages.Messages` history response. No simulated network error was injected.

The invalid request raised:

```text
TypeError: Cannot cast InputPeerChat to any kind of InputChannel.
```

The complete run and saved evidence receipt showed:

```text
Accepted source type: Chat
Attempted requests: ['GetFullChannelRequest', 'GetHistoryRequest']
history_scan_complete: true
capture_complete: false
Final status: warning
Saved evidence status: warning
Only issue: stage=full_channel, error_type=TypeError
```

As a control, `messages.GetFullChatRequest(chat_id=456)` resolved successfully with the same client and peer. A separate direct capture check confirmed that only the basic `channel` observation was written and `_full_channel` remained `None` after the incompatible request.

**Correction direction:** Dispatch full-metadata capture by the resolved peer type: `GetFullChatRequest` for basic groups and `GetFullChannelRequest` for channels/supergroups. Feed either successful response into the existing raw-observation and profile-photo paths.

## Supporting validation

The existing channel-isolation tests and selected tests for run-state closure, immutable run channel binding, two-factor login, attachment removal, media streaming, and archive-rebinding rejection all passed: **60 passed in 3.99 seconds**. These passing tests cover the surrounding guards; the three failing scenarios above were verified separately using the focused reproductions described under each finding. No live Telegram requests or desktop UI interactions were used.
