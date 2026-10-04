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
    "Bandcamp": "Bandcamp",
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
    album: str = ""  # album the source knows (fallback for the tags)
    alternates: List[str] = dataclasses.field(default_factory=list)  # other trusted URLs, tried if the download fails

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
        r"roblox",
        r"роблокс",
        r"minecraft",
        r"майнкрафт",
        r"\bgameplay\b",
        r"\bгеймплей\b",
        r"\bamv\b",
        r"tik\s*tok",
        r"тик\s*ток",
        r"#shorts?\b",
    )
]


# Censored versions are used only when nothing else exists (see ``Candidate``).
CENSORED = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"без\s*мата",
        r"\bцензур\w*",
        r"\bcensored\b",
        r"\bclean\s*(?:version|ver\.?)\b",
        r"\bclean\b",
        r"\bзапиканн\w*",
    )
]


def censored_words(song: Song, result: Result) -> List[str]:
    wanted = song.name or ""
    got = result.name or ""
    return [p.pattern for p in CENSORED if p.search(got) and not p.search(wanted)]


def not_original_words(song: Song, result: Result) -> List[str]:
    """Markers like 'type beat', 'кавер', 'live' found in the result title but
    not in the song title."""
    wanted = song.name or ""
    got = result.name or ""
    return [p.pattern for p in NOT_ORIGINAL if p.search(got) and not p.search(wanted)]


def acceptable(song: Song, result: Result) -> bool:
    """Result can be the original song: title matches and no 'not original'
    markers (beats, covers, live, remixes, diss tracks, game videos...)."""
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


# --- official uploads vs. random re-uploads --------------------------------

_CHANNEL_NOISE = re.compile(r"\b(?:vevo|official|music|records?|topic|channel|канал|музыка)\b|[-–—]", re.IGNORECASE)
MAX_REUPLOAD_DIFF = 10  # seconds of difference from the Spotify length


def _normalized(text: str) -> str:
    from spotdl.utils.formatter import slugify

    return slugify(text or "").replace("-", " ").strip()


def is_official(song: Song, result: Result) -> bool:
    """A YouTube Music song, a verified SoundCloud account, an auto-generated
    "Artist - Topic" channel, or a channel that is the artist itself."""
    from rapidfuzz import fuzz

    if result.verified:
        return True
    author = (result.author or "").strip()
    if author.lower().endswith("- topic"):
        return True
    channel = _normalized(_CHANNEL_NOISE.sub(" ", author))
    if not channel:
        return False
    artists = song.artists or ([song.artist] if song.artist else [])
    return any(
        fuzz.ratio(channel, _normalized(_CHANNEL_NOISE.sub(" ", artist))) >= 85 for artist in artists if artist
    )


@dataclass
class Candidate:
    result: Result
    source: str  # "YouTube Music" / "YouTube" / "SoundCloud"
    score: float  # spotDL's match score (title, artist, album, length)
    official: bool
    censored: bool = False
    licensed: bool = False  # YouTube says "Music in this video" / auto-generated

    @property
    def value(self) -> float:
        value = self.score + (25 if self.official or self.licensed else 0)
        return value - (50 if self.censored else 0)


Describe = Callable[[str], Optional[Dict[str, Any]]]


def youtube_details(url: str) -> Optional[Dict[str, Any]]:
    """What YouTube says about a video: its category ("Music", "Gaming"...) and
    whether it carries licensed music metadata. None if it cannot be read."""
    from yt_dlp import YoutubeDL

    from spotdl.utils.deno import get_local_deno_yt_dlp_options

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_retries": 1,
        **get_local_deno_yt_dlp_options(),
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # blocked, private, no network...
        logger.debug("Could not inspect %s: %s", url, exc)
        return None
    if not info:
        return None
    description = info.get("description") or ""
    return {
        "categories": [str(c) for c in (info.get("categories") or [])],
        "licensed": bool(info.get("track") and (info.get("artist") or info.get("creator")))
        or description.startswith("Provided to YouTube by"),
    }


def _is_youtube_video(url: str) -> bool:
    return "://www.youtube.com/watch" in url


def _duration_gap(song: Song, result: Result) -> Optional[float]:
    if song.duration and result.duration:
        return abs(float(song.duration) - float(result.duration))
    return None


def collect_candidates(
    provider: AudioProvider, song: Song, source: str
) -> Tuple[List[Candidate], int]:
    """All usable results of one source with spotDL's scores (same queries as
    spotDL's own ``provider.search``). Returns (candidates, raw result count)."""
    from spotdl.utils.formatter import create_song_title

    query = create_song_title(song.name, song.artists).lower()
    seen: Dict[str, Result] = {}
    dropped: List[str] = []
    kept: List[Result] = []

    def take(results: List[Result]) -> None:
        for result in results:
            seen.setdefault(result.url, result)
            if acceptable(song, result):
                kept.append(result)
            else:
                dropped.append(result.name)

    if song.isrc and provider.SUPPORTS_ISRC and not provider.search_query:
        take(provider.get_results(song.isrc))
    for options in provider.GET_RESULTS_OPTS:
        take(provider.get_results(query, **options))

    unique = list({result.url: result for result in kept}.values())
    scores = order_results(unique, song, provider.search_query) if unique else {}
    candidates = [
        Candidate(
            result=result,
            source=source,
            score=score,
            official=is_official(song, result),
            censored=bool(censored_words(song, result)),
        )
        for result, score in scores.items()
    ]
    if not candidates:
        logger.info(
            "%s: no match for %s: %d results, %d rejected %s",
            provider.name,
            song.display_name,
            len(seen),
            len(dropped),
            dropped[:3],
        )
    return candidates, len(seen)


def choose(
    song: Song, candidates: List[Candidate], only_official: bool, describe: Optional[Describe] = None
) -> Optional[Candidate]:
    """Best official upload; otherwise (unless ``only_official``) the best
    re-upload that is really music of the right length."""
    ranked = sorted(candidates, key=lambda c: c.value, reverse=True)
    official = [c for c in ranked if c.official]
    if official:
        return official[0]
    if only_official:
        return None
    for candidate in ranked:
        gap = _duration_gap(song, candidate.result)
        if gap is not None and gap > MAX_REUPLOAD_DIFF:
            logger.info("Skipped %s: length differs by %.0f s", candidate.result.url, gap)
            continue
        if describe is not None and _is_youtube_video(candidate.result.url):
            details = describe(candidate.result.url)
            if details is None:
                # A random upload we cannot look at is not worth the risk.
                logger.info("Skipped %s: could not inspect the video", candidate.result.url)
                continue
            if details["licensed"]:
                candidate.licensed = True
            elif details["categories"] and "Music" not in details["categories"]:
                logger.info("Skipped %s: category %s", candidate.result.url, details["categories"])
                continue
        return candidate
    return None


MAX_ALTERNATES = 3


def _alternates(song: Song, candidates: List[Candidate], best: Candidate, only_official: bool) -> List[str]:
    """Other URLs worth trying if the chosen one cannot be downloaded (DRM,
    403, sign-in wall): only official ones, or non-YouTube uploads of the
    right length, so no extra video lookups are needed."""
    urls: List[str] = []
    for candidate in sorted(candidates, key=lambda c: c.value, reverse=True):
        url = candidate.result.url
        if candidate is best or url == best.result.url or url in urls:
            continue
        if not candidate.official:
            gap = _duration_gap(song, candidate.result)
            if only_official or _is_youtube_video(url) or (gap is not None and gap > MAX_REUPLOAD_DIFF):
                continue
        urls.append(url)
        if len(urls) == MAX_ALTERNATES:
            break
    return urls


def find_match(
    providers: Union[AudioProvider, Sequence[AudioProvider]],
    song: Song,
    only_verified: bool = False,
    on_try: Optional[Callable[[str], None]] = None,
    describe: Optional[Describe] = None,
) -> MatchResult:
    """Look in every source (YouTube Music, Bandcamp, SoundCloud, YouTube), stopping at
    the first one that has an official upload, and pick the best candidate:
    official uploads first, then re-uploads that pass the music/length checks
    (``only_verified`` = official uploads only). Providers must not be shared
    between threads."""
    if isinstance(providers, AudioProvider):
        providers = [providers]

    from musicdl.network import looks_like_network_error

    error: Optional[str] = None
    network_error = False
    answered = False  # at least one source returned real search results
    used_any = False  # at least one source could be started
    silent: Optional[str] = None  # a source that answered without a single result
    candidates: List[Candidate] = []
    for provider in providers:
        used_any = True
        source = SOURCE_NAMES.get(provider.name, provider.name)
        if on_try:
            on_try(source)
        raw_results = 0
        for variant in query_variants(song):
            try:
                found, raw = collect_candidates(provider, variant, source)
            except Exception as exc:  # network errors, YouTube blocks, API changes
                logger.debug("%s search failed for %s: %s", source, song.display_name, exc, exc_info=True)
                error = short_error(exc)
                network_error = network_error or looks_like_network_error(exc)
                break  # this provider is unusable for this song; try the next one
            raw_results += raw
            if raw:
                answered = True
            candidates.extend(found)
            if found:
                break
        else:
            if not raw_results:
                # A real song always gets some hits; none at all means the
                # source is blocking or throttling us, not "track not found".
                silent = source
        if any(c.official and not c.censored for c in candidates):
            break  # an official upload exists: no need to look further

    best = choose(song, candidates, only_verified, describe)
    if best is not None:
        result = best.result
        return MatchResult(
            song=song,
            url=result.url,
            title=result.name,
            author=", ".join(result.artists) if result.artists else result.author,
            duration=result.duration,
            verified=best.official or best.licensed,
            source=best.source,
            album=result.album or "",
            alternates=_alternates(song, candidates, best, only_verified),
        )
    if not used_any:
        failure = getattr(providers, "last_error", None)
        if failure is not None:
            error = "Источники поиска недоступны: " + short_error(failure)
            network_error = looks_like_network_error(failure)
    elif answered and not network_error:
        error = None  # a source searched fine and had nothing good enough: plain "not found"
    elif silent and not error and not network_error:
        error = f"{silent}: пустой ответ (возможно, блокировка)"
    return MatchResult(song=song, error=error or "not found", network_error=network_error)


class ProviderChain:
    """Providers created on first use, so a source that cannot start (e.g.
    SoundCloud unreachable) is skipped instead of breaking the search."""

    def __init__(self, factories: Sequence[Callable[[], AudioProvider]]):
        self._factories = list(factories)
        self._created: Dict[int, AudioProvider] = {}
        self.last_error: Optional[BaseException] = None  # why the latest source failed to start

    def __iter__(self):
        self.last_error = None
        for index, factory in enumerate(self._factories):
            if index not in self._created:
                try:
                    self._created[index] = factory()
                except Exception as exc:
                    # Not remembered: the next song tries to start it again, so a
                    # source that was only briefly unreachable comes back.
                    logger.warning("Audio source unavailable: %s", short_error(exc))
                    self.last_error = exc
                    continue
            yield self._created[index]


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

    from musicdl.bandcamp import Bandcamp

    YouTube = _fast_youtube_class()

    # "Official tracks only" still searches every source: an artist's own
    # SoundCloud page or "Artist - Topic" channel is official too.
    return ProviderChain(
        [
            lambda: YouTubeMusic(output_format="mp3"),
            lambda: Bandcamp(output_format="mp3"),  # artists' own pages
            lambda: SoundCloud(output_format="mp3"),
            lambda: YouTube(output_format="mp3"),
        ]
    )


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
    # Inspecting YouTube videos (category, licensed music) only for real searches
    describe = youtube_details if provider_factory is default_provider_factory else None

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
        match = find_match(providers, song, only_verified, on_try=tried, describe=describe)
        # Connection lost: wait until it is back and search this song again
        # instead of reporting it as not found.
        attempts = 0
        while guard is not None and match.network_error and not match.found and attempts < network_retries:
            attempts += 1
            if not guard.wait_until_online():
                return MatchResult(song=song, error=CANCELLED)
            match = find_match(providers, song, only_verified, on_try=tried, describe=describe)
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
