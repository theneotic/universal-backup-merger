#!/usr/bin/env python3
"""
Universal music-app backup merger for Metrolist.

This is a best-effort importer for ZIP-based Android music-app backups. It:
  1. Scans a folder for .backup/.zip files.
  2. Detects the Metrolist backup by filename or database schema.
  3. Treats every other readable backup as a source.
  4. Merges canonical tables when compatible.
  5. Uses field aliases for differently named song/artist/album/playlist tables.
  6. Skips records it cannot understand and writes a merge report.

It cannot guarantee playable results from an app that does not preserve YouTube
video IDs or a compatible music database. Such records can still be imported as
metadata, but Metrolist may not be able to play them.

Examples:
  python3 universal_backup_merger.py --folder ./uploads --once
  python3 universal_backup_merger.py --folder ./uploads --watch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = logging.getLogger("universal-backup-merger")
SQLITE_HEADER = b"SQLite format 3\x00"
BACKUP_SUFFIXES = {".backup", ".zip"}
EXCLUDED_WORDS = {"merged", "converted", "output", "result"}


class MergeError(RuntimeError):
    pass


@dataclass
class Candidate:
    path: Path
    db_member: str
    preferences_member: str | None
    tables: set[str] = field(default_factory=set)
    table_columns: dict[str, set[str]] = field(default_factory=dict)
    score: int = 0


def q(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def norm(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def is_archive(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in BACKUP_SUFFIXES


def zip_is_complete(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def wait_until_stable(path: Path, stable_seconds: float = 2.0, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    previous: tuple[int, int] | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            time.sleep(0.5)
            continue
        if current == previous:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds and zip_is_complete(path):
                return True
        else:
            previous = current
            stable_since = None
        time.sleep(0.5)
    return False


def find_sqlite_member(zf: zipfile.ZipFile) -> str | None:
    members = [info for info in zf.infolist() if not info.is_dir()]
    preferred = sorted(
        members,
        key=lambda info: (
            0 if Path(info.filename).name.lower() in {"song.db", "music.db", "database.db", "main.db"} else 1,
            0 if Path(info.filename).suffix.lower() == ".db" else 1,
            info.filename,
        ),
    )
    for info in preferred:
        try:
            with zf.open(info) as stream:
                if stream.read(16) == SQLITE_HEADER:
                    return info.filename
        except (OSError, RuntimeError, zipfile.BadZipFile):
            continue
    return None


def inspect_candidate(path: Path) -> Candidate | None:
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return None
            db_member = find_sqlite_member(zf)
            if not db_member:
                return None
            prefs = next(
                (name for name in zf.namelist() if Path(name).name == "settings.preferences_pb"),
                None,
            )
            with tempfile.TemporaryDirectory(prefix="inspect_backup_") as tmp:
                db_path = Path(tmp) / "source.db"
                with zf.open(db_member) as source, db_path.open("wb") as target:
                    shutil.copyfileobj(source, target)
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    tables = {
                        row[0]
                        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    table_columns = {
                        table: {row[1] for row in con.execute(f"PRAGMA table_info({q(table)})")}
                        for table in tables
                    }
                finally:
                    con.close()
            lowered = path.name.lower()
            score = 0
            if "metrolist" in lowered or "metro_list" in lowered:
                score += 100
            if prefs:
                score += 30
            if "song" in tables:
                score += 20
            song_cols = table_columns.get("song", set())
            if {"id", "title"}.issubset(song_cols):
                score += 20
            if "liked" in song_cols and "totalPlayTime" in song_cols:
                score += 20
            return Candidate(path, db_member, prefs, tables, table_columns, score)
    except (OSError, zipfile.BadZipFile, sqlite3.Error):
        return None


def discover(folder: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        if not is_archive(path):
            continue
        lowered = path.name.lower()
        if any(word in lowered for word in EXCLUDED_WORDS):
            continue
        candidate = inspect_candidate(path)
        if candidate:
            candidates.append(candidate)
    return candidates


def choose_target(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (candidate.score, candidate.path.stat().st_mtime_ns))


def extract_candidate(candidate: Candidate, destination: Path) -> tuple[Path, Path | None]:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(candidate.path) as zf:
        db_path = destination / "song.db"
        with zf.open(candidate.db_member) as source, db_path.open("wb") as target:
            shutil.copyfileobj(source, target)
        preferences = None
        if candidate.preferences_member:
            preferences = destination / "settings.preferences_pb"
            with zf.open(candidate.preferences_member) as source, preferences.open("wb") as target:
                shutil.copyfileobj(source, target)
    return db_path, preferences


def table_exists(con: sqlite3.Connection, schema: str, table: str) -> bool:
    return bool(con.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def columns(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA {schema}.table_info({q(table)})")]


def primary_keys(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA {schema}.table_info({q(table)})").fetchall()
    return [row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5]]


def count(con: sqlite3.Connection, schema: str, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {schema}.{q(table)}").fetchone()[0])


def merge_same_named_table(
    target: sqlite3.Connection,
    source_schema: str,
    table: str,
    report: dict[str, Any],
    update_existing: bool = False,
) -> None:
    if not table_exists(target, "main", table) or not table_exists(target, source_schema, table):
        return
    target_cols = columns(target, "main", table)
    source_cols = columns(target, source_schema, table)
    common = [col for col in target_cols if col in source_cols]
    keys = primary_keys(target, "main", table)
    if not common or not keys:
        return
    before = count(target, "main", table)
    col_list = ", ".join(q(col) for col in common)
    try:
        target.execute(
            f"INSERT OR IGNORE INTO main.{q(table)} ({col_list}) "
            f"SELECT {col_list} FROM {source_schema}.{q(table)}"
        )
        inserted = count(target, "main", table) - before
        updated = 0
        if update_existing:
            non_keys = [col for col in common if col not in keys]
            if non_keys:
                match = " AND ".join(f"a.{q(key)}=m.{q(key)}" for key in keys)
                assignments = ", ".join(
                    f"{q(col)}=(SELECT a.{q(col)} FROM {source_schema}.{q(table)} AS a WHERE {match})"
                    for col in non_keys
                )
                updated = max(target.execute(
                    f"UPDATE main.{q(table)} AS m SET {assignments} "
                    f"WHERE EXISTS (SELECT 1 FROM {source_schema}.{q(table)} AS a WHERE {match})"
                ).rowcount, 0)
        report["tables"][table] = {
            "before": before,
            "inserted": inserted,
            "updated": updated,
            "after": count(target, "main", table),
        }
    except sqlite3.Error as exc:
        report["skipped_tables"][table] = str(exc)
        LOG.warning("Skipped incompatible table %s: %s", table, exc)


def first_value(row: dict[str, Any], aliases: list[str]) -> Any:
    normalized = {norm(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(norm(alias))
        if value is not None and value != "":
            return value
    return None


def table_by_alias(con: sqlite3.Connection, aliases: list[str], excluded: set[str] | None = None) -> str | None:
    excluded = excluded or set()
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    by_norm = {norm(row[0]): row[0] for row in tables}
    for alias in aliases:
        found = by_norm.get(norm(alias))
        if found and found not in excluded:
            return found
    return None


def rows_as_dicts(con: sqlite3.Connection, table: str):
    cur = con.execute(f"SELECT * FROM {q(table)}")
    names = [description[0] for description in cur.description]
    for values in cur:
        yield dict(zip(names, values))


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha1(str(value).encode("utf-8", "replace")).hexdigest()[:18]
    return f"import_{prefix}_{digest}"


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def duration_seconds(value: Any) -> int:
    number = as_int(value, -1)
    if number > 10000:  # Many apps store duration in milliseconds.
        number = round(number / 1000)
    return number


def insert_if_possible(con: sqlite3.Connection, table: str, values: dict[str, Any]) -> bool:
    target_cols = set(columns(con, "main", table))
    usable = {key: value for key, value in values.items() if key in target_cols}
    if not usable:
        return False
    names = list(usable)
    placeholders = ", ".join("?" for _ in names)
    try:
        con.execute(
            f"INSERT OR IGNORE INTO main.{q(table)} ({', '.join(q(name) for name in names)}) "
            f"VALUES ({placeholders})",
            [usable[name] for name in names],
        )
        return True
    except sqlite3.Error:
        return False


def import_generic_entities(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    source_label: str,
    report: dict[str, Any],
) -> None:
    """Import common entities when an app uses non-Metrolist table names."""
    canonical = {name.lower() for name in ["song", "artist", "album", "playlist", "lyrics", "playlist_song_map"]}

    artist_table = table_by_alias(source, ["artists", "artist", "performers", "performer", "authors"])
    album_table = table_by_alias(source, ["albums", "album", "releases", "release"])
    song_table = table_by_alias(source, ["songs", "song", "tracks", "track", "music", "media"])
    playlist_table = table_by_alias(source, ["playlists", "playlist", "collections", "collection", "mixes", "mix"])
    lyrics_table = table_by_alias(source, ["lyrics", "lyric", "song_lyrics", "track_lyrics"])

    # Do not reinterpret a canonical table that was already handled by direct merging.
    if song_table and song_table.lower() == "song":
        song_table = None
    if artist_table and artist_table.lower() == "artist":
        artist_table = None
    if album_table and album_table.lower() == "album":
        album_table = None
    if playlist_table and playlist_table.lower() == "playlist":
        playlist_table = None
    if lyrics_table and lyrics_table.lower() == "lyrics":
        lyrics_table = None

    artist_ids: dict[str, str] = {}
    album_ids: dict[str, str] = {}

    if artist_table:
        for row in rows_as_dicts(source, artist_table):
            raw_id = first_value(row, ["id", "artistId", "channelId", "channel_id"])
            name = first_value(row, ["name", "artistName", "artist", "title", "author"])
            if name is None:
                continue
            aid = str(raw_id) if raw_id is not None and str(raw_id).strip() else stable_id("artist", name)
            artist_ids[str(raw_id)] = aid if raw_id is not None else aid
            insert_if_possible(target, "artist", {
                "id": aid,
                "name": str(name),
                "thumbnailUrl": first_value(row, ["thumbnailUrl", "imageUrl", "avatar", "coverUrl"]),
                "channelId": first_value(row, ["channelId", "channel_id"]),
                "lastUpdateTime": as_int(first_value(row, ["lastUpdateTime", "updatedAt", "updated_at"]), 0),
                "isLocal": 0,
            })

    if album_table:
        for row in rows_as_dicts(source, album_table):
            raw_id = first_value(row, ["id", "albumId", "releaseId"])
            title = first_value(row, ["title", "name", "albumName", "releaseName"])
            if title is None:
                continue
            aid = str(raw_id) if raw_id is not None and not str(raw_id).isdigit() else stable_id("album", title)
            album_ids[str(raw_id)] = aid if raw_id is not None else aid
            insert_if_possible(target, "album", {
                "id": aid,
                "playlistId": None,
                "title": str(title),
                "year": as_int(first_value(row, ["year", "releaseYear"]), 0) or None,
                "thumbnailUrl": first_value(row, ["thumbnailUrl", "imageUrl", "coverUrl", "artworkUrl"]),
                "songCount": as_int(first_value(row, ["songCount", "trackCount", "count"]), 0),
                "duration": duration_seconds(first_value(row, ["duration", "durationMs", "length"])),
                "explicit": as_int(first_value(row, ["explicit", "isExplicit"]), 0),
                "lastUpdateTime": as_int(first_value(row, ["lastUpdateTime", "updatedAt"]), 0),
                "isLocal": 0,
                "isUploaded": 0,
            })

    imported_song_ids: list[str] = []
    if song_table:
        for row in rows_as_dicts(source, song_table):
            raw_id = first_value(row, ["id", "videoId", "video_id", "trackId", "songId", "youtubeVideoId", "youtubeId"])
            title = first_value(row, ["title", "name", "trackName", "songName"])
            if raw_id is None or title is None:
                continue
            youtube_id = first_value(row, ["videoId", "video_id", "youtubeVideoId", "youtubeId"])
            if youtube_id is not None:
                sid = str(youtube_id)
            else:
                sid = str(raw_id)
                if sid.isdigit() or len(sid) < 5:
                    sid = stable_id("song", f"{source_label}:{sid}")
            album_raw = first_value(row, ["albumId", "album_id", "releaseId"])
            album_name = first_value(row, ["albumName", "album", "releaseName"])
            album_id = album_ids.get(str(album_raw), str(album_raw) if album_raw else None)
            if album_id is None and album_name:
                album_id = stable_id("album", album_name)
                insert_if_possible(target, "album", {
                    "id": album_id, "title": str(album_name), "songCount": 0,
                    "duration": 0, "explicit": 0, "lastUpdateTime": 0, "isLocal": 0, "isUploaded": 0,
                })
            inserted = insert_if_possible(target, "song", {
                "id": sid,
                "title": str(title),
                "duration": duration_seconds(first_value(row, ["duration", "durationMs", "length", "durationSeconds"])),
                "thumbnailUrl": first_value(row, ["thumbnailUrl", "imageUrl", "coverUrl", "artworkUrl", "coverArtUrl"]),
                "albumId": album_id,
                "albumName": str(album_name) if album_name is not None else None,
                "explicit": as_int(first_value(row, ["explicit", "isExplicit"]), 0),
                "year": as_int(first_value(row, ["year", "releaseYear"]), 0) or None,
                "date": as_int(first_value(row, ["date", "createdAt"]), 0) or None,
                "dateModified": as_int(first_value(row, ["dateModified", "updatedAt"]), 0) or None,
                "liked": as_int(first_value(row, ["liked", "isLiked", "favorite", "favorited"]), 0),
                "likedDate": as_int(first_value(row, ["likedDate", "favoritedAt"]), 0) or None,
                "totalPlayTime": as_int(first_value(row, ["totalPlayTime", "playTime", "playedMs"]), 0),
                "inLibrary": as_int(first_value(row, ["inLibrary", "isInLibrary"]), 0),
                "isLocal": 0,
                "isDownloaded": 0,
                "isUploaded": 0,
                "isVideo": 0,
                "isEpisode": 0,
                "isCached": 0,
                "lyricsOffset": 0,
                "romanizeLyrics": 1,
            })
            if inserted:
                imported_song_ids.append(sid)

            artist_raw = first_value(row, ["artistId", "artist_id", "channelId", "artist"])
            artist_name = first_value(row, ["artistName", "artist", "performer", "author"])
            if artist_raw is not None or artist_name is not None:
                aid = artist_ids.get(str(artist_raw)) if artist_raw is not None else None
                if aid is None:
                    aid = str(artist_raw) if artist_raw is not None and not str(artist_raw).isdigit() else stable_id("artist", artist_name or artist_raw)
                    insert_if_possible(target, "artist", {
                        "id": aid, "name": str(artist_name or artist_raw),
                        "channelId": str(artist_raw) if artist_raw is not None and str(artist_raw).startswith("UC") else None,
                        "lastUpdateTime": 0, "isLocal": 0,
                    })
                insert_if_possible(target, "song_artist_map", {"songId": sid, "artistId": aid, "position": 0})

            if album_id:
                insert_if_possible(target, "song_album_map", {"songId": sid, "albumId": album_id, "index": 0})

    if lyrics_table:
        for row in rows_as_dicts(source, lyrics_table):
            sid = first_value(row, ["id", "songId", "trackId", "videoId", "youtubeVideoId"])
            text = first_value(row, ["lyrics", "text", "content", "lyricText"])
            if sid is not None and text:
                insert_if_possible(target, "lyrics", {
                    "id": str(sid), "lyrics": str(text),
                    "provider": str(first_value(row, ["provider", "source"]) or source_label),
                    "translatedLyrics": str(first_value(row, ["translatedLyrics"]) or ""),
                    "translationLanguage": str(first_value(row, ["translationLanguage"]) or ""),
                    "translationMode": str(first_value(row, ["translationMode"]) or ""),
                })

    if playlist_table:
        for row in rows_as_dicts(source, playlist_table):
            raw_id = first_value(row, ["id", "playlistId", "collectionId"])
            name = first_value(row, ["name", "title", "playlistName"])
            if raw_id is None or name is None:
                continue
            pid = str(raw_id) if not str(raw_id).isdigit() else stable_id("playlist", f"{source_label}:{raw_id}")
            insert_if_possible(target, "playlist", {
                "id": pid, "name": str(name),
                "browseId": first_value(row, ["browseId", "remoteId"]),
                "createdAt": as_int(first_value(row, ["createdAt", "created_at"]), 0) or None,
                "lastUpdateTime": as_int(first_value(row, ["lastUpdateTime", "updatedAt"]), 0) or None,
                "isEditable": 1, "remoteSongCount": as_int(first_value(row, ["songCount", "trackCount"]), 0),
                "isLocal": 1, "isAutoSync": 0,
            })

    report["generic"] = {
        "song_table": song_table,
        "artist_table": artist_table,
        "album_table": album_table,
        "playlist_table": playlist_table,
        "lyrics_table": lyrics_table,
        "imported_song_candidates": len(imported_song_ids),
    }


def validate_db(path: Path) -> None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_errors:
            raise MergeError(
                f"Output failed validation: integrity={integrity!r}, foreign-key errors={len(foreign_errors)}"
            )
    finally:
        con.close()


def merge(target_candidate: Candidate, source_candidates: list[Candidate], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"Metrolist_Universal_Merged_{stamp}.backup"
    if output.exists():
        output = output_dir / f"Metrolist_Universal_Merged_{stamp}_{time.time_ns() % 1000000:06d}.backup"

    all_reports: dict[str, Any] = {
        "target": target_candidate.path.name,
        "sources": [candidate.path.name for candidate in source_candidates],
        "skipped_sources": [],
        "source_reports": [],
    }

    with tempfile.TemporaryDirectory(prefix="universal_merge_") as tmp_name:
        tmp = Path(tmp_name)
        target_db, target_preferences = extract_candidate(target_candidate, tmp / "target")
        work_db = tmp / "song.db"
        source_db_paths: list[tuple[Candidate, Path]] = []
        for index, candidate in enumerate(source_candidates):
            try:
                source_db, _ = extract_candidate(candidate, tmp / f"source_{index}")
                source_db_paths.append((candidate, source_db))
            except (OSError, zipfile.BadZipFile, MergeError) as exc:
                all_reports["skipped_sources"].append({"file": candidate.path.name, "reason": str(exc)})

        target_ro = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
        work = sqlite3.connect(work_db)
        try:
            target_ro.backup(work)
        finally:
            target_ro.close()

        work.execute("PRAGMA foreign_keys=OFF")
        for candidate, source_db in source_db_paths:
            source_report: dict[str, Any] = {
                "file": candidate.path.name,
                "schema_tables": sorted(candidate.tables),
                "tables": {},
                "skipped_tables": {},
            }
            source_schema = f"src_{len(all_reports['source_reports'])}"
            work.execute(f"ATTACH DATABASE ? AS {q(source_schema)}", (str(source_db),))
            source_con = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
            try:
                work.execute("BEGIN")
                for table in ["artist", "album", "song", "format", "playlist"]:
                    merge_same_named_table(work, source_schema, table, source_report)
                # Existing Metrolist lyrics are preserved; missing IDs are filled.
                if table_exists(work, "main", "lyrics") and table_exists(work, source_schema, "lyrics"):
                    before = count(work, "main", "lyrics")
                    work.execute(
                        f"INSERT OR IGNORE INTO main.\"lyrics\" "
                        f"(\"id\", \"lyrics\", \"provider\", \"translatedLyrics\", \"translationLanguage\", \"translationMode\") "
                        f"SELECT \"id\", \"lyrics\", COALESCE(NULLIF(\"source\", ''), 'Imported'), '', '', '' FROM {source_schema}.\"lyrics\""
                    )
                    source_report["tables"]["lyrics"] = {"before": before, "inserted": count(work, "main", "lyrics") - before, "after": count(work, "main", "lyrics")}
                for table in ["album_artist_map", "song_album_map", "song_artist_map", "set_video_id", "playCount"]:
                    merge_same_named_table(work, source_schema, table, source_report)
                # Autoincrement IDs are not copied from an unrelated app.
                if table_exists(work, "main", "event") and table_exists(work, source_schema, "event"):
                    before = count(work, "main", "event")
                    source_cols = set(columns(work, source_schema, "event"))
                    required = {"songId", "timestamp", "playTime"}
                    if required.issubset(source_cols):
                        work.execute(f"INSERT INTO main.\"event\" (\"songId\", \"timestamp\", \"playTime\") SELECT \"songId\", \"timestamp\", \"playTime\" FROM {source_schema}.\"event\"")
                        source_report["tables"]["event"] = {"before": before, "inserted": count(work, "main", "event") - before, "after": count(work, "main", "event")}
                if table_exists(work, "main", "search_history") and table_exists(work, source_schema, "search_history"):
                    before = count(work, "main", "search_history")
                    if "query" in columns(work, source_schema, "search_history"):
                        work.execute(f"INSERT OR IGNORE INTO main.\"search_history\" (\"query\") SELECT \"query\" FROM {source_schema}.\"search_history\"")
                        source_report["tables"]["search_history"] = {"before": before, "inserted": count(work, "main", "search_history") - before, "after": count(work, "main", "search_history")}
                if table_exists(work, "main", "related_song_map") and table_exists(work, source_schema, "related_song_map"):
                    source_cols = set(columns(work, source_schema, "related_song_map"))
                    if {"songId", "relatedSongId"}.issubset(source_cols):
                        before = count(work, "main", "related_song_map")
                        work.execute(f"INSERT INTO main.\"related_song_map\" (\"songId\", \"relatedSongId\") SELECT a.\"songId\", a.\"relatedSongId\" FROM {source_schema}.\"related_song_map\" AS a WHERE NOT EXISTS (SELECT 1 FROM main.\"related_song_map\" AS m WHERE m.\"songId\"=a.\"songId\" AND m.\"relatedSongId\"=a.\"relatedSongId\")")
                        source_report["tables"]["related_song_map"] = {"before": before, "inserted": count(work, "main", "related_song_map") - before, "after": count(work, "main", "related_song_map")}
                if table_exists(work, "main", "playlist_song_map") and table_exists(work, source_schema, "playlist_song_map"):
                    source_cols = set(columns(work, source_schema, "playlist_song_map"))
                    required = {"playlistId", "songId", "position"}
                    if required.issubset(source_cols):
                        before = count(work, "main", "playlist_song_map")
                        set_video = '"setVideoId"' if "setVideoId" in source_cols else "NULL"
                        work.execute(f"INSERT INTO main.\"playlist_song_map\" (\"playlistId\", \"songId\", \"position\", \"setVideoId\") SELECT a.\"playlistId\", a.\"songId\", a.\"position\", a.{set_video} FROM {source_schema}.\"playlist_song_map\" AS a WHERE EXISTS (SELECT 1 FROM main.\"playlist\" AS p WHERE p.\"id\"=a.\"playlistId\") AND EXISTS (SELECT 1 FROM main.\"song\" AS s WHERE s.\"id\"=a.\"songId\") AND NOT EXISTS (SELECT 1 FROM main.\"playlist_song_map\" AS m WHERE m.\"playlistId\"=a.\"playlistId\" AND m.\"songId\"=a.\"songId\" AND m.\"position\"=a.\"position\")")
                        source_report["tables"]["playlist_song_map"] = {"before": before, "inserted": count(work, "main", "playlist_song_map") - before, "after": count(work, "main", "playlist_song_map")}
                work.commit()
                # Use generic aliases only when the source did not have the canonical table.
                import_generic_entities(work, source_con, candidate.path.stem, source_report)
                work.commit()
            except Exception as exc:
                work.rollback()
                source_report["fatal_error"] = str(exc)
                LOG.warning("Source %s partially skipped: %s", candidate.path.name, exc)
            finally:
                source_con.close()
                work.execute(f"DETACH DATABASE {q(source_schema)}")
            all_reports["source_reports"].append(source_report)

        work.execute("PRAGMA foreign_keys=ON")
        work.execute("VACUUM")
        work.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        work.close()
        validate_db(work_db)

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(work_db, "song.db")
            if target_preferences and target_preferences.exists():
                zf.write(target_preferences, "settings.preferences_pb")
        report_path = output.with_suffix(".json")
        report_path.write_text(json.dumps(all_reports, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG.info("Created %s", output)
    LOG.info("Wrote report %s", report_path)
    return output


def process(folder: Path, output_dir: Path, processed: set[tuple[str, ...]]) -> Path | None:
    candidates = discover(folder)
    target = choose_target(candidates)
    if not target:
        return None
    sources = [candidate for candidate in candidates if candidate.path != target.path]
    if not sources:
        return None
    if not all(wait_until_stable(candidate.path) for candidate in [target, *sources]):
        LOG.info("Waiting for backup uploads to finish")
        return None
    key = tuple(sorted(str(candidate.path.resolve()) for candidate in [target, *sources]))
    if key in processed:
        return None
    LOG.info("Target Metrolist backup: %s", target.path.name)
    LOG.info("Source backups: %s", ", ".join(candidate.path.name for candidate in sources))
    output = merge(target, sources, output_dir)
    processed.add(key)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge different music-app backups into Metrolist.")
    parser.add_argument("--folder", type=Path, default=Path("uploads"), help="Folder receiving backup uploads.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output folder; defaults to FOLDER/merged.")
    parser.add_argument("--once", action="store_true", help="Process one complete set and exit.")
    parser.add_argument("--watch", action="store_true", help="Keep watching. This is the default unless --once is used.")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    folder = args.folder.expanduser().resolve()
    output_dir = (args.output_dir or folder / "merged").expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    processed: set[tuple[str, ...]] = set()
    LOG.info("Scanning %s", folder)
    while True:
        try:
            output = process(folder, output_dir, processed)
            if output:
                print(output)
                if args.once:
                    return 0
            elif args.once:
                LOG.info("No complete Metrolist plus source-backup set found")
                return 1
            time.sleep(max(args.interval, 0.5))
        except KeyboardInterrupt:
            LOG.info("Stopped")
            return 0
        except (OSError, sqlite3.Error, zipfile.BadZipFile, MergeError) as exc:
            LOG.error("Merge attempt failed: %s", exc)
            if args.once:
                return 2
            time.sleep(max(args.interval, 0.5))


if __name__ == "__main__":
    sys.exit(main())
