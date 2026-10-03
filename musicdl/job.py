"""One run of musicdl (load -> skip existing -> search -> download -> report),
shared by the command line and the window. Progress is reported through
``JobEvents`` callbacks, which are called from worker threads."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from spotdl.types.song import Song

from musicdl.cache import MatchCache
from musicdl.matching import CANCELLED, MatchResult, default_provider_factory, find_matches
from musicdl import network
from musicdl.network import ConnectionGuard, looks_like_network_error
from musicdl.pipeline import (
    MIN_FILE_SIZE,
    downloader_settings,
    expected_path,
    prepare_for_download,
    remove_incomplete,
    split_existing,
    write_report,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".musicdl" / "matches.json"


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
    # None = always search; read at creation so tests can redirect it
    cache_path: Optional[Path] = field(default_factory=lambda: DEFAULT_CACHE)
    cancel: Optional[threading.Event] = None  # set it to stop after the current tracks

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
    searching: Callable[[str, str], None] = lambda key, source: None  # song started on a source
    connection: Callable[[bool], None] = lambda online: None  # internet lost (False) / back (True)


@dataclass
class JobSummary:
    total: int = 0
    existing: int = 0
    processed: int = 0
    failed: List[Tuple[Song, str]] = field(default_factory=list)
    matches: List[MatchResult] = field(default_factory=list)
    report: Optional[Path] = None
    cancelled: int = 0

    @property
    def succeeded(self) -> int:
        return self.processed - len(self.failed) - self.cancelled


class JobError(Exception):
    """User-facing error (bad input, missing file...)."""


def load_songs(
    options: JobOptions, info: Callable[[str], None], guard: Optional[ConnectionGuard] = None
) -> List[Song]:
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
    songs = retry_on_network(lambda: songs_from_url(options.source, options.threads), guard)
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


def ensure_deno() -> None:
    """yt-dlp needs a JavaScript runtime (Deno) for some YouTube downloads;
    fetch spotDL's local copy once. Best effort: never stops the download."""
    try:
        from spotdl.utils.deno import download_deno, is_deno_installed

        if not is_deno_installed():
            logger.info("Deno not found, downloading")
            download_deno()
    except Exception as exc:
        logger.warning("Could not get Deno: %s", exc)


def log_download_causes() -> None:
    """spotDL reports a failed download only as "YT-DLP download error - <url>";
    also write the real reason to the log. Idempotent."""
    from spotdl.providers.audio.base import AudioProvider
    from musicdl.matching import short_error

    original = AudioProvider.get_download_metadata
    if getattr(original, "_musicdl_logs_cause", False):
        return

    def get_download_metadata(self, url, download=False):
        try:
            return original(self, url, download)
        except Exception as exc:
            logger.warning("Download of %s failed: %s", url, short_error(exc.__cause__ or exc, 200))
            raise

    get_download_metadata._musicdl_logs_cause = True  # type: ignore[attr-defined]
    AudioProvider.get_download_metadata = get_download_metadata  # type: ignore[method-assign]


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
    ensure_deno()
    log_download_causes()
    # One track at a time: parallel downloads made YouTube/SoundCloud refuse
    # requests and flooded the connection; "threads" is for searching only.
    downloader = Downloader(downloader_settings(options.out_dir, 1, options.bitrate))
    downloader.progress_handler.update_callback = lambda tracker, message: on_status(
        tracker.song.url, message, int(tracker.progress or 0)
    )
    results = downloader.download_multiple_songs(songs)
    for error in downloader.errors:
        logger.warning(error)
    return results


def retry_on_network(action: Callable, guard: Optional[ConnectionGuard], attempts: int = 5):
    """Run ``action``; if it fails because the connection dropped, wait until
    it is back and try again."""
    for attempt in range(attempts):
        try:
            return action()
        except JobError:
            raise
        except Exception as exc:
            if guard is None or attempt == attempts - 1 or not looks_like_network_error(exc):
                raise
            logger.warning("Network error, will retry: %s", exc)
            if not guard.wait_until_online():
                raise JobError("Остановлено") from exc
    raise AssertionError("unreachable")


def is_complete(path: Optional[Path]) -> bool:
    return path is not None and Path(path).is_file() and Path(path).stat().st_size >= MIN_FILE_SIZE


def search_threads(download_threads: int) -> int:
    """Searching is much lighter than downloading: use twice the parallelism (max 8)."""
    return max(1, min(8, download_threads * 2))


def run_job(
    options: JobOptions,
    events: Optional[JobEvents] = None,
    provider_factory=default_provider_factory,
    downloader: Callable = download_songs,
    ffmpeg_check: Callable[[Callable[[str], None]], None] = ensure_ffmpeg,
    online_check: Optional[Callable[[], bool]] = None,
    download_retries: int = 2,
) -> JobSummary:
    events = events or JobEvents()
    summary = JobSummary()
    guard = ConnectionGuard(events.connection, options.cancel, online_check or network.is_online)

    songs = load_songs(options, events.info, guard)
    if not songs:
        raise JobError("Во входных данных нет ни одного трека")

    todo, existing = split_existing(songs, options.out_dir)
    summary.total, summary.existing, summary.processed = len(songs), len(existing), len(todo)
    events.songs_loaded(todo, existing)
    events.info(f"Треков: {len(songs)}, уже скачано: {len(existing)}, к обработке: {len(todo)}")

    if todo:
        events.phase("search", len(todo))
        cache = MatchCache(options.cache_path)
        known: Dict[int, MatchResult] = {}
        for index, song in enumerate(todo):
            match = cache.get(song, options.only_verified)
            if match:
                known[index] = match
                events.matched(match)
        to_search = [song for index, song in enumerate(todo) if index not in known]
        if known:
            events.info(f"Уже найдены раньше: {len(known)}, ищу остальные: {len(to_search)}…")
        else:
            events.info("Ищу треки (YouTube Music, YouTube, SoundCloud)…")

        searched = iter(
            find_matches(
                to_search,
                search_threads(options.threads),
                provider_factory,
                options.only_verified,
                on_result=events.matched,
                on_try=lambda song, source: events.searching(song.url, source),
                cancel=options.cancel,
                guard=guard,
            )
        )
        summary.matches = [known[i] if i in known else next(searched) for i in range(len(todo))]
        for match in summary.matches:
            cache.put(match, options.only_verified)
        cache.save()
    summary.cancelled = sum(1 for m in summary.matches if m.error == CANCELLED)
    summary.failed = [
        (m.song, m.error or "not found") for m in summary.matches if not m.found and m.error != CANCELLED
    ]

    cancelled = options.cancel is not None and options.cancel.is_set()
    if not options.dry_run and not cancelled:
        to_download = prepare_for_download(summary.matches)
        if to_download:
            retry_on_network(lambda: ffmpeg_check(events.info), guard)
            events.info(f"Скачиваю {len(to_download)} трек(ов)…")
            events.phase("download", len(to_download))

            def run_downloader(songs: List[Song]) -> None:
                try:
                    for song, path in downloader(songs, options, events.download_status):
                        results[song.url] = (song, path)
                except Exception as exc:  # e.g. the connection dropped mid-run
                    if not looks_like_network_error(exc):
                        raise
                    logger.warning("Download interrupted: %s", exc)

            results: Dict[str, Tuple[Song, Optional[Path]]] = {song.url: (song, None) for song in to_download}
            run_downloader(to_download)
            # Failed downloads (lost connection, YouTube hiccups) get another
            # chance once the connection is there.
            for _ in range(download_retries):
                retry = [song for song, path in results.values() if not is_complete(path)]
                if not retry or (options.cancel is not None and options.cancel.is_set()):
                    break
                if not guard.wait_until_online():
                    break
                for song in retry:
                    remove_incomplete(expected_path(song, options.out_dir))
                    events.download_status(song.url, "Retry", 0)
                events.info(f"Повторяю загрузку: {len(retry)} трек(ов)…")
                run_downloader(retry)

            for song, path in results.values():
                if is_complete(path):
                    events.download_status(song.url, "Done", 100)
                else:
                    remove_incomplete(expected_path(song, options.out_dir))
                    summary.failed.append((song, "download failed"))
                    events.download_status(song.url, "Error", 0)

    summary.report = write_report(options.report, summary.failed)
    return summary
