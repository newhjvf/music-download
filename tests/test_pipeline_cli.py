import csv
from pathlib import Path

import pytest
from rich.console import Console

import musicdl.cli as cli
from musicdl.csv_import import TrackRow, row_to_song
from musicdl.pipeline import MIN_FILE_SIZE, expected_path, format_duration, split_existing, write_report
from musicdl.spotify_input import SpotifyInputError, check_url, load_credentials

from .stubs import StubProvider, make_result

FAKE_MP3 = b"ID3" + b"\0" * MIN_FILE_SIZE
OLD_MP3 = b"old" + b"\0" * MIN_FILE_SIZE
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.csv"

RESULTS = {
    "queen - bohemian rhapsody": [make_result("bohe", "Bohemian Rhapsody", ["Queen"], 355)],
    "nirvana - smells like teen spirit": [make_result("teen", "Smells Like Teen Spirit", ["Nirvana"], 301)],
    "кино - кукушка": [make_result("kuku", "Кукушка", ["Кино"], 400, verified=False)],
    # "Under Pressure" -> nothing found
}


def stub_factory():
    return StubProvider(RESULTS)


def run_cli(argv, monkeypatch, downloads=None):
    monkeypatch.setattr(cli, "console", Console(width=250))
    calls = []

    def fake_download(songs, options, on_status):
        calls.append(songs)
        out = []
        for song in songs:
            path = expected_path(song, options.out_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(FAKE_MP3)
            out.append((song, path))
        return out

    args = cli.build_parser().parse_args(argv)
    code = cli.run(
        args,
        provider_factory=stub_factory,
        downloader=downloads or fake_download,
        ffmpeg_check=lambda info: None,
    )
    return code, calls


def read_report(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_filename_template(tmp_path):
    song = row_to_song(TrackRow(line=2, title="Bohemian Rhapsody", artists=["Queen", "X"]))
    assert expected_path(song, tmp_path) == tmp_path / "Queen - Bohemian Rhapsody.mp3"


def test_filename_is_sanitized(tmp_path):
    song = row_to_song(TrackRow(line=2, title='What? "Now"', artists=["AC/DC"]))
    name = expected_path(song, tmp_path).name
    assert name.endswith(".mp3")
    assert not any(ch in name for ch in '/\\?"')


def test_dry_run_table_and_report(tmp_path, monkeypatch, capsys):
    code, calls = run_cli(["csv", str(SAMPLE), "--out", str(tmp_path), "--dry-run"], monkeypatch)
    assert code == 0
    assert calls == []  # nothing downloaded
    out = capsys.readouterr().out
    assert "Bohemian Rhapsody" in out and "5:55" in out
    rows = read_report(tmp_path / "not_found.csv")
    assert [(r["Artist name"], r["Track name"]) for r in rows] == [("Queen, David Bowie", "Under Pressure")]
    assert not list(tmp_path.glob("*.mp3"))


def test_download_skips_existing(tmp_path, monkeypatch):
    (tmp_path / "Queen - Bohemian Rhapsody.mp3").write_bytes(OLD_MP3)
    code, calls = run_cli(["csv", str(SAMPLE), "--out", str(tmp_path)], monkeypatch)
    assert code == 0
    downloaded = [s.name for s in calls[0]]
    assert downloaded == ["Smells Like Teen Spirit", "Кукушка"]
    assert (tmp_path / "Queen - Bohemian Rhapsody.mp3").read_bytes() == OLD_MP3
    song = calls[0][0]
    assert song.download_url == "https://music.youtube.com/watch?v=teen"
    assert song.cover_url == "https://i.ytimg.com/vi/teen/hqdefault.jpg"
    assert (tmp_path / "Кино - Кукушка.mp3").exists()

    # second run: everything found is already there, nothing new downloaded
    code, calls = run_cli(["csv", str(SAMPLE), "--out", str(tmp_path)], monkeypatch)
    assert calls == []


def test_failed_download_goes_to_report(tmp_path, monkeypatch):
    code, _ = run_cli(
        ["csv", str(SAMPLE), "--out", str(tmp_path), "--report", str(tmp_path / "r.csv")],
        monkeypatch,
        downloads=lambda songs, *a: [(s, None) for s in songs],
    )
    reasons = {r["Track name"]: r["Reason"] for r in read_report(tmp_path / "r.csv")}
    assert reasons["Under Pressure"] == "not found"
    assert reasons["Bohemian Rhapsody"] == "download failed"


def test_write_report_removes_stale(tmp_path):
    report = tmp_path / "not_found.csv"
    report.write_text("old")
    assert write_report(report, []) is None
    assert not report.exists()


def test_split_existing(tmp_path):
    a = row_to_song(TrackRow(line=2, title="A", artists=["X"]))
    b = row_to_song(TrackRow(line=3, title="B", artists=["X"]))
    (tmp_path / "X - A.mp3").write_bytes(FAKE_MP3)
    assert split_existing([a, b], tmp_path) == ([b], [a])


@pytest.mark.parametrize("seconds,text", [(0, "?"), (59.6, "1:00"), (355, "5:55"), (3723, "1:02:03")])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv", "track"),
        ("https://open.spotify.com/intl-de/album/4u7EnebtmKWzUH433cf5Qv?si=abc", "album"),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "playlist"),
    ],
)
def test_check_url(url, kind):
    assert check_url(url) == kind


@pytest.mark.parametrize(
    "url", ["https://open.spotify.com/artist/4u7EnebtmKWzUH433cf5Qv", "https://youtube.com/watch?v=x", "abc"]
)
def test_check_url_rejects(url):
    with pytest.raises(SpotifyInputError):
        check_url(url)


def test_load_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    env = tmp_path / ".env"
    env.write_text("SPOTIFY_CLIENT_ID=\nSPOTIFY_CLIENT_SECRET=\n", encoding="utf-8")
    assert load_credentials(env) == (None, None)

    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    env.write_text("SPOTIFY_CLIENT_ID=id\nSPOTIFY_CLIENT_SECRET=secret\n", encoding="utf-8")
    assert load_credentials(env) == ("id", "secret")

    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    env.write_text("SPOTIFY_CLIENT_ID=id\n", encoding="utf-8")
    with pytest.raises(SpotifyInputError):
        load_credentials(env)


def test_missing_csv_file(tmp_path):
    assert cli.main(["csv", str(tmp_path / "nope.csv")]) == 1


def _listing_song():
    from spotdl.types.song import Song

    return Song.from_missing_data(
        name="Track",
        artists=["Artist"],
        artist="Artist",
        album_name="Album",
        album_id="alb",
        disc_number=1,
        duration=200,
        track_number=3,
        tracks_count=10,
        song_id="4u7EnebtmKWzUH433cf5Qv",
        url="https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv",
        cover_url="https://i.scdn.co/image/x",
    )


def test_complete_song_avoids_spotdl_refetch():
    from musicdl.spotify_input import complete_song

    song = complete_song(_listing_song())
    for field in ("genres", "disc_count", "tracks_count", "track_number", "album_id", "album_artist"):
        assert getattr(song, field) is not None, field
    assert (song.track_number, song.tracks_count, song.album_artist) == (3, 10, "Artist")
    assert song.cover_url == "https://i.scdn.co/image/x" and song.duration == 200


def test_songs_from_url_uses_single_listing_request(monkeypatch):
    import spotdl.utils.search as search

    from musicdl.spotify_input import songs_from_url

    calls = []
    monkeypatch.setattr(search, "get_simple_songs", lambda q: calls.append(q) or [_listing_song()])
    monkeypatch.setattr(search, "reinit_song", lambda s: pytest.fail("per-track refetch"))
    songs = songs_from_url("https://open.spotify.com/playlist/1FacUjfBAJd0HVjgVUTI9r?si=x")
    assert calls == [["https://open.spotify.com/playlist/1FacUjfBAJd0HVjgVUTI9r?si=x"]]
    assert songs[0].genres == []
