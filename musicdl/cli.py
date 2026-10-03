"""Command line: ``musicdl csv FILE`` / ``musicdl url SPOTIFY_LINK``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from rich.console import Console
from rich.table import Table

from musicdl import __version__
from musicdl import job as job_module
from musicdl.job import JobError, JobEvents, JobOptions
from musicdl.matching import MatchResult, default_provider_factory
from musicdl.pipeline import format_duration

console = Console()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--out", type=Path, default=Path("music"), help="папка для mp3 (по умолчанию ./music)")
    common.add_argument("-t", "--threads", type=int, default=4, help="параллельных поисков (по умолчанию 4); треки скачиваются по одному")
    common.add_argument("--dry-run", action="store_true", help="только поиск и сопоставление, без скачивания")
    common.add_argument("--bitrate", default="320k", help="битрейт mp3, например 192k или 320k (по умолчанию 320k)")
    common.add_argument("--report", type=Path, help="куда писать not_found.csv (по умолчанию в папку --out)")
    common.add_argument("--only-verified", action="store_true", help="только официальные загрузки (YouTube Music, каналы исполнителей, SoundCloud исполнителей)")
    common.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    parser = argparse.ArgumentParser(
        prog="musicdl",
        description="Скачивание музыки по метаданным (TuneMyMusic CSV или ссылка Spotify) через spotDL.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_csv = sub.add_parser("csv", parents=[common], help="CSV-экспорт TuneMyMusic (без Spotify)")
    p_csv.add_argument("file", type=Path, help="путь к CSV")

    p_url = sub.add_parser("url", parents=[common], help="ссылка Spotify: трек, альбом или плейлист")
    p_url.add_argument("link", help="https://open.spotify.com/...")
    p_url.add_argument("--env", type=Path, help="файл с ключами (по умолчанию ./.env)")
    return parser


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # spotDL/yt-dlp are chatty; our own messages go through `console`.
        for name in ("spotdl", "yt_dlp", "urllib3", "ytmusicapi"):
            logging.getLogger(name).setLevel(logging.ERROR)


def match_table(matches: List[MatchResult]) -> Table:
    table = Table(title="Сопоставление", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Трек")
    table.add_column("Найдено (источник)")
    table.add_column("Длит.", justify="right")
    table.add_column("Ссылка")
    for index, match in enumerate(matches, 1):
        track = f"{match.song.artist} - {match.song.name}"
        if match.found:
            found = f"{match.author} - {match.title}" if match.title else "?"
            source = match.source or "YouTube Music"
            if source == "YouTube Music" and not match.verified:
                source += ", видео"
            found += f" [dim]({source})[/dim]"
            table.add_row(str(index), track, found, format_duration(match.duration), match.url)
        else:
            table.add_row(str(index), track, f"[red]{match.error}[/red]", "", "")
    return table


def options_from_args(args: argparse.Namespace) -> JobOptions:
    return JobOptions(
        source_kind=args.mode,
        source=str(args.file) if args.mode == "csv" else args.link,
        out_dir=args.out,
        threads=args.threads,
        bitrate=args.bitrate,
        dry_run=args.dry_run,
        only_verified=args.only_verified,
        report_path=args.report,
        env_file=getattr(args, "env", None),
    )


def run(args: argparse.Namespace, provider_factory: Callable = default_provider_factory, **job_kwargs) -> int:
    options = options_from_args(args)
    with console.status("Работаю…") as status:
        events = JobEvents(
            info=lambda message: status.update(message),
            connection=lambda online: console.print(
                "[green]Интернет снова есть — продолжаю.[/green]"
                if online
                else "[red]Нет интернета. Жду подключения — работа продолжится сама.[/red]"
            ),
        )
        try:
            summary = job_module.run_job(options, events, provider_factory, **job_kwargs)
        except JobError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1

    console.print(f"Треков: {summary.total}, уже скачано: {summary.existing}, обработано: {summary.processed}")
    if options.dry_run:
        console.print(match_table(summary.matches))
    verb = "найдено" if options.dry_run else "скачано"
    console.print(
        f"Готово: {verb} {summary.succeeded} из {summary.processed}, пропущено (уже есть): {summary.existing}."
    )
    if summary.report:
        console.print(f"[yellow]Не удалось: {len(summary.failed)} — список в {summary.report}[/yellow]")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows consoles may default to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return run(args)
    except KeyboardInterrupt:
        console.print("[red]Прервано.[/red]")
        return 130
    except Exception as exc:  # user-facing message instead of a traceback
        if args.verbose:
            raise
        message = str(exc)
        if "ffmpeg" in message.lower():
            message += " — выполните: spotdl --download-ffmpeg"
        console.print(f"[red]Ошибка: {message}[/red]")
        return 1


__all__ = ["main", "run", "build_parser"]
