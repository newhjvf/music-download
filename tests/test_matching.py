import pytest

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


def test_short_error():
    from musicdl.matching import short_error

    assert short_error(ConnectionError("a\n b")) == "ConnectionError: a b"
    assert len(short_error(ValueError("x" * 500))) < 100


# --- fallbacks, title variants, sanity check -------------------------------

from musicdl.matching import ProviderChain, clean_title, query_variants, title_matches  # noqa: E402


class SecondSource(StubProvider):
    pass


@pytest.mark.parametrize(
    "title,cleaned",
    [
        ("Ты не права (broken bass remix)", "Ты не права"),
        ("Song feat. Someone", "Song"),
        ("Song [Remastered 2011] - Live", "Song"),
        ("RUSSIAN SOUL", "RUSSIAN SOUL"),
    ],
)
def test_clean_title(title, cleaned):
    assert clean_title(title) == cleaned


def test_query_variants():
    assert [v.name for v in query_variants(song(title="Ты не права (broken bass remix)"))] == [
        "Ты не права (broken bass remix)",
        "Ты не права",
    ]
    assert len(query_variants(song())) == 1


def test_title_check_rejects_different_song():
    s = song(title="Силиконовый Гном", artists=("Мэйби Бэйби",))
    wrong = make_result("x", "ДИСС НА ИВАНГАЯ (Бит: кто-то)", ["Esenin-Senin"], 217)
    right = make_result("y", "Мэйби Бэйби - Силиконовый Гном (Official Audio)", ["Мэйби Бэйби"], 200)
    assert not title_matches(s, wrong)
    assert title_matches(s, right)


def test_wrong_lone_result_is_rejected_and_next_source_used():
    s = song(title="Силиконовый Гном", artists=("Мэйби Бэйби",))
    wrong = make_result("x", "ДИСС НА ИВАНГАЯ", ["Мэйби Бэйби"], 217)
    right = make_result("sc", "Силиконовый Гном", ["Мэйби Бэйби"], 200, verified=False)
    query = "мэйби бэйби - силиконовый гном"
    match = find_match([StubProvider({query: [wrong]}), SecondSource({query: [right]})], s)
    assert match.url == right.url
    assert match.source == "SecondSource"


def test_cleaned_title_variant_is_tried():
    s = song(title="Ты не права (broken bass remix)", artists=("Размаха",))
    hit = make_result("r", "Ты не права", ["Размаха"], 180)
    provider = StubProvider({"размаха - ты не права": [hit]})
    match = find_match(provider, s)
    assert match.url == hit.url
    assert provider.queries == ["размаха - ты не права (broken bass remix)", "размаха - ты не права"]


def test_provider_error_falls_through_to_next_source():
    s = song()
    failing = StubProvider({}, fail=ConnectionError("Sign in to confirm you're not a bot"))
    match = find_match([failing, SecondSource({QUERY: [CORRECT]})], s)
    assert match.url == CORRECT.url and match.error is None


def test_view_count_errors_do_not_fail_search():
    class NoViews(StubProvider):
        def get_views(self, url):
            raise RuntimeError("YT-DLP download error")

    a = make_result("a", "Smells Like Teen Spirit", ["Nirvana"], 301)
    b = make_result("b", "Smells Like Teen Spirit", ["Nirvana"], 302)
    match = find_match(NoViews({QUERY: [a, b]}), song())
    assert match.found
    assert "get_views" not in vars(match) and match.error is None


def test_provider_chain_skips_broken_source():
    def broken():
        raise OSError("soundcloud unreachable")

    good = StubProvider({})
    chain = ProviderChain([broken, lambda: good])
    assert list(chain) == [good]
    assert list(chain) == [good]  # created once
