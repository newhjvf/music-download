"""TuneMyMusic CSV export -> spotDL ``Song`` objects (no Spotify API involved)."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from spotdl.types.song import Song

# Normalized header (lowercase, letters/digits only) -> field name.
# TuneMyMusic exports: "Track name", "Artist name", "Album", "Playlist name",
# "Type", "ISRC", "Spotify - id". Other aliases make the parser tolerant to
# small format changes and hand-edited files.
HEADER_ALIASES: Dict[str, str] = {
    "trackname": "title",
    "track": "title",
    "title": "title",
    "name": "title",
    "songname": "title",
    "song": "title",
    "artistname": "artist",
    "artist": "artist",
    "artists": "artist",
    "artistnames": "artist",
    "album": "album",
    "albumname": "album",
    "isrc": "isrc",
    "spotifyid": "spotify_id",
    "spotifytrackid": "spotify_id",
    "duration": "duration",
    "durationms": "duration",
    "durations": "duration",
    "length": "duration",
    "playlistname": "playlist",
}

ARTIST_SEPARATOR = re.compile(r"\s*[,;]\s*")
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


class CsvFormatError(ValueError):
    """The CSV file does not contain the required columns."""


@dataclass(frozen=True)
class TrackRow:
    """One track from the CSV file."""

    line: int
    title: str
    artists: List[str]
    album: str = ""
    isrc: Optional[str] = None
    spotify_id: Optional[str] = None
    duration: int = 0  # seconds, 0 = unknown
    playlist: str = ""

    @property
    def artist(self) -> str:
        return self.artists[0]


def _normalize_header(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.strip().lower())


def parse_duration(value: str) -> int:
    """'3:45' / '1:02:03' / '225' (seconds) / '225000' (ms) -> seconds. Unknown -> 0."""
    value = (value or "").strip()
    if not value:
        return 0
    if ":" in value:
        try:
            parts = [int(float(p)) for p in value.split(":")]
        except ValueError:
            return 0
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return 0
    # Track lengths in seconds never reach 10000; larger numbers are milliseconds.
    return int(round(number / 1000)) if number >= 10000 else int(round(number))


def split_artists(value: str) -> List[str]:
    return [a for a in ARTIST_SEPARATOR.split(value.strip()) if a]


def _detect_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel  # type: ignore[return-value]


def parse_csv(path: Path) -> List[TrackRow]:
    """Read a TuneMyMusic CSV export. Rows without title or artist are skipped,
    exact duplicates (same artist + title) are dropped."""
    path = Path(path)
    # utf-8-sig strips the BOM that Excel/TuneMyMusic may add.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        reader = csv.reader(handle, _detect_dialect(sample))
        try:
            header = next(reader)
        except StopIteration:
            return []

        columns: Dict[str, int] = {}
        for index, name in enumerate(header):
            field = HEADER_ALIASES.get(_normalize_header(name))
            if field and field not in columns:
                columns[field] = index

        missing = {"title", "artist"} - columns.keys()
        if missing:
            raise CsvFormatError(
                f"{path.name}: no column for {', '.join(sorted(missing))}; "
                f"found columns: {', '.join(header)}"
            )

        def cell(row: List[str], field: str) -> str:
            index = columns.get(field)
            if index is None or index >= len(row):
                return ""
            return row[index].strip()

        tracks: List[TrackRow] = []
        seen = set()
        for line, row in enumerate(reader, start=2):
            title = cell(row, "title")
            artists = split_artists(cell(row, "artist"))
            if not title or not artists:
                continue

            key = (artists[0].casefold(), title.casefold())
            if key in seen:
                continue
            seen.add(key)

            spotify_id = cell(row, "spotify_id")
            tracks.append(
                TrackRow(
                    line=line,
                    title=title,
                    artists=artists,
                    album=cell(row, "album"),
                    isrc=cell(row, "isrc").upper() or None,
                    spotify_id=spotify_id if SPOTIFY_ID.match(spotify_id) else None,
                    duration=parse_duration(cell(row, "duration")),
                    playlist=cell(row, "playlist"),
                )
            )
    return tracks


def row_to_song(row: TrackRow) -> Song:
    """Build a spotDL ``Song`` from CSV data only.

    All fields that would make spotDL's Downloader call ``reinit_song`` (i.e.
    query Spotify) are filled with neutral values, so the CSV mode works
    without any Spotify access.
    """
    if row.spotify_id:
        song_id = row.spotify_id
        url = f"https://open.spotify.com/track/{row.spotify_id}"
    else:
        digest = hashlib.sha1(f"{row.artist}\0{row.title}".encode("utf-8")).hexdigest()
        song_id = f"csv-{digest[:16]}"
        url = f"https://musicdl.local/csv/{song_id}"

    return Song.from_missing_data(
        name=row.title,
        artists=list(row.artists),
        artist=row.artist,
        album_name=row.album or None,
        album_artist=row.artist,
        album_id="",
        genres=[],
        disc_number=1,
        disc_count=1,
        track_number=1,
        tracks_count=1,
        duration=row.duration,
        year=0,
        date="",
        explicit=None,
        publisher="",
        song_id=song_id,
        url=url,
        isrc=row.isrc,
        cover_url=None,
        copyright_text=None,
        list_name=row.playlist or None,
    )


def songs_from_csv(path: Path) -> List[Song]:
    return [row_to_song(row) for row in parse_csv(path)]
