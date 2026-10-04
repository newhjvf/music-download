"""Missing track data (album, year, cover, track number...) from Spotify.

CSV files and typed searches carry little more than artist and title, and
Spotify playlist listings sometimes lack the album. The tags of the finished
file should be complete, so such tracks are looked up on Spotify (no account
needed) and the gaps are filled."""

from __future__ import annotations

import dataclasses
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from spotdl.types.song import Song

logger = logging.getLogger(__name__)

Lookup = Callable[[Song], Optional[Dict[str, Any]]]

_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_SPOTIFY_TRACK = re.compile(r"open\.spotify\.com/track/[0-9A-Za-z]{22}")

# Fields taken from Spotify when they are empty in the song (all of them,
# with ``prefer_found``: CSV rows only carry neutral placeholders there).
_FILLABLE = (
    "album_name",
    "album_artist",
    "album_id",
    "year",
    "date",
    "track_number",
    "tracks_count",
    "disc_number",
    "cover_url",
    "isrc",
    "duration",
    "explicit",
    "album_type",
)
_PLACEHOLDER_VALUES = {"track_number": 1, "tracks_count": 1, "disc_number": 1}


def split_query(line: str) -> "tuple[str, str]":
    """'Artist - Title' -> (artist, title); a line without a dash -> ('', line)."""
    parts = _SEPARATOR.split(line.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", line.strip()


def needs_metadata(song: Song) -> bool:
    return not (song.album_name and song.cover_url and song.year)


def _norm(text: str) -> str:
    from spotdl.utils.formatter import slugify

    return slugify(text or "").replace("-", " ").strip()


def track_info(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Song fields from a raw Spotify track object (defensive: keyless
    responses may lack parts)."""
    album = raw.get("album") or {}
    images = sorted(album.get("images") or [], key=lambda image: image.get("width") or 0, reverse=True)
    date = album.get("release_date") or ""
    album_artists = album.get("artists") or []
    info: Dict[str, Any] = {
        "name": raw.get("name") or "",
        "artists": [a.get("name", "") for a in raw.get("artists") or [] if a.get("name")],
        "album_name": album.get("name") or "",
        "album_artist": album_artists[0].get("name") if album_artists else "",
        "album_id": album.get("id") or "",
        "album_type": album.get("album_type") or "",
        "date": date,
        "year": int(date[:4]) if date[:4].isdigit() else 0,
        "track_number": raw.get("track_number") or 0,
        "tracks_count": album.get("total_tracks") or 0,
        "disc_number": raw.get("disc_number") or 0,
        "cover_url": images[0].get("url") if images else "",
        "isrc": ((raw.get("external_ids") or {}).get("isrc") or "").upper() or None,
        "duration": int(round((raw.get("duration_ms") or 0) / 1000)),
        "explicit": raw.get("explicit"),
    }
    return info


def _best_hit(items: List[Dict[str, Any]], artist: str, title: str) -> Optional[Dict[str, Any]]:
    from rapidfuzz import fuzz

    wanted_title, wanted_artist = _norm(title), _norm(artist)
    best, best_score = None, 0.0
    for raw in items:
        got_title = _norm(raw.get("name", ""))
        title_score = max(fuzz.ratio(wanted_title, got_title), fuzz.token_set_ratio(wanted_title, got_title))
        if title_score < 80:
            continue
        artist_score = 100.0
        if wanted_artist:
            names = [_norm(a.get("name", "")) for a in raw.get("artists") or []]
            artist_score = max((fuzz.partial_ratio(wanted_artist, name) for name in names), default=0)
            if artist_score < 80:
                continue
        score = title_score + artist_score
        if score > best_score:
            best, best_score = raw, score
    return best


def _client():
    from spotdl.utils.spotify import SpotifyClient

    return SpotifyClient()


def find_on_spotify(song: Song) -> Optional[Dict[str, Any]]:
    """Track info from Spotify: by its link if the song has one, otherwise by
    searching 'artist title'. None when nothing convincing is found."""
    client = _client()
    if _SPOTIFY_TRACK.search(song.url or ""):
        raw = client.track(song.url)
        return track_info(raw) if raw else None
    result = client.search(f"{song.artist} {song.name}".strip(), type="track", limit=10) or {}
    hit = _best_hit((result.get("tracks") or {}).get("items") or [], song.artist, song.name)
    return track_info(hit) if hit else None


def find_free_text(text: str) -> Optional[Dict[str, Any]]:
    """Best Spotify track for a free-form query ('culture aarne'), or None."""
    from rapidfuzz import fuzz

    result = _client().search(text, type="track", limit=5) or {}
    for raw in (result.get("tracks") or {}).get("items") or []:
        info = track_info(raw)
        label = _norm(" ".join(info["artists"]) + " " + info["name"])
        if fuzz.token_set_ratio(_norm(text), label) >= 60:
            info["id"] = raw.get("id") or ""
            return info
    return None


def songs_from_text(
    text: str,
    lookup_text: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> "tuple[List[Song], List[str]]":
    """One track per line: 'Artist - Title', or just words to look up on
    Spotify. Returns (songs, lines that could not be understood)."""
    from musicdl.csv_import import TrackRow, row_to_song, split_artists

    lookup_text = lookup_text or find_free_text
    songs: List[Song] = []
    skipped: List[str] = []
    seen = set()
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        artist, title = split_query(line)
        if artist:
            artists = split_artists(artist) or [artist]
        else:
            try:
                info = lookup_text(title)
            except Exception as exc:
                logger.info("Spotify search failed for %r: %s", title, exc)
                info = None
            if not info:
                skipped.append(line)
                continue
            title, artists = info["name"], info["artists"]
        key = (artists[0].casefold(), title.casefold())
        if key in seen:
            continue
        seen.add(key)
        songs.append(row_to_song(TrackRow(line=number, title=title, artists=artists)))
    return songs, skipped


def fill_song(song: Song, info: Dict[str, Any], prefer_found: bool = False, canonical: bool = False) -> Song:
    """``song`` with its gaps filled from ``info`` (``canonical``: also take
    Spotify's spelling of title and artists)."""
    changes: Dict[str, Any] = {}
    for field in _FILLABLE:
        found = info.get(field)
        if found in (None, "", 0, []):
            continue
        current = getattr(song, field, None)
        placeholder = prefer_found and current == _PLACEHOLDER_VALUES.get(field, object())
        if current in (None, "", 0, []) or placeholder:
            changes[field] = found
    if canonical and info.get("name") and info.get("artists"):
        changes.update(name=info["name"], artists=list(info["artists"]), artist=info["artists"][0])
        if not song.album_artist or song.album_artist == song.artist:
            changes["album_artist"] = info.get("album_artist") or info["artists"][0]
    return dataclasses.replace(song, **changes) if changes else song


def enrich_songs(
    songs: List[Song],
    lookup: Optional[Lookup] = None,
    prefer_found: bool = False,
    canonical: bool = False,
    only_missing: bool = True,
    threads: int = 8,
) -> List[Song]:
    """Songs with gaps filled from Spotify, same order. A track that cannot be
    looked up (not on Spotify, no connection) is returned unchanged."""
    lookup = lookup or find_on_spotify

    def one(song: Song) -> Song:
        if only_missing and not needs_metadata(song):
            return song
        try:
            info = lookup(song)
        except Exception as exc:
            logger.info("No Spotify data for %s: %s", song.display_name, exc)
            return song
        if not info:
            logger.info("No Spotify data for %s: not found", song.display_name)
            return song
        return fill_song(song, info, prefer_found, canonical)

    if not songs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        return list(pool.map(one, songs))
