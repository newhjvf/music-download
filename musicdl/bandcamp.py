"""Bandcamp as an extra source: artists publish there themselves, so a match
is an official upload. Bandcamp has no public search API, so the public search
page is read. Playback/download goes through yt-dlp (a 128 kbps stream; higher
quality on Bandcamp needs a purchase or the e-mail "free download" form)."""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from spotdl.providers.audio.base import AudioProvider
from spotdl.types.result import Result

SEARCH_URL = "https://bandcamp.com/search"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:93.0) Gecko/20100101 Firefox/93.0"

_ITEM = re.compile(r'<li[^>]*class="[^"]*searchresult[^"]*"[^>]*>(.*?)</li>', re.S)
_HEADING = re.compile(r'<div class="heading">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SUBHEAD = re.compile(r'<div class="subhead">(.*?)</div>', re.S)
_LENGTH = re.compile(r'<div class="length">\s*length:\s*(\d+):(\d{2})', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", fragment)).split())


def parse_search(page: str) -> List[Dict[str, object]]:
    """Track hits of a Bandcamp search page: url, title, artist, album, duration."""
    hits: List[Dict[str, object]] = []
    for block in _ITEM.findall(page):
        heading = _HEADING.search(block)
        subhead = _SUBHEAD.search(block)
        if not heading or not subhead:
            continue
        url = html.unescape(heading.group(1))
        parts = urlsplit(url)
        if "/track/" not in parts.path:
            continue  # albums, artists and labels are not single tracks
        album, _, artist = (" " + _text(subhead.group(1))).rpartition(" by ")
        album = re.sub(r"^\s*from\s+", "", album).strip()
        if not artist.strip():
            continue
        length = _LENGTH.search(block)
        hits.append(
            {
                "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                "title": _text(heading.group(2)),
                "artist": artist.strip(),
                "album": album or None,
                "duration": int(length.group(1)) * 60 + int(length.group(2)) if length else 0,
            }
        )
    return hits


class Bandcamp(AudioProvider):
    """Searches bandcamp.com for tracks."""

    SUPPORTS_ISRC = False
    GET_RESULTS_OPTS: List[Dict[str, object]] = [{}]

    def get_results(self, search_term: str, *_args, **_kwargs) -> List[Result]:
        response = requests.get(
            SEARCH_URL,
            params={"q": search_term, "item_type": "t"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        results = []
        for hit in parse_search(response.text)[:15]:
            artist = str(hit["artist"])
            results.append(
                Result(
                    source=self.name,
                    url=str(hit["url"]),
                    verified=False,
                    name=str(hit["title"]),
                    duration=float(hit["duration"] or 0),  # type: ignore[arg-type]
                    author=artist,
                    artists=(artist,),
                    result_id=str(hit["url"]),
                    isrc_search=False,
                    search_query=search_term,
                    album=hit["album"],  # type: ignore[arg-type]
                )
            )
        return results
