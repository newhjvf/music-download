from musicdl.csv_import import TrackRow, row_to_song
from musicdl.metadata import (
    enrich_songs,
    fill_song,
    needs_metadata,
    songs_from_text,
    split_query,
    track_info,
)

RAW = {
    "name": "CULTURE",
    "artists": [{"name": "Aarne"}, {"name": "Toxi$"}],
    "album": {
        "name": "CULTURE EP",
        "id": "alb1",
        "release_date": "2024-05-17",
        "total_tracks": 6,
        "album_type": "single",
        "artists": [{"name": "Aarne"}],
        "images": [{"url": "small.jpg", "width": 64}, {"url": "big.jpg", "width": 640}],
    },
    "track_number": 3,
    "disc_number": 1,
    "duration_ms": 185000,
    "explicit": True,
    "external_ids": {"isrc": "ru1234567890"},
}


def song(title="culture", artist="aarne", album=""):
    return row_to_song(TrackRow(line=2, title=title, artists=[artist], album=album))


def test_split_query():
    assert split_query("Aarne - CULTURE") == ("Aarne", "CULTURE")
    assert split_query("Aarne – CULTURE – remix") == ("Aarne", "CULTURE – remix")
    assert split_query("hello world") == ("", "hello world")
    assert split_query("Foo-bar") == ("", "Foo-bar")  # no spaces around: part of the name


def test_track_info_reads_spotify_fields():
    info = track_info(RAW)
    assert info["album_name"] == "CULTURE EP" and info["year"] == 2024 and info["date"] == "2024-05-17"
    assert info["cover_url"] == "big.jpg" and info["track_number"] == 3 and info["tracks_count"] == 6
    assert info["isrc"] == "RU1234567890" and info["duration"] == 185


def test_track_info_survives_partial_data():
    info = track_info({"name": "x"})
    assert info["album_name"] == "" and info["year"] == 0 and info["cover_url"] == ""


def test_fill_only_gaps_keeps_known_values():
    s = song(album="My Album")
    filled = fill_song(s, track_info(RAW))
    assert filled.album_name == "My Album"  # known value wins
    assert filled.year == 2024 and filled.cover_url == "big.jpg"
    assert filled.name == "culture"  # spelling untouched


def test_prefer_found_replaces_csv_placeholders_and_canonical_renames():
    filled = fill_song(song(), track_info(RAW), prefer_found=True, canonical=True)
    assert filled.track_number == 3 and filled.tracks_count == 6
    assert filled.name == "CULTURE" and filled.artists == ["Aarne", "Toxi$"] and filled.artist == "Aarne"


def test_enrich_skips_complete_songs_and_survives_failures():
    complete = fill_song(song(), track_info(RAW))
    calls = []

    def lookup(s):
        calls.append(s.name)
        if s.name == "broken":
            raise ConnectionError("offline")
        if s.name == "unknown":
            return None
        return track_info(RAW)

    songs = [complete, song("broken"), song("unknown"), song("good")]
    result = enrich_songs(songs, lookup=lookup)
    assert calls == ["broken", "unknown", "good"]  # complete one not looked up
    assert result[0] is complete
    assert not result[1].album_name and not result[2].album_name  # unchanged
    assert result[3].album_name == "CULTURE EP"
    assert needs_metadata(result[1]) and not needs_metadata(result[3])


def test_songs_from_text():
    def lookup_text(text):
        return {"name": "Culture", "artists": ["Aarne"]} if "culture" in text else None

    songs, skipped = songs_from_text(
        "Aarne - CULTURE\n\n  aarne - culture \nfoo, bar - Baz\nculture aarne\nsomething unknown\n",
        lookup_text=lookup_text,
    )
    assert [(s.artist, s.name) for s in songs] == [("Aarne", "CULTURE"), ("foo", "Baz")]
    assert skipped == ["something unknown"]
    assert songs[1].artists == ["foo", "bar"]
