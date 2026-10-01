from spotdl.utils.matching import order_results as spotdl_order_results

from musicdl.csv_import import TrackRow, row_to_song
from musicdl.matching import find_match, find_matches, order_results, youtube_cover_url

from .stubs import StubProvider, make_result


def song(title="Smells Like Teen Spirit", artists=("Nirvana",), album="Nevermind", duration=0):
    return row_to_song(
        TrackRow(line=2, title=title, artists=list(artists), album=album, duration=duration)
    )


QUERY = "nirvana - smells like teen spirit"
CORRECT = make_result("good", "Smells Like Teen Spirit", ["Nirvana"], 301, album="Nevermind")
WRONG_ARTIST = make_result("cover", "Smells Like Teen Spirit", ["Some Cover Band"], 300)
WRONG_TITLE = make_result("other", "Come As You Are", ["Nirvana"], 219, album="Nevermind")


def test_spotdl_rejects_everything_without_duration():
    # This is why the CSV mode needs the unknown-duration tolerance.
    assert spotdl_order_results([CORRECT], song()) == {}


def test_unknown_duration_ranks_by_title_and_artist():
    scores = order_results([WRONG_ARTIST, WRONG_TITLE, CORRECT], song())
    assert CORRECT in scores
    assert max(scores, key=scores.get) == CORRECT
    assert WRONG_ARTIST not in scores  # spotDL's artist filter still applies


def test_known_duration_uses_spotdl_unchanged():
    s = song(duration=301)
    assert order_results([CORRECT, WRONG_TITLE], s) == spotdl_order_results([CORRECT, WRONG_TITLE], s)


def test_find_match_returns_result_details():
    provider = StubProvider({QUERY: [WRONG_ARTIST, CORRECT, WRONG_TITLE]})
    match = find_match(provider, song())
    assert match.found
    assert match.url == CORRECT.url
    assert match.title == "Smells Like Teen Spirit"
    assert match.author == "Nirvana"
    assert match.duration == 301
    assert provider.queries == [QUERY]
    # wrapper removed again
    assert "get_results" not in vars(provider)


def test_find_match_cyrillic():
    s = song(title="Кукушка", artists=("Кино",), album="Звезда по имени Солнце")
    good = make_result("kino", "Кукушка", ["Кино"], 400, album="Звезда по имени Солнце")
    provider = StubProvider({"кино - кукушка": [good]})
    assert find_match(provider, s).url == good.url


def test_find_match_not_found():
    match = find_match(StubProvider({QUERY: [WRONG_ARTIST]}), song())
    assert not match.found
    assert match.error == "not found"


def test_find_match_provider_error():
    match = find_match(StubProvider({}, fail=ConnectionError("blocked")), song())
    assert not match.found
    assert "blocked" in match.error


def test_find_matches_parallel_keeps_order():
    other = song(title="Come As You Are")
    results = {QUERY: [CORRECT], "nirvana - come as you are": [WRONG_TITLE]}
    seen = []
    matches = find_matches(
        [song(), other, song(title="Missing")],
        threads=3,
        provider_factory=lambda: StubProvider(results),
        on_result=seen.append,
    )
    assert [m.url for m in matches] == [CORRECT.url, WRONG_TITLE.url, None]
    assert len(seen) == 3


def test_youtube_cover_url():
    assert youtube_cover_url("https://music.youtube.com/watch?v=abc&list=x") == (
        "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    )
    assert youtube_cover_url("https://example.com/") is None
