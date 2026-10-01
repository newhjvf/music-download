"""Search & match on YouTube Music using spotDL's provider and scoring.

Everything here delegates to spotDL (``YouTubeMusic.search`` and
``spotdl.utils.matching.order_results``). We only add two small things:

* ``order_results`` tolerance for unknown duration. TuneMyMusic CSV has no
  track length, and spotDL drops every result whose time match is < 25 %
  (with duration 0 that is *every* result). For songs with duration 0 we
  score each result as if its length matched, so ranking relies on
  title/artist/album/ISRC only.
* Capturing the chosen ``Result`` so we can show title/duration in
  ``--dry-run`` and reuse the URL for the download step.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from spotdl.providers.audio import base as _provider_base
from spotdl.providers.audio.base import AudioProvider
from spotdl.providers.audio.ytmusic import YouTubeMusic
from spotdl.types.result import Result
from spotdl.types.song import Song
from spotdl.utils.matching import order_results as _spotdl_order_results

logger = logging.getLogger(__name__)


def order_results(
    results: List[Result], song: Song, search_query: Optional[str] = None
) -> Dict[Result, float]:
    """spotDL's ``order_results`` that does not reject everything when the
    song duration is unknown (0/None)."""
    if song.duration:
        return _spotdl_order_results(results, song, search_query)

    scored: Dict[Result, float] = {}
    for result in results:
        assumed = dataclasses.replace(song, duration=int(round(result.duration or 0)))
        scored.update(_spotdl_order_results([result], assumed, search_query))
    return scored


def install_unknown_duration_patch() -> None:
    """Make spotDL's providers (and thus its Downloader) use ``order_results``
    above. Idempotent; songs with a known duration behave exactly as in spotDL."""
    _provider_base.order_results = order_results


install_unknown_duration_patch()


@dataclass
class MatchResult:
    song: Song
    url: Optional[str] = None
    title: str = ""
    author: str = ""
    duration: float = 0.0  # seconds
    verified: bool = False
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.url is not None


def find_match(provider: AudioProvider, song: Song, only_verified: bool = False) -> MatchResult:
    """Run spotDL's search for one song and return the chosen result.

    ``provider`` must not be shared between threads (we wrap its
    ``get_results`` for the duration of the call).
    """
    seen: Dict[str, Result] = {}
    original = provider.get_results

    def recording_get_results(search_term: str, *args, **kwargs) -> List[Result]:
        results = original(search_term, *args, **kwargs)
        for result in results:
            seen.setdefault(result.url, result)
        return results

    provider.get_results = recording_get_results  # type: ignore[method-assign]
    try:
        url = provider.search(song, only_verified)
    except Exception as exc:  # network errors, YouTube blocks, ytmusicapi changes
        logger.debug("Search failed for %s: %s", song.display_name, exc, exc_info=True)
        return MatchResult(song=song, error=f"{type(exc).__name__}: {exc}")
    finally:
        del provider.get_results  # restore the class method

    if not url:
        return MatchResult(song=song, error="not found")

    result = seen.get(url)
    if result is None:
        return MatchResult(song=song, url=url)
    return MatchResult(
        song=song,
        url=url,
        title=result.name,
        author=", ".join(result.artists) if result.artists else result.author,
        duration=result.duration,
        verified=result.verified,
    )


def default_provider_factory() -> AudioProvider:
    return YouTubeMusic(output_format="mp3")


def find_matches(
    songs: List[Song],
    threads: int = 4,
    provider_factory: Callable[[], AudioProvider] = default_provider_factory,
    only_verified: bool = False,
    on_result: Optional[Callable[[MatchResult], None]] = None,
) -> List[MatchResult]:
    """Search all songs in parallel (one provider per worker thread).
    Results keep the input order."""
    local = threading.local()

    def work(song: Song) -> MatchResult:
        provider = getattr(local, "provider", None)
        if provider is None:
            provider = local.provider = provider_factory()
        match = find_match(provider, song, only_verified)
        if on_result:
            on_result(match)
        return match

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        return list(pool.map(work, songs))


def youtube_cover_url(url: str) -> Optional[str]:
    """Thumbnail of a YouTube video, used as cover art when the source
    (CSV) has none."""
    if "watch?v=" not in url:
        return None
    video_id = url.split("watch?v=", 1)[1].split("&", 1)[0]
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
