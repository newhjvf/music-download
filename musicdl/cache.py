"""Remembers found matches between runs, so «Скачать» after «Проверить»
(or re-checking the same playlist) does not search again.

Only successful matches are stored; tracks that were not found are searched
again every time. Entries expire after ``MAX_AGE_DAYS``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from spotdl.types.song import Song

from musicdl.matching import MatchResult

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 30
VERSION = 2  # bump when matching rules change: old matches are searched again


def song_key(song: Song, only_verified: bool) -> str:
    """Spotify URL / CSV id plus the search mode (official-only searches a
    smaller set of sources, so its results are kept apart)."""
    return f"{song.url}|{'verified' if only_verified else 'all'}"


class MatchCache:
    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.entries: Dict[str, dict] = {}
        if self.path and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") == VERSION:
                    cutoff = time.time() - MAX_AGE_DAYS * 86400
                    self.entries = {k: v for k, v in data.get("entries", {}).items() if v.get("time", 0) >= cutoff}
            except (OSError, ValueError, AttributeError):
                logger.warning("Ignoring unreadable match cache %s", self.path)

    def get(self, song: Song, only_verified: bool) -> Optional[MatchResult]:
        entry = self.entries.get(song_key(song, only_verified))
        if not entry:
            return None
        return MatchResult(
            song=song,
            url=entry["url"],
            title=entry.get("title", ""),
            author=entry.get("author", ""),
            duration=entry.get("duration", 0.0),
            verified=entry.get("verified", False),
            source=entry.get("source", ""),
        )

    def put(self, match: MatchResult, only_verified: bool) -> None:
        if not match.found:
            return
        self.entries[song_key(match.song, only_verified)] = {
            "url": match.url,
            "title": match.title,
            "author": match.author,
            "duration": match.duration,
            "verified": match.verified,
            "source": match.source,
            "time": time.time(),
        }

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": VERSION, "entries": self.entries}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            logger.warning("Could not save match cache %s", self.path, exc_info=True)
