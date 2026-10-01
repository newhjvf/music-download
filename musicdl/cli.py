"""Command line: ``musicdl csv FILE`` / ``musicdl url SPOTIFY_LINK``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from rich.console import Console
from rich.table import Table

from musicdl import __version__
from musicdl.matching import MatchResult, default_provider_factory, find_matches
from musicdl.pipeline import (
    download,
    expected_path,
    format_duration,
    prepare_for_download,
    split_existing,
    write_report,
)

console = Console()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--out", type=Path, default=Path("music"), help="папка для mp3 (по умолчанию ./music)")
    common.add_argument("-t", "--threads", type=int, default=4, help="параллельных загрузок (по умолчанию 4)")
    common.add_argument("--dry-run", action="store_true", help="только поиск и сопоставление, без скачивания")
    common.add_argument("--bitrate", default="320k", help="битрейт mp3, например 192k или 320k (по умолчанию 320k)")
    common.add_argument("--report", type=Path, help="куда писать not_found.csv (по умолчанию в папку --out)")
    common.add_argument("--only-verified", action="store_true", help="брать только официальные треки YouTube Music")
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


def load_songs(args: argparse.Namespace):
    if args.mode == "csv":
        from musicdl.csv_import import songs_from_csv

        if not args.file.is_file():
            raise SystemExit(f"Файл не найден: {args.file}")
        return songs_from_csv(args.file)

    from musicdl.spotify_input import check_url, init_spotify, load_credentials, songs_from_url

    check_url(args.link)
    official = init_spotify(*load_credentials(args.env))
    console.print(
        "Spotify: " + ("официальный API (ключи из .env)" if official else "без ключей (spotDL / SpotipyFree)")
    )
    return songs_from_url(args.link, args.threads)


def match_table(matches: List[MatchResult]) -> Table:
    table = Table(title="Сопоставление", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Трек")
    table.add_column("Найдено на YouTube Music")
    table.add_column("Длит.", justify="right")
    table.add_column("Ссылка")
    for index, match in enumerate(matches, 1):
        track = f"{match.song.artist} - {match.song.name}"
        if match.found:
            found = f"{match.author} - {match.title}" if match.title else "?"
            if not match.verified:
                found += " [dim](видео)[/dim]"
            table.add_row(str(index), track, found, format_duration(match.duration), match.url)
        else:
            table.add_row(str(index), track, f"[red]{match.error}[/red]", "", "")
    return table


def run(args: argparse.Namespace, provider_factory: Callable = default_provider_factory) -> int:
    songs = load_songs(args)
    if not songs:
        console.print("[yellow]Треков не найдено во входных данных.[/yellow]")
        return 1

    out_dir: Path = args.out
    report_path: Path = args.report or out_dir / "not_found.csv"
    todo, existing = split_existing(songs, out_dir)
    console.print(f"Треков: {len(songs)}, уже скачано: {len(existing)}, к обработке: {len(todo)}")

    with console.status("Поиск на YouTube Music…"):
        matches = find_matches(todo, args.threads, provider_factory, args.only_verified)

    failed: List[Tuple] = [(m.song, m.error or "not found") for m in matches if not m.found]

    if args.dry_run:
        console.print(match_table(matches))
    else:
        results = download(prepare_for_download(matches), out_dir, args.threads, args.bitrate)
        for song, path in results:
            if path is None or not Path(path).exists():
                failed.append((song, "download failed"))

    report = write_report(report_path, failed)
    found = len(todo) - len(failed)
    verb = "найдено" if args.dry_run else "скачано"
    console.print(f"Готово: {verb} {found} из {len(todo)}, пропущено (уже есть): {len(existing)}.")
    if report:
        console.print(f"[yellow]Не удалось: {len(failed)} — список в {report}[/yellow]")
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


__all__ = ["main", "run", "build_parser", "expected_path"]
