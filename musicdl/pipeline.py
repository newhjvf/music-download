"""Skip / download / report steps. Downloading, ffmpeg conversion and ID3
tagging are done entirely by spotDL's ``Downloader``."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from spotdl.types.song import Song
from spotdl.utils.formatter import create_file_name

from musicdl.matching import MatchResult, youtube_cover_url

logger = logging.getLogger(__name__)

FILENAME_TEMPLATE = "{artist} - {title}.{output-ext}"
FORMAT = "mp3"


def output_template(out_dir: Path) -> str:
    return str(Path(out_dir) / FILENAME_TEMPLATE)


def expected_path(song: Song, out_dir: Path) -> Path:
    """Where spotDL will save the song ("{artist} - {title}.mp3", sanitized)."""
    return create_file_name(song, output_template(out_dir), FORMAT)


# A real mp3 track is never this small; anything below is a broken/partial
# file (e.g. the program was closed or the connection dropped mid-way).
MIN_FILE_SIZE = 64 * 1024


def remove_incomplete(path: Path) -> bool:
    """Delete ``path`` if it is a partial file. Returns True if removed."""
    try:
        if path.is_file() and path.stat().st_size < MIN_FILE_SIZE:
            path.unlink()
            logger.warning("Removed incomplete file %s", path)
            return True
    except OSError:
        logger.warning("Could not remove incomplete file %s", path, exc_info=True)
    return False


def split_existing(songs: Iterable[Song], out_dir: Path) -> Tuple[List[Song], List[Song]]:
    """-> (songs to process, songs already downloaded). Partial files left by
    an interrupted run are deleted so the song is downloaded again."""
    todo: List[Song] = []
    existing: List[Song] = []
    for song in songs:
        path = expected_path(song, out_dir)
        remove_incomplete(path)
        (existing if path.exists() else todo).append(song)
    return todo, existing


def downloader_settings(out_dir: Path, threads: int, bitrate: str) -> Dict[str, Any]:
    return {
        "output": output_template(out_dir),
        "format": FORMAT,
        "bitrate": bitrate,
        "threads": max(1, threads),
        "overwrite": "skip",
        "audio_providers": ["youtube-music"],
        "lyrics_providers": [],  # no lyrics scraping/embedding
        "simple_tui": True,
        "print_errors": False,
        "load_config": False,
    }


def prepare_for_download(matches: Iterable[MatchResult]) -> List[Song]:
    """Attach the already found URL (so spotDL does not search again) and a
    cover fallback (CSV rows have no artwork)."""
    songs: List[Song] = []
    for match in matches:
        if not match.found:
            continue
        song = match.song
        song.download_url = match.url
        if not song.album_name:  # tags should always have an album: the source's, else the title (a single)
            song.album_name = match.album or song.name
        if not song.cover_url:
            song.cover_url = youtube_cover_url(match.url or "")
        songs.append(song)
    return songs


def download(songs: List[Song], out_dir: Path, threads: int, bitrate: str) -> List[Tuple[Song, Optional[Path]]]:
    # Imported lazily: the Downloader checks for ffmpeg on construction.
    from spotdl.download.downloader import Downloader

    if not songs:
        return []
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    downloader = Downloader(downloader_settings(out_dir, threads, bitrate))
    results = downloader.download_multiple_songs(songs)
    for error in downloader.errors:
        logger.warning(error)
    return results


REPORT_COLUMNS = ["Artist name", "Track name", "Album", "ISRC", "Reason"]


def write_report(path: Path, items: List[Tuple[Song, str]]) -> Optional[Path]:
    """Write not_found.csv (UTF-8 with BOM so Excel on Windows shows Cyrillic).
    Removes a stale report when everything was found."""
    path = Path(path)
    if not items:
        if path.exists():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for song, reason in items:
            writer.writerow(
                [", ".join(song.artists or [song.artist]), song.name, song.album_name or "", song.isrc or "", reason]
            )
    return path


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    if seconds <= 0:
        return "?"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
