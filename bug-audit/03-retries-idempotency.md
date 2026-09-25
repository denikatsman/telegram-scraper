Verified against the working tree on 2026-09-19, including the existing local changes above HEAD `188afe9fcfa3f9b1963edc23f5e46e143abd2632`. Reproductions used Python 3.14.5, installed Telethon 1.42.0, offline clients, and disposable temporary libraries. No production code, real archive, or Telegram account was changed.

1. **[P2] Detailed capture downloads and stores the primary attachment again for link previews and photos.**

   **Location:** [enrichment.py:315](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/enrichment.py:315), especially the primary selection at lines 363–370 and the download decision at lines 395–412. The caller completes the primary download in [engine.py:713](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:713) before calling the collector at line 736.

   `ContextCollector._inventory()` identifies the primary document only with `getattr(message.media, "document", None)`. For `MessageMediaWebPage`, the document selected by Telethon's primary downloader lives at `message.media.webpage.document`, so it never receives `primary_managed_elsewhere=True`. Photo sizes never receive the primary flag at all. Consequently, detailed capture sends a second download request for the same full document or selected photo size immediately after the primary downloader has saved it. The variant cache searches only its own manifests, so it cannot reuse the primary file.

   **Trigger and impact:** With the default `download_media=True` and `capture_context=True`, syncing a Telegram video attached to a link preview, or an ordinary photo post, performs duplicate acquisition and publishes two separately allocated copies of the same bytes. For large preview videos this doubles the transfer and storage for the affected attachment. Backups also include both copies.

   **Verified reproduction:** Used actual `types.Message`, `MessageMediaDocument`, `MessageMediaWebPage`, `MessageMediaPhoto`, `Document`, and `Photo` objects with Telethon's real `DownloadMethods.download_media` and its document/photo selection methods. Only the final file-transfer methods were replaced with deterministic offline byte writers. Each case ran through `TelegramService.run("sync")` with detailed capture enabled.

   | Message shape | Calls for the same file location | Primary files | Variant files with identical bytes |
   | --- | ---: | ---: | ---: |
   | Direct document, control | 1 | 1 | 0 |
   | Web preview containing that document | 2 | 1 | 1 |
   | Direct photo with one downloadable size | 2 | 1 | 1 |

   The preview case requested `InputDocumentFileLocation(id=100, thumb_size="")` twice. Its `.mp4` and variant `.bin` both contained 19 bytes with SHA-256 `fef970131a70d412a011d8b7c7401bd2d8efdcd39779cfc735b4cc66ceda2cc8`. The photo case requested `InputPhotoFileLocation(id=200, thumb_size="x")` twice and produced two identical 3-byte files. All primary downloads succeeded. A subsequent sync reused both existing copies; this finding concerns duplicate initial acquisition and publication.

   **Correction:** Share the primary attachment's exact object/size identity and verified file receipt with the variant collector. Reuse that file for the matching variant while continuing to acquire distinct thumbnails, photo sizes, and alternate documents.

2. **[P2] Retrying after primary-file publication creates another complete copy on every attempt.**

   **Location:** [engine.py:626](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:626), especially the unconditional suffix allocation at lines 633–635. The persisted lookup used to decide whether to download is at [engine.py:699](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:699); the completed file is added to the reading index only at lines 727–731.

   `_download()` publishes the completed, size-checked, hashed file using `os.link()`, then updates the in-memory record. If the process exits after publication and before the record's next durable save, the saved message still has `media_status="pending"` and no `media_file`. On restart, the download decision checks only that saved record. It downloads the attachment again, and `FileExistsError` always selects another suffixed filename without comparing the already-published file with the newly downloaded checksum. There is no primary-media receipt or recovery lookup that connects the completed artifact to a replayed attempt.

   **Trigger and impact:** A process termination in that publication/checkpoint window makes the next Sync repeat a completed transfer and keep another identical full-size file. Repeated interruptions accumulate complete duplicates; the eventual successful record points only to the last copy. The amplification matters for large attachments and can consume the space needed for recovery. Existing files are preserved; the verified defect is non-idempotent replay and unnecessary resource consumption.

   **Verified reproduction:** In a temporary library, used the existing offline `tests/test_engine.py` `Client`, `Message`, and `service` fixtures with a 17-byte attachment. A subprocess wrapper called the real `os.link()` and then `os._exit(71)` immediately after publishing the `.mp4`. Ran that interrupted attempt twice, then ran an unmodified successful sync on the same temporary library. Writer locks were held for every attempt.

   ```text
   Interrupted attempt 1: 1 published MP4; saved record pending, no media_file
   Interrupted attempt 2: 2 published MP4s; saved record pending, no media_file
   Successful retry:     another download; 3 published MP4s
   Distinct inodes:      3
   Distinct SHA-256s:    1
   ```

   The resulting names were `Message-1 (2025-03-11T00-00-00).mp4`, `Message-1 (2025-03-11T00-00-00)-100-1.mp4`, and `Message-1 (2025-03-11T00-00-00)-100-2.mp4`. The index referenced only the last file. This result was verified with process termination, not inferred from a simulated power-loss model.

   **Correction:** Publish a durable receipt keyed by source/media identity and checksum and reconcile it before replay. At minimum, after a fresh transfer, compare the verified bytes with an existing candidate before allocating another suffix. Preserve conflicting or damaged files; do not adopt an unverified file based only on its name or size.

3. **[P2] Adding a different supported link for an already-bound channel creates a duplicate archive and repeats the scrape.**

   **Location:** [channels.py:76](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/channels.py:76), particularly the string comparison at lines 80–82 and new-folder allocation at lines 83–91. [server.py:187](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:187) immediately selects the new folder. Resolution and binding later occur in [engine.py:1031](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1031); [storage.py:279](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/storage.py:279) checks identity only inside that selected folder.

   `Channels.add()` deduplicates the normalized input string, not the saved Telegram channel ID. A public username and a private/numeric message link remain different normalized strings even when they designate the same channel. The app allocates a fresh UUID folder before resolution, and the subsequent sync never compares the resolved ID with other archives' `channel.json` identities. Both archives therefore bind successfully to the same channel and independently download its history and media.

   **Trigger and impact:** After syncing a channel under its username, adding a supported numeric/private message link for that channel reports `added: true`, opens an empty archive, and repeats the capture. The same channel appears twice, its media is stored twice, and future syncs update only the selected copy. The existing duplicate-link behavior fails across supported representations of the same channel.

   **Verified reproduction:** Through `Runtime`, configured `@fixture`, connected an offline client, and synced one media post. The first archive saved `channel.json` with ID `123`. Added `https://t.me/c/123/42`, which the production normalizer converted to `-1000000000123`, then synced again. The offline client's `get_entity()` returned the same channel object with ID `123` for both representations.

   ```json
   {
     "added": true,
     "archive_entries": 2,
     "main_source_channel_id": 123,
     "second_source_channel_id": 123,
     "download_calls": [1, 1],
     "same_bytes": true,
     "different_path": true,
     "different_inode": true,
     "normalized_targets": ["@fixture", -1000000000123]
   }
   ```

   **Correction:** Compare resolved channel IDs with existing bound archives before committing a second capture, and reopen the existing archive when they match. Inputs whose identity is not yet known can be resolved before their first sync. Preserve any already-created duplicate archives during repair.

Verification controls: nine existing focused tests passed, covering history-page transient retries and exhaustion, missing/empty primary-media reuse, cancellable flood waits, variant reuse and corrupt-file recovery, and repeated comment pages. The three findings above come from additional executed offline reproductions, not from failures in those existing tests. No live Telegram request or app rebuild was performed.
