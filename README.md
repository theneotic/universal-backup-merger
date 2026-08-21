# Universal Music-App Backup Merger

`universal_backup_merger.py` is a best-effort importer for different music-app backup exports. It accepts `.backup` and `.zip` files, finds an SQLite database inside each archive, identifies the Metrolist backup by its filename or schema, and treats the remaining archives as source backups.

> This script works with **backup/export files**, not raw Android installer files ending in `.apk`. A raw installer APK contains application code, not the user’s SQLite library data. The app must first export its data as a `.backup`, `.zip`, or another database backup.

## Automatic mode

Put the Metrolist backup and one or more source-app backups into the same folder, then run:

```bash
python3 universal_backup_merger.py --folder /home/ubuntu/upload --watch
```

The script waits for each upload to stop changing, automatically detects the target and source files, and writes output to:

```text
/home/ubuntu/upload/merged/Metrolist_Universal_Merged_YYYYMMDD_HHMMSS.backup
```

For one scan only:

```bash
python3 universal_backup_merger.py --folder /home/ubuntu/upload --once
```

The target can have any filename if it contains Metrolist’s database structure or its `settings.preferences_pb` file. A source can have any filename; the script no longer requires the words `ArchiveTune` or `Metrolist` in the names.

## What is imported

When the source app uses the same database tables as Metrolist, the script merges compatible records from songs, artists, albums, playlists, lyrics, formats, relationships, events, search history, and related songs. When the source app uses different names, it searches for common aliases such as `tracks`, `media`, `performers`, `releases`, `collections`, and `lyric` and maps their basic metadata into Metrolist.

The script preserves Metrolist’s database schema and preferences as the base. Existing records are not overwritten by source records in the universal version. Unsupported tables are reported in the generated JSON report instead of stopping the complete merge.

## Output report

For every generated backup, a matching JSON file is created. For example:

```text
Metrolist_Universal_Merged_20260821_210605.backup
Metrolist_Universal_Merged_20260821_210605.json
```

The report lists the detected target, source files, imported row counts, skipped tables, and any generic table aliases detected.

## Important limitation

No universal converter can guarantee playback for every app. Metrolist generally needs a compatible YouTube video ID or equivalent source identifier. If another app stores only local file paths, encrypted IDs, Spotify IDs, or Apple Music IDs, the script can preserve basic metadata when possible, but Metrolist may need to search for or rematch those tracks before they play.

Keep the original backups until the merged backup has been restored and tested. The script never deletes source files.
