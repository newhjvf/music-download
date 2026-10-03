"""A pretend engine for trying the window without a network, spotDL or ffmpeg:

    python -m musicdl.webui --demo

It produces the same callbacks as ``musicdl.job.run_job`` (several worker
threads, cache hits, not-found tracks, a lost connection, a retried download),
so it is also what the tests and screenshots of the design run on.
"""

from __future__ import annotations

import csv
import random
import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist)/[0-9A-Za-z]{22}(?:[/?#].*)?$"
)

CATALOG: List[Tuple[str, str, int]] = [
    ("Bohemian Rhapsody", "Queen", 355), ("Smells Like Teen Spirit", "Nirvana", 301),
    ("Кукушка", "Кино", 386), ("Under Pressure", "Queen, David Bowie", 248),
    ("Blinding Lights", "The Weeknd", 200), ("Billie Jean", "Michael Jackson", 294),
    ("Дальний свет", "Мумий Тролль", 241), ("Creep", "Radiohead", 238),
    ("Hotel California", "Eagles", 391), ("Lose Yourself", "Eminem", 326),
    ("Группа крови", "Кино", 285), ("Take Five", "The Dave Brubeck Quartet", 324),
    ("Numb", "Linkin Park", 187), ("Seven Nation Army", "The White Stripes", 232),
    ("Закрой за мной дверь", "Чайф", 267), ("Believer", "Imagine Dragons", 204),
    ("Get Lucky", "Daft Punk, Pharrell Williams", 369), ("Wonderwall", "Oasis", 258),
    ("Звезда по имени Солнце", "Кино", 225), ("Sweet Child O' Mine", "Guns N' Roses", 356),
    ("Rolling in the Deep", "Adele", 228), ("Shape of You", "Ed Sheeran", 234),
    ("Мой рок-н-ролл", "Би-2", 255), ("Smooth Operator", "Sade", 258),
    ("Back in Black", "AC/DC", 255), ("Mr. Brightside", "The Killers", 223),
    ("Нервы", "Мияги", 190), ("Heroes", "David Bowie", 372),
    ("Another Brick in the Wall, Pt. 2", "Pink Floyd", 239), ("Stairway to Heaven", "Led Zeppelin", 482),
]


def _roll(key: str) -> float:
    """Stable pseudo-random number in [0, 1) per track, so demo runs look the same every time."""
    return random.Random(zlib.crc32(key.encode())).random()


@dataclass
class DemoSong:
    artist: str
    name: str
    duration: float
    url: str


@dataclass
class DemoMatch:
    song: DemoSong
    url: Optional[str] = None
    title: str = ""
    author: str = ""
    duration: float = 0.0
    verified: bool = False
    error: Optional[str] = None
    source: str = ""

    @property
    def found(self) -> bool:
        return self.url is not None


@dataclass
class DemoSummary:
    total: int = 0
    existing: int = 0
    processed: int = 0
    failed: List[Any] = field(default_factory=list)
    report: Optional[Path] = None
    cancelled: int = 0

    @property
    def succeeded(self) -> int:
        return self.processed - len(self.failed) - self.cancelled


class DemoUserError(Exception):
    pass


def write_demo_csv(path: Path, count: int = 90) -> Path:
    """A sample TuneMyMusic-style CSV for the file picker of the demo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Track name", "Artist name", "Album", "Playlist name", "Type", "ISRC", "Spotify - id"])
        for i in range(count):
            name, artist, _ = CATALOG[i % len(CATALOG)]
            suffix = "" if i < len(CATALOG) else f" (Live {2000 + i})"
            writer.writerow([name + suffix, artist, "", "Demo", "Playlist", "", f"{rng.getrandbits(64):016x}"])
    return path


class DemoEngine:
    name = "demo"

    def __init__(self, speed: float = 1.0, offline_blip: bool = True) -> None:
        self.speed = max(0.01, speed)
        self.offline_blip = offline_blip

    # -- small questions the session asks ------------------------------------
    def is_user_error(self, exc: BaseException) -> bool:
        return isinstance(exc, DemoUserError)

    def is_cancelled(self, match: Any) -> bool:
        return match.error == "cancelled"

    def is_not_found(self, match: Any) -> bool:
        return match.error == "not found"

    def existing_name(self, song: Any, out_dir: str) -> str:
        return f"{song.artist} - {song.name}.mp3"

    def check_link(self, link: str) -> Optional[str]:
        match = SPOTIFY_URL.match(link.strip())
        return match.group(1) if match else None

    def version(self) -> str:
        return "демо"

    # -- the pretend job ------------------------------------------------------
    def _sleep(self, seconds: float, cancel: Any = None) -> None:
        end = time.monotonic() + seconds / self.speed
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                return
            time.sleep(min(0.02, max(0.0, end - time.monotonic())))

    def _load(self, options: Dict[str, Any]) -> List[DemoSong]:
        rng = random.Random(1)
        songs: List[DemoSong] = []
        if options["source_kind"] == "csv":
            path = Path(options["source"])
            if not path.is_file():
                raise DemoUserError(f"Файл не найден: {path}")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for number, row in enumerate(csv.DictReader(handle)):
                    name, artist = (row.get("Track name") or "").strip(), (row.get("Artist name") or "").strip()
                    if name:
                        songs.append(DemoSong(artist, name, 200 + rng.randint(-40, 80), f"csv:{number}"))
            if not songs:
                raise DemoUserError("Во входных данных нет ни одного трека")
        else:
            for number in range(24):
                name, artist, duration = CATALOG[number % len(CATALOG)]
                songs.append(DemoSong(artist, name, duration, f"spotify:{number}"))
        return songs

    def run(self, options: Dict[str, Any], cancel: Any, handlers: Dict[str, Callable]) -> DemoSummary:
        rng = random.Random(42)
        info = handlers["info"]
        self._sleep(0.6, cancel)
        songs = self._load(options)
        out_dir = Path(options["out_dir"])
        existing = [song for number, song in enumerate(songs) if number % 17 == 5]
        todo = [song for song in songs if song not in existing]
        summary = DemoSummary(total=len(songs), existing=len(existing), processed=len(todo))
        handlers["songs_loaded"](todo, existing)
        info(f"Треков: {len(songs)}, уже скачано: {len(existing)}, к обработке: {len(todo)}")

        handlers["phase"]("search", len(todo))
        cached = todo[: len(todo) // 5]
        matches: Dict[str, DemoMatch] = {}
        for song in cached:
            match = self._found(song, rng, "YouTube Music", True)
            matches[song.url] = match
            handlers["matched"](match)
        info(f"Уже найдены раньше: {len(cached)}, ищу остальные: {len(todo) - len(cached)}…")

        lock = threading.Lock()
        blip_at = len(todo) // 2 if self.offline_blip and len(todo) > 8 else -1
        progress = {"searched": 0}
        sources = ["YouTube Music", "YouTube", "SoundCloud"]

        def search(song: DemoSong) -> None:
            if cancel.is_set():
                match = DemoMatch(song, error="cancelled")
            else:
                roll = _roll(song.url)
                handlers["searching"](song.url, sources[0])
                self._sleep(0.25 + roll * 0.5, cancel)
                if roll > 0.9:
                    handlers["searching"](song.url, sources[1])
                    self._sleep(0.3, cancel)
                if cancel.is_set():
                    match = DemoMatch(song, error="cancelled")
                elif roll > 0.95:
                    match = DemoMatch(song, error="not found")
                elif roll < 0.02:
                    match = DemoMatch(song, error="ConnectionError: Max retries exceeded with url")
                else:
                    source = sources[0] if roll < 0.7 else sources[1] if roll < 0.93 else sources[2]
                    match = self._found(song, rng, source, source == "YouTube Music" and roll < 0.55)
            matches[song.url] = match
            handlers["matched"](match)
            with lock:
                progress["searched"] += 1
                if progress["searched"] == blip_at:
                    handlers["connection"](False)
                    self._sleep(2.5)
                    handlers["connection"](True)

        pending = [song for song in todo if song.url not in matches]
        with ThreadPoolExecutor(max_workers=max(1, min(8, int(options["threads"]) * 2))) as pool:
            list(pool.map(search, pending))

        ordered = [matches[song.url] for song in todo]
        summary.cancelled = sum(1 for m in ordered if m.error == "cancelled")
        summary.failed = [(m.song, m.error or "not found") for m in ordered if not m.found and m.error != "cancelled"]

        if not options["dry_run"] and not cancel.is_set():
            to_download = [m for m in ordered if m.found]
            if to_download:
                info("ffmpeg найден")
                info(f"Скачиваю {len(to_download)} трек(ов)…")
                handlers["phase"]("download", len(to_download))
                self._download(to_download, options, handlers, summary, cancel)

        if summary.failed:
            summary.report = out_dir / "not_found.csv"
        return summary

    def _found(self, song: DemoSong, rng: random.Random, source: str, verified: bool) -> DemoMatch:
        return DemoMatch(
            song,
            url=f"https://music.youtube.com/watch?v={zlib.crc32(song.url.encode()) * 7919 % 10**11:011d}",
            title=song.name,
            author=song.artist.split(",")[0],
            duration=song.duration + rng.choice((-2, 0, 0, 1, 3)),
            verified=verified,
            source=source,
        )

    def _download(self, matches: List[DemoMatch], options: Dict[str, Any], handlers: Dict[str, Callable], summary: DemoSummary, cancel: Any) -> None:
        status = handlers["download_status"]

        def one(match: DemoMatch) -> None:
            key = match.song.url
            roll = _roll(key)
            fails_once = roll < 0.06
            for attempt in range(2):
                status(key, "Searching", 0)
                self._sleep(0.15)
                status(key, "Getting metadata", 0)
                self._sleep(0.2)
                for percent in range(0, 101, 8):
                    status(key, "Downloading", percent)
                    self._sleep(0.07 + roll * 0.08)
                if fails_once and attempt == 0:
                    status(key, "Error", 0)
                    self._sleep(0.8)
                    status(key, "Retry", 0)
                    continue
                status(key, "Converting", 100)
                self._sleep(0.35)
                status(key, "Embedding metadata", 100)
                self._sleep(0.15)
                status(key, "Done", 100)
                return

        with ThreadPoolExecutor(max_workers=max(1, int(options["threads"]))) as pool:
            list(pool.map(one, matches))
