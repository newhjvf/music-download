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
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from spotdl.providers.audio import base as _provider_base
from spotdl.providers.audio.base import AudioProvider
from spotdl.providers.audio.ytmusic import YouTubeMusic
from spotdl.types.result import Result
from spotdl.types.song import Song
from spotdl.utils.matching import order_results as _spotdl_order_results

if False:  # typing only
    from musicdl.network import ConnectionGuard

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


CANCELLED = "cancelled"
SOURCE_NAMES = {
    "YouTubeMusic": "YouTube Music",
    "YouTube": "YouTube",
    "FastYouTube": "YouTube",
    "SoundCloud": "SoundCloud",
}


@dataclass
class MatchResult:
    song: Song
    url: Optional[str] = None
    title: str = ""
    author: str = ""
    duration: float = 0.0  # seconds
    verified: bool = False
    error: Optional[str] = None
    source: str = ""  # "YouTube Music" / "YouTube" / "SoundCloud"
    network_error: bool = False  # failed because the connection was (probably) lost

    @property
    def found(self) -> bool:
        return self.url is not None


def short_error(exc: BaseException, limit: int = 80) -> str:
    text = " ".join(str(exc).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|при уч\.?)\s+.*$", re.IGNORECASE)
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+.*$")


def clean_title(title: str) -> str:
    """'Song (feat. X) [Remastered] - Live' -> 'Song'."""
    cleaned = _FEAT.sub("", _BRACKETS.sub("", title))
    cleaned = _DASH_SUFFIX.sub("", cleaned).strip()
    return cleaned or title.strip()


def query_variants(song: Song) -> List[Song]:
    """The song itself, then (if different) the song with a cleaned-up title."""
    variants = [song]
    cleaned = clean_title(song.name)
    if cleaned.casefold() != song.name.strip().casefold():
        variants.append(dataclasses.replace(song, name=cleaned))
    return variants


# Versions that are not the original track. A result containing one of these
# is dropped unless the song title itself contains it (a real remix etc.).
NOT_ORIGINAL = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"type\s*beat",
        r"free\s*for\s*profit",
        r"\bбит\b",
        r"\bbeat\b",
        r"\bинструментал\w*",
        r"\binstrumental\b",
        r"\bминус\b",
        r"\bкараоке\b",
        r"\bkaraoke\b",
        r"\bкавер\b",
        r"\bcover\b",
        r"\blive\b",
        r"\bлайв\b",
        r"\bконцерт\w*",
        r"\bconcert\b",
        r"\bremix\b",
        r"\bремикс\b",
        r"\bmashup\b",
        r"\bмэшап\b",
        r"\bslowed\b",
        r"\bsped\s*up\b",
        r"\bspeed\s*up\b",
        r"\bnightcore\b",
        r"\breverb\b",
        r"bass\s*boost",
        r"\b8d\b",
        r"\bacoustic\b",
        r"\bакустик\w*",
        r"\bdiss\b",
        r"\bдисс\b",
        r"\breaction\b",
        r"\bреакци\w*",
        r"\btutorial\b",
        r"\bразбор\b",
    )
]


def not_original_words(song: Song, result: Result) -> List[str]:
    """Markers like 'type beat', 'кавер', 'live' found in the result title but
    not in the song title."""
    wanted = song.name or ""
    got = result.name or ""
    return [p.pattern for p in NOT_ORIGINAL if p.search(got) and not p.search(wanted)]


def acceptable(song: Song, result: Result) -> bool:
    """Result can be the original song: title matches and no 'not original'
    markers (beats, covers, live, remixes, diss tracks...)."""
    return title_matches(song, result) and not not_original_words(song, result)


def title_matches(song: Song, result: Result, threshold: float = 70.0) -> bool:
    """Sanity check on top of spotDL's scoring: the result title must actually
    contain the song title (transliterated, so Cyrillic/Latin both work).
    Guards against e.g. a lone ISRC hit that is a different song."""
    from rapidfuzz import fuzz
    from spotdl.utils.formatter import slugify

    wanted = slugify(clean_title(song.name)).replace("-", " ")
    got = slugify(result.name or "").replace("-", " ")
    if not wanted or not got:
        return True
    return max(fuzz.partial_ratio(wanted, got), fuzz.token_set_ratio(wanted, got)) >= threshold


def _search_once(
    provider: AudioProvider, song: Song, only_verified: bool
) -> Tuple[Optional[str], Optional[Result]]:
    """spotDL's ``provider.search`` plus the chosen ``Result``, without the
    slow per-result view-count lookups."""
    seen: Dict[str, Result] = {}
    original_get_results = provider.get_results

    def recording_get_results(search_term: str, *args, **kwargs) -> List[Result]:
        results = original_get_results(search_term, *args, **kwargs)
        kept = []
        for result in results:
            seen.setdefault(result.url, result)
            if acceptable(song, result):
                kept.append(result)
            else:
                logger.debug("Dropped %r for %s", result.name, song.display_name)
        return kept

    def no_view_lookup(url: str) -> int:
        # spotDL fetches the view count of up to 8 near-equal results with a
        # full yt-dlp request each, only as a tie-breaker. That was most of
        # the search time (and YouTube often refuses it), so results without
        # a known view count simply count as 0 and keep their match score order.
        return 0

    provider.get_results = recording_get_results  # type: ignore[method-assign]
    provider.get_views = no_view_lookup  # type: ignore[method-assign]
    try:
        url = provider.search(song, only_verified)
    finally:
        del provider.get_results  # restore the class methods
        del provider.get_views
    return url, (seen.get(url) if url else None)


def find_match(
    providers: Union[AudioProvider, Sequence[AudioProvider]],
    song: Song,
    only_verified: bool = False,
    on_try: Optional[Callable[[str], None]] = None,
) -> MatchResult:
    """Try each provider (YouTube Music, then YouTube, then SoundCloud) and
    each title variant until a result passes spotDL's scoring and our title
    check. Providers must not be shared between threads."""
    if isinstance(providers, AudioProvider):
        providers = [providers]

    from musicdl.network import looks_like_network_error

    error: Optional[str] = None
    network_error = False
    answered = False  # at least one source searched without failing
    for provider in providers:
        source = SOURCE_NAMES.get(provider.name, provider.name)
        if on_try:
            on_try(source)
        for variant in query_variants(song):
            try:
                url, result = _search_once(provider, variant, only_verified)
            except Exception as exc:  # network errors, YouTube blocks, API changes
                logger.debug("%s search failed for %s: %s", source, song.display_name, exc, exc_info=True)
                error = short_error(exc)
                network_error = network_error or looks_like_network_error(exc)
                break  # this provider is unusable for this song; try the next one
            answered = True
            if not url:
                continue
            if result is None:
                return MatchResult(song=song, url=url, source=source)
            if not acceptable(song, result):
                logger.info("Rejected %s for %s: title %r", url, song.display_name, result.name)
                continue
            return MatchResult(
                song=song,
                url=url,
                title=result.name,
                author=", ".join(result.artists) if result.artists else result.author,
                duration=result.duration,
                verified=result.verified,
                source=source,
            )
    if answered and not network_error:
        error = None  # a source searched fine and had nothing: plain "not found"
    return MatchResult(song=song, error=error or "not found", network_error=network_error)


class ProviderChain:
    """Providers created on first use, so a source that cannot start (e.g.
    SoundCloud unreachable) is skipped instead of breaking the search."""

    def __init__(self, factories: Sequence[Callable[[], AudioProvider]]):
        self._factories = list(factories)
        self._created: Dict[int, Optional[AudioProvider]] = {}

    def __iter__(self):
        for index, factory in enumerate(self._factories):
            if index not in self._created:
                try:
                    self._created[index] = factory()
                except Exception as exc:
                    logger.warning("Audio source unavailable: %s", short_error(exc))
                    self._created[index] = None
            provider = self._created[index]
            if provider is not None:
                yield provider


def _fast_youtube_class():
    from spotdl.providers.audio.youtube import YouTube
    from yt_dlp import YoutubeDL

    class FastYouTube(YouTube):
        """spotDL's YouTube provider, but the search reads only the result
        list (``extract_flat``) instead of opening all 10 videos: much faster,
        and YouTube does not answer it with "Sign in to confirm you're not a bot"."""

        def get_results(self, search_term: str, *_args, **_kwargs) -> List[Result]:
            options = {**self.audio_handler.params, "skip_download": True, "extract_flat": "in_playlist"}
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"ytsearch10:{search_term}", download=False)
            results = []
            for entry in (info or {}).get("entries") or []:
                if not entry or not entry.get("id"):
                    continue
                results.append(
                    Result(
                        source=self.name,
                        url=f"https://www.youtube.com/watch?v={entry['id']}",
                        verified=False,
                        name=entry.get("title") or "",
                        duration=entry.get("duration") or 0,
                        author=entry.get("channel") or entry.get("uploader") or "",
                        search_query=search_term,
                        views=entry.get("view_count") or 0,
                        result_id=entry["id"],
                    )
                )
            return results

    return FastYouTube


def default_provider_factory(only_verified: bool = False) -> ProviderChain:
    from spotdl.providers.audio.soundcloud import SoundCloud

    YouTube = _fast_youtube_class()

    factories: List[Callable[[], AudioProvider]] = [lambda: YouTubeMusic(output_format="mp3")]
    if not only_verified:  # "official tracks only" = YouTube Music songs only
        factories += [lambda: YouTube(output_format="mp3"), lambda: SoundCloud(output_format="mp3")]
    return ProviderChain(factories)


def find_matches(
    songs: List[Song],
    threads: int = 4,
    provider_factory: Callable[..., Any] = default_provider_factory,
    only_verified: bool = False,
    on_result: Optional[Callable[[MatchResult], None]] = None,
    on_try: Optional[Callable[[Song, str], None]] = None,
    cancel: Optional[threading.Event] = None,
    guard: Optional["ConnectionGuard"] = None,
    network_retries: int = 5,
) -> List[MatchResult]:
    """Search all songs in parallel (own providers per worker thread).
    Results keep the input order. ``on_try(song, source)`` is called when a
    source starts being searched for a song; once ``cancel`` is set the
    remaining songs are returned with ``error="cancelled"``."""
    local = threading.local()

    def make_providers():
        if provider_factory is default_provider_factory:
            return provider_factory(only_verified)
        return provider_factory()

    def work(song: Song) -> MatchResult:
        if cancel is not None and cancel.is_set():
            return MatchResult(song=song, error=CANCELLED)
        if guard is not None and guard.offline and not guard.wait_until_online():
            return MatchResult(song=song, error=CANCELLED)
        providers = getattr(local, "providers", None)
        if providers is None:
            providers = local.providers = make_providers()
        tried = (lambda source: on_try(song, source)) if on_try else None
        match = find_match(providers, song, only_verified, on_try=tried)
        # Connection lost: wait until it is back and search this song again
        # instead of reporting it as not found.
        attempts = 0
        while guard is not None and match.network_error and not match.found and attempts < network_retries:
            attempts += 1
            if not guard.wait_until_online():
                return MatchResult(song=song, error=CANCELLED)
            match = find_match(providers, song, only_verified, on_try=tried)
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
