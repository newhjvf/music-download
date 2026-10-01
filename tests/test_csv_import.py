from pathlib import Path

import pytest

from musicdl.csv_import import (
    CsvFormatError,
    parse_csv,
    parse_duration,
    row_to_song,
    split_artists,
)

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.csv"


def write(tmp_path: Path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "export.csv"
    path.write_text(text, encoding=encoding, newline="")
    return path


def test_sample_csv_parsed():
    rows = parse_csv(SAMPLE)
    assert [(r.artist, r.title) for r in rows] == [
        ("Queen", "Bohemian Rhapsody"),
        ("Nirvana", "Smells Like Teen Spirit"),
        ("Кино", "Кукушка"),
        ("Queen", "Under Pressure"),
    ]
    assert rows[0].isrc == "GBUM71029604"
    assert rows[0].spotify_id == "4u7EnebtmKWzUH433cf5Qv"
    assert rows[0].album == "A Night At The Opera (2011 Remaster)"
    assert rows[2].isrc is None and rows[2].spotify_id is None
    assert rows[3].artists == ["Queen", "David Bowie"]


def test_bom_semicolon_and_header_case(tmp_path):
    path = write(
        tmp_path,
        "TRACK NAME;artist name;ALBUM\r\nПесня;Исполнитель;Альбом\r\n",
        encoding="utf-8-sig",
    )
    rows = parse_csv(path)
    assert len(rows) == 1
    assert (rows[0].title, rows[0].artist, rows[0].album) == ("Песня", "Исполнитель", "Альбом")


def test_missing_columns_raise(tmp_path):
    path = write(tmp_path, "Album,ISRC\nX,Y\n")
    with pytest.raises(CsvFormatError):
        parse_csv(path)


def test_empty_file(tmp_path):
    assert parse_csv(write(tmp_path, "")) == []


def test_short_rows_and_invalid_spotify_id(tmp_path):
    path = write(
        tmp_path,
        "Track name,Artist name,Album,Spotify - id\nSong,Artist\nSong 2,Artist,,not-an-id\n",
    )
    rows = parse_csv(path)
    assert [r.title for r in rows] == ["Song", "Song 2"]
    assert rows[0].album == "" and rows[1].spotify_id is None


def test_duplicates_are_case_insensitive(tmp_path):
    path = write(tmp_path, "Track name,Artist name\nSong,Artist\nsong,ARTIST\n")
    assert len(parse_csv(path)) == 1


def test_optional_duration_column(tmp_path):
    path = write(tmp_path, "Track name,Artist name,Duration (ms)\nSong,Artist,215000\n")
    assert parse_csv(path)[0].duration == 215


@pytest.mark.parametrize(
    "value,expected",
    [("3:45", 225), ("1:02:03", 3723), ("225", 225), ("225000", 225), ("", 0), ("abc", 0)],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


def test_split_artists():
    assert split_artists("A, B;C") == ["A", "B", "C"]
    assert split_artists("Simon & Garfunkel") == ["Simon & Garfunkel"]


def test_row_to_song_needs_no_spotify():
    rows = parse_csv(SAMPLE)
    song = row_to_song(rows[0])
    assert song.name == "Bohemian Rhapsody"
    assert song.artist == "Queen" and song.artists == ["Queen"]
    assert song.isrc == "GBUM71029604"
    assert song.url == "https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv"
    # None of these may be None, otherwise spotDL's Downloader calls reinit_song (Spotify).
    for field in ("genres", "disc_count", "tracks_count", "track_number", "album_id", "album_artist"):
        assert getattr(song, field) is not None, field

    no_id = row_to_song(rows[2])
    assert no_id.url.startswith("https://musicdl.local/csv/")
    assert no_id.song_id == row_to_song(rows[2]).song_id  # stable
