"""Spotify link (track / album / playlist) -> spotDL ``Song`` objects.

Without credentials spotDL's keyless client (SpotipyFree, spotDL PR #2626)
is used: no Premium needed, but only public playlists are visible. With
SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env the official Web API is
used instead (Spotify requires Premium for Dev Mode apps since Feb 2026).
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from spotdl.types.song import Song

SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist)/[0-9A-Za-z]{22}(?:[/?#].*)?$"
)


class SpotifyInputError(ValueError):
    pass


def check_url(url: str) -> str:
    """-> 'track' | 'album' | 'playlist'."""
    match = SPOTIFY_URL.match(url.strip())
    if not match:
        raise SpotifyInputError(
            "Expected a Spotify track, album or playlist link, e.g. "
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        )
    return match.group(1)


def load_credentials(env_file: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    from dotenv import load_dotenv

    load_dotenv(env_file or Path.cwd() / ".env", override=False)
    client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip() or None
    client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip() or None
    if bool(client_id) != bool(client_secret):
        raise SpotifyInputError("Set both SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env, or neither")
    return client_id, client_secret


def init_spotify(client_id: Optional[str], client_secret: Optional[str]) -> bool:
    """Initialize spotDL's Spotify client. Returns True if the official API is used."""
    from spotdl.utils.config import DEFAULT_CONFIG
    from spotdl.utils.spotify import SpotifyClient

    if SpotifyClient._instance is not None:  # second run in the same window
        return SpotifyClient.is_using_official_api()

    official = bool(client_id and client_secret)
    SpotifyClient.init(
        client_id=client_id or DEFAULT_CONFIG["client_id"],
        client_secret=client_secret or DEFAULT_CONFIG["client_secret"],
        use_official_api=official,
        no_cache=True,
    )
    return official


# Fields that make spotDL's Downloader re-query Spotify for every song
# (``reinit_song``) when they are None. Playlist/album listings already carry
# everything needed for matching and tagging except genre/publisher.
_NEUTRAL_DEFAULTS = {
    "genres": [],
    "disc_number": 1,
    "disc_count": 1,
    "track_number": 1,
    "tracks_count": 1,
    "album_id": "",
    "publisher": "",
    "date": "",
}


def complete_song(song: Song) -> Song:
    """Fill fields missing from a playlist/album listing with neutral values so
    no extra per-track Spotify request is made."""
    changes = {key: value for key, value in _NEUTRAL_DEFAULTS.items() if getattr(song, key) is None}
    if song.album_artist is None:
        changes["album_artist"] = song.artist
    if song.disc_count is None and song.disc_number:
        changes["disc_count"] = song.disc_number
    return dataclasses.replace(song, **changes) if changes else song


def songs_from_url(url: str, threads: int = 4) -> List[Song]:
    """Resolve the link with spotDL after ``init_spotify``.

    Uses ``get_simple_songs`` (one request per playlist/album page) instead of
    ``parse_query``, which re-fetches every track separately and takes minutes
    on large playlists.
    """
    from spotdl.utils.search import get_simple_songs

    check_url(url)
    return [complete_song(song) for song in get_simple_songs([url.strip()])]
