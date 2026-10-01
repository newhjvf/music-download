"""Offline stand-ins for spotDL's YouTube Music provider."""

from typing import Dict, List, Optional

from spotdl.providers.audio.base import AudioProvider
from spotdl.types.result import Result


def make_result(
    video_id: str,
    name: str,
    artists: List[str],
    duration: float,
    verified: bool = True,
    album: Optional[str] = None,
    views: Optional[int] = None,
) -> Result:
    host = "music" if verified else "www"
    return Result(
        source="youtube-music",
        url=f"https://{host}.youtube.com/watch?v={video_id}",
        verified=verified,
        name=name,
        duration=duration,
        author=artists[0],
        result_id=video_id,
        artists=tuple(artists),
        album=album,
        views=views,
        isrc_search=False,
    )


class StubProvider(AudioProvider):
    """Uses spotDL's real AudioProvider.search/scoring, but canned results."""

    SUPPORTS_ISRC = False
    GET_RESULTS_OPTS = [{"filter": "songs"}]

    def __init__(self, results: Dict[str, List[Result]], fail: Optional[Exception] = None):
        super().__init__(output_format="mp3")
        self.results = results
        self.fail = fail
        self.queries: List[str] = []

    def get_results(self, search_term: str, **kwargs) -> List[Result]:
        self.queries.append(search_term)
        if self.fail:
            raise self.fail
        return list(self.results.get(search_term, []))

    def get_views(self, url: str) -> int:  # avoid yt-dlp network calls
        return 0
