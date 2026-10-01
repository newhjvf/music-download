"""Spotify link (track / album / playlist) -> spotDL ``Song`` objects.

Without credentials spotDL's keyless client (SpotipyFree, spotDL PR #2626)
is used: no Premium needed, but only public playlists are visible. With
SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env the official Web API is
used instead (Spotify requires Premium for Dev Mode apps since Feb 2026).
"""

from __future__ import annotations

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


def songs_from_url(url: str, threads: int = 4) -> List[Song]:
    """Resolve the link with spotDL (``parse_query``) after ``init_spotify``."""
    from spotdl.utils.search import parse_query

    check_url(url)
    return parse_query([url.strip()], threads=max(1, threads))
