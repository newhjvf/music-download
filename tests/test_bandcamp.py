import requests

from musicdl import bandcamp
from musicdl.bandcamp import Bandcamp, parse_search
from musicdl.matching import find_match, is_official

from .test_matching import song

PAGE = """
<ul class="result-items">
<li class="searchresult data-search" data-search="{}">
  <div class="art"><img src="x.jpg"></div>
  <div class="result-info">
    <div class="itemtype">TRACK</div>
    <div class="heading"><a href="https://aarne.bandcamp.com/track/culture?from=search&amp;search_item_id=1">CULTURE</a></div>
    <div class="subhead">from Culture EP by Aarne &amp; Toxi$</div>
    <div class="itemurl"><a href="https://aarne.bandcamp.com/track/culture?from=search">aarne.bandcamp.com/track/culture</a></div>
    <div class="length">length: 03:05</div>
  </div>
</li>
<li class="searchresult data-search">
  <div class="heading"><a href="https://aarne.bandcamp.com/album/culture-ep">Culture EP</a></div>
  <div class="subhead">by Aarne</div>
</li>
<li class="searchresult data-search">
  <div class="heading"><a href="https://other.bandcamp.com/track/no-album">Без альбома</a></div>
  <div class="subhead">by Кто-то</div>
</li>
</ul>
"""


def test_parse_search_reads_tracks_only():
    hits = parse_search(PAGE)
    assert hits == [
        {
            "url": "https://aarne.bandcamp.com/track/culture",
            "title": "CULTURE",
            "artist": "Aarne & Toxi$",
            "album": "Culture EP",
            "duration": 185,
        },
        {
            "url": "https://other.bandcamp.com/track/no-album",
            "title": "Без альбома",
            "artist": "Кто-то",
            "album": None,
            "duration": 0,
        },
    ]


def test_parse_search_survives_garbage():
    assert parse_search("<html>nothing here</html>") == []


def test_provider_turns_hits_into_results(monkeypatch):
    class Response:
        text = PAGE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(bandcamp.requests, "get", lambda *a, **k: Response())
    results = Bandcamp(output_format="mp3").get_results("aarne - culture")
    assert [r.name for r in results] == ["CULTURE", "Без альбома"]
    assert results[0].author == "Aarne & Toxi$" and results[0].duration == 185


def test_http_error_is_a_provider_error(monkeypatch):
    def refuse(*a, **k):
        raise requests.ConnectionError("blocked")

    monkeypatch.setattr(bandcamp.requests, "get", refuse)
    match = find_match(Bandcamp(output_format="mp3"), song())
    assert not match.found and "blocked" in match.error


def test_artist_page_on_bandcamp_is_official(monkeypatch):
    class Response:
        text = PAGE.replace("Aarne &amp; Toxi$", "Nirvana").replace("CULTURE", "Smells Like Teen Spirit")

        def raise_for_status(self):
            pass

    monkeypatch.setattr(bandcamp.requests, "get", lambda *a, **k: Response())
    s = song()
    match = find_match(Bandcamp(output_format="mp3"), s, only_verified=True)
    assert match.found and match.source == "Bandcamp" and match.verified
