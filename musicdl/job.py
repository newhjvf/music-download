"""One run of musicdl (load -> skip existing -> search -> download -> report),
shared by the command line and the window. Progress is reported through
``JobEvents`` callbacks, which are called from worker threads."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from spotdl.types.song import Song

from musicdl.matching import MatchResult, default_provider_factory, find_matches
from musicdl.pipeline import downloader_settings, prepare_for_download, split_existing, write_report

logger = logging.getLogger(__name__)


@dataclass
class JobOptions:
    source_kind: str  # "csv" | "url"
    source: str  # CSV path or Spotify link
    out_dir: Path = Path("music")
    threads: int = 4
    bitrate: str = "320k"
    dry_run: bool = False
    only_verified: bool = False
    report_path: Optional[Path] = None
    env_file: Optional[Path] = None

    @property
    def report(self) -> Path:
        return self.report_path or Path(self.out_dir) / "not_found.csv"


@dataclass
class JobEvents:
    """All callbacks are optional. ``key`` identifies a song (``song.url``)."""

    info: Callable[[str], None] = lambda message: None
    songs_loaded: Callable[[List[Song], List[Song]], None] = lambda todo, existing: None
    matched: Callable[[MatchResult], None] = lambda match: None
    download_status: Callable[[str, str, int], None] = lambda key, status, percent: None
    phase: Callable[[str, int], None] = lambda name, count: None  # "search" | "download"


@dataclass
class JobSummary:
    total: int = 0
    existing: int = 0
    processed: int = 0
    failed: List[Tuple[Song, str]] = field(default_factory=list)
    matches: List[MatchResult] = field(default_factory=list)
    report: Optional[Path] = None

    @property
    def succeeded(self) -> int:
        return self.processed - len(self.failed)


class JobError(Exception):
    """User-facing error (bad input, missing file...)."""


def load_songs(options: JobOptions, info: Callable[[str], None]) -> List[Song]:
    if options.source_kind == "csv":
        from musicdl.csv_import import CsvFormatError, songs_from_csv

        path = Path(options.source)
        if not path.is_file():
            raise JobError(f"Файл не найден: {path}")
        try:
            return songs_from_csv(path)
        except CsvFormatError as exc:
            raise JobError(str(exc)) from exc
        except UnicodeDecodeError as exc:
            raise JobError(f"{path.name}: файл не в кодировке UTF-8") from exc

    from musicdl.spotify_input import (
        SpotifyInputError,
        check_url,
        init_spotify,
        load_credentials,
        songs_from_url,
    )

    try:
        check_url(options.source)
        official = init_spotify(*load_credentials(options.env_file))
    except SpotifyInputError as exc:
        raise JobError(str(exc)) from exc
    info("Spotify: " + ("официальный API (ключи из .env)" if official else "без ключей"))
    info("Получаю список треков из Spotify… (большой плейлист — до минуты)")
    songs = songs_from_url(options.source, options.threads)
    info(f"Из Spotify получено треков: {len(songs)}")
    return songs


def ensure_ffmpeg(info: Callable[[str], None]) -> None:
    """Download ffmpeg with spotDL's helper if it is missing."""
    from spotdl.utils.ffmpeg import download_ffmpeg, is_ffmpeg_installed

    if is_ffmpeg_installed():
        return
    info("ffmpeg не найден — скачиваю (один раз, ~80 МБ)…")
    try:
        download_ffmpeg()
    except Exception as exc:
        raise JobError(f"Не удалось скачать ffmpeg: {exc}. Выполните: spotdl --download-ffmpeg") from exc
    if not is_ffmpeg_installed():
        raise JobError("ffmpeg не установлен. Выполните: spotdl --download-ffmpeg")


def download_songs(
    songs: List[Song],
    options: JobOptions,
    on_status: Callable[[str, str, int], None],
) -> List[Tuple[Song, Optional[Path]]]:
    """spotDL's Downloader with its progress forwarded to ``on_status``."""
    from spotdl.download.downloader import Downloader

    if not songs:
        return []
    Path(options.out_dir).mkdir(parents=True, exist_ok=True)
    downloader = Downloader(downloader_settings(options.out_dir, options.threads, options.bitrate))
    downloader.progress_handler.update_callback = lambda tracker, message: on_status(
        tracker.song.url, message, int(tracker.progress or 0)
    )
    results = downloader.download_multiple_songs(songs)
    for error in downloader.errors:
        logger.warning(error)
    return results


def run_job(
    options: JobOptions,
    events: Optional[JobEvents] = None,
    provider_factory=default_provider_factory,
    downloader: Callable = download_songs,
    ffmpeg_check: Callable[[Callable[[str], None]], None] = ensure_ffmpeg,
) -> JobSummary:
    events = events or JobEvents()
    summary = JobSummary()

    songs = load_songs(options, events.info)
    if not songs:
        raise JobError("Во входных данных нет ни одного трека")

    todo, existing = split_existing(songs, options.out_dir)
    summary.total, summary.existing, summary.processed = len(songs), len(existing), len(todo)
    events.songs_loaded(todo, existing)
    events.info(f"Треков: {len(songs)}, уже скачано: {len(existing)}, к обработке: {len(todo)}")

    if todo:
        events.info("Ищу треки (YouTube Music, YouTube, SoundCloud)…")
        events.phase("search", len(todo))
        summary.matches = find_matches(
            todo, options.threads, provider_factory, options.only_verified, on_result=events.matched
        )
    summary.failed = [(m.song, m.error or "not found") for m in summary.matches if not m.found]

    if not options.dry_run:
        to_download = prepare_for_download(summary.matches)
        if to_download:
            ffmpeg_check(events.info)
            events.info(f"Скачиваю {len(to_download)} трек(ов)…")
            events.phase("download", len(to_download))
            for song, path in downloader(to_download, options, events.download_status):
                if path is None or not Path(path).exists():
                    summary.failed.append((song, "download failed"))
                    events.download_status(song.url, "Error", 0)
                else:
                    events.download_status(song.url, "Done", 100)

    summary.report = write_report(options.report, summary.failed)
    return summary
