from pathlib import Path

import pytest

from musicdl.job import JobError, JobEvents, JobOptions, run_job
from musicdl.pipeline import MIN_FILE_SIZE, expected_path

from .stubs import StubProvider, make_result

FAKE_MP3 = b"ID3" + b"\0" * MIN_FILE_SIZE
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.csv"
RESULTS = {
    "queen - bohemian rhapsody": [make_result("bohe", "Bohemian Rhapsody", ["Queen"], 355)],
    "nirvana - smells like teen spirit": [make_result("teen", "Smells Like Teen Spirit", ["Nirvana"], 301)],
}


def options(tmp_path, **kw):
    return JobOptions(source_kind="csv", source=str(SAMPLE), out_dir=tmp_path, threads=2, **kw)


def fake_download(songs, opts, on_status):
    out = []
    for song in songs:
        on_status(song.url, "Downloading", 50)
        path = expected_path(song, opts.out_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FAKE_MP3)
        out.append((song, path))
    return out


def test_events_and_summary(tmp_path):
    log = []
    events = JobEvents(
        info=lambda m: log.append(("info", m)),
        songs_loaded=lambda todo, existing: log.append(("songs", len(todo), len(existing))),
        matched=lambda m: log.append(("match", m.song.name, m.found)),
        download_status=lambda key, status, pct: log.append(("dl", status)),
        phase=lambda name, count: log.append(("phase", name, count)),
    )
    ffmpeg_calls = []
    summary = run_job(
        options(tmp_path),
        events,
        provider_factory=lambda: StubProvider(RESULTS),
        downloader=fake_download,
        ffmpeg_check=ffmpeg_calls.append,
    )
    assert (summary.total, summary.existing, summary.processed, summary.succeeded) == (4, 0, 4, 2)
    assert ("songs", 4, 0) in log
    assert ("phase", "search", 4) in log and ("phase", "download", 2) in log
    assert sum(1 for e in log if e[0] == "match") == 4
    assert log.count(("dl", "Done")) == 2
    assert len(ffmpeg_calls) == 1
    assert summary.report == tmp_path / "not_found.csv"


def test_dry_run_never_downloads_or_needs_ffmpeg(tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not be called")

    summary = run_job(
        options(tmp_path, dry_run=True),
        provider_factory=lambda: StubProvider(RESULTS),
        downloader=boom,
        ffmpeg_check=boom,
    )
    assert summary.succeeded == 2


def test_nothing_found_skips_ffmpeg(tmp_path):
    summary = run_job(
        options(tmp_path),
        provider_factory=lambda: StubProvider({}),
        downloader=fake_download,
        ffmpeg_check=lambda info: (_ for _ in ()).throw(AssertionError("no ffmpeg needed")),
    )
    assert summary.succeeded == 0 and len(summary.failed) == 4


def test_missing_file_is_user_error(tmp_path):
    with pytest.raises(JobError, match="не найден"):
        run_job(JobOptions(source_kind="csv", source=str(tmp_path / "x.csv"), out_dir=tmp_path))


def test_bad_csv_is_user_error(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(JobError, match="Track name|title"):
        run_job(JobOptions(source_kind="csv", source=str(bad), out_dir=tmp_path))


def test_bad_spotify_link_is_user_error(tmp_path):
    with pytest.raises(JobError):
        run_job(JobOptions(source_kind="url", source="https://example.com", out_dir=tmp_path))


def test_found_matches_are_reused(tmp_path):
    provider_calls = []

    def factory():
        provider_calls.append(1)
        return StubProvider(RESULTS)

    def no_search_factory():
        raise AssertionError("everything found should come from the cache")

    first = run_job(options(tmp_path, dry_run=True), provider_factory=factory)
    assert first.succeeded == 2 and provider_calls

    # second check: found tracks come from the cache, only the 2 missing ones are searched
    searched = []

    def counting_factory():
        provider = StubProvider(RESULTS)
        original = provider.get_results

        def get_results(term, **kw):
            searched.append(term)
            return original(term, **kw)

        provider.get_results = get_results
        return provider

    matched = []
    second = run_job(
        options(tmp_path, dry_run=True),
        JobEvents(matched=matched.append),
        provider_factory=counting_factory,
    )
    assert second.succeeded == 2
    assert len(matched) == 4
    assert not any("bohemian" in term or "teen spirit" in term for term in searched)
    assert [m.url for m in second.matches if m.found] == [m.url for m in first.matches if m.found]


def test_cache_disabled(tmp_path):
    opts = options(tmp_path, dry_run=True)
    opts.cache_path = None
    run_job(opts, provider_factory=lambda: StubProvider(RESULTS))
    assert not (tmp_path / "matches.json").exists()


def test_search_threads():
    from musicdl.job import search_threads

    assert [search_threads(n) for n in (1, 2, 4, 8)] == [2, 4, 8, 8]


def test_cache_from_older_matching_rules_is_ignored(tmp_path):
    import json

    from musicdl.cache import VERSION, MatchCache

    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": VERSION - 1, "entries": {"x|all": {"url": "u", "time": 9e12}}}), encoding="utf-8")
    assert MatchCache(path).entries == {}


class FakeSong:
    def __init__(self, url="spotify:1", download_url="https://www.youtube.com/watch?v=main"):
        self.url = url
        self.download_url = download_url


def fake_downloader_class(settings_out, behaviour):
    """spotDL Downloader stand-in: ``behaviour(song)`` -> path or None."""

    class FakeDownloader:
        errors: list = []
        progress_handler = type("P", (), {})()

        def __init__(self, settings):
            settings_out.update(settings)

        def download_multiple_songs(self, songs):
            return [(song, behaviour(song)) for song in songs]

    return FakeDownloader


def test_downloads_use_the_thread_setting(monkeypatch, tmp_path):
    from musicdl import job
    import spotdl.download.downloader as spotdl_downloader

    captured = {}
    monkeypatch.setattr(spotdl_downloader, "Downloader", fake_downloader_class(captured, lambda s: None))
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    monkeypatch.setattr(job, "original_soundcloud_files", lambda songs: {})
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path, threads=5)
    job.download_songs([FakeSong()], options, lambda *a: None)
    assert captured["threads"] == 5


def test_failed_download_is_retried_from_alternate_sources(monkeypatch, tmp_path):
    from musicdl import job
    import spotdl.download.downloader as spotdl_downloader

    good = tmp_path / "a.mp3"
    good.write_bytes(b"0" * job.MIN_FILE_SIZE)
    tried = []

    def behaviour(song):
        tried.append(song.download_url)
        return good if song.download_url.endswith("ok") else None  # first two are DRM / 403

    monkeypatch.setattr(spotdl_downloader, "Downloader", fake_downloader_class({}, behaviour))
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    monkeypatch.setattr(job, "original_soundcloud_files", lambda songs: {})
    song = FakeSong()
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path)
    options.alternates = {"spotify:1": ["https://soundcloud.com/drm", "https://bandcamp.com/ok", "https://never"]}
    results = job.download_songs([song], options, lambda *a: None)
    assert tried == ["https://www.youtube.com/watch?v=main", "https://soundcloud.com/drm", "https://bandcamp.com/ok"]
    assert results == [(song, good)]


def test_all_sources_failing_returns_the_failure(monkeypatch, tmp_path):
    from musicdl import job
    import spotdl.download.downloader as spotdl_downloader

    monkeypatch.setattr(spotdl_downloader, "Downloader", fake_downloader_class({}, lambda s: None))
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    monkeypatch.setattr(job, "original_soundcloud_files", lambda songs: {})
    song = FakeSong()
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path)
    options.alternates = {"spotify:1": ["https://other"]}
    results = job.download_songs([song], options, lambda *a: None)
    assert results == [(song, None)]


def test_progress_forwards_stage_changes_only(monkeypatch, tmp_path):
    from musicdl import job
    import spotdl.download.downloader as spotdl_downloader

    holder = {}

    class Downloader(fake_downloader_class({}, lambda s: None)):
        def __init__(self, settings):
            super().__init__(settings)
            holder["handler"] = self.progress_handler

    monkeypatch.setattr(spotdl_downloader, "Downloader", Downloader)
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    monkeypatch.setattr(job, "original_soundcloud_files", lambda songs: {})
    seen = []
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path)
    job.download_songs([FakeSong()], options, lambda key, status, percent: seen.append((status, percent)))
    tracker = type("T", (), {"song": FakeSong(), "progress": 40})()
    for message in ("Downloading", "Downloading", "Downloading", "Converting", "Converting", "Done"):
        holder["handler"].update_callback(tracker, message)
    assert seen == [("Downloading", 0), ("Converting", 0), ("Done", 0)]


def test_ensure_deno_never_raises(monkeypatch):
    import spotdl.utils.deno as deno
    from musicdl import job

    monkeypatch.setattr(deno, "is_deno_installed", lambda *a: False)
    monkeypatch.setattr(deno, "download_deno", lambda: (_ for _ in ()).throw(OSError("offline")))
    job.ensure_deno()  # logs a warning, does not raise


def test_soundcloud_original_file_replaces_the_stream_when_downloadable(monkeypatch):
    import soundcloud
    from soundcloud.resource.track import Track
    from musicdl import job

    class FakeTrack(Track):
        def __init__(self, downloadable):  # bypass the dataclass fields
            object.__setattr__(self, "id", 7)
            object.__setattr__(self, "secret_token", None)
            object.__setattr__(self, "downloadable", downloadable)

    class FakeClient:
        def __init__(self, auth_token=None):
            pass

        def resolve(self, url):
            return FakeTrack("free" in url)

        def get_track_original_download(self, track_id, token):
            return "https://cdn.example/original.wav"

    monkeypatch.setattr(soundcloud, "SoundCloud", FakeClient)
    free = type("S", (), {"download_url": "https://soundcloud.com/a/free"})()
    locked = type("S", (), {"download_url": "https://soundcloud.com/a/locked"})()
    youtube = type("S", (), {"download_url": "https://www.youtube.com/watch?v=x"})()
    free.url, locked.url, youtube.url = "f", "l", "y"
    assert job.original_soundcloud_files([free, locked, youtube]) == {"f": "https://soundcloud.com/a/free"}
    assert free.download_url == "https://cdn.example/original.wav"
    assert locked.download_url == "https://soundcloud.com/a/locked"
    assert youtube.download_url == "https://www.youtube.com/watch?v=x"


def test_soundcloud_original_failure_keeps_the_stream(monkeypatch):
    import soundcloud
    from musicdl import job

    class Broken:
        def __init__(self, auth_token=None):
            pass

        def resolve(self, url):
            raise PermissionError("401 login required")

    monkeypatch.setattr(soundcloud, "SoundCloud", Broken)
    song = type("S", (), {"download_url": "https://soundcloud.com/a/b"})()
    song.url = "s"
    assert job.original_soundcloud_files([song]) == {}
    assert song.download_url == "https://soundcloud.com/a/b"


def test_failed_original_file_falls_back_to_the_stream(monkeypatch, tmp_path):
    from musicdl import job
    import spotdl.download.downloader as spotdl_downloader

    class Song:
        url = "spotify:1"
        download_url = "https://cdn.example/original.wav"

    song = Song()
    good = tmp_path / "a.mp3"
    good.write_bytes(b"0" * job.MIN_FILE_SIZE)
    calls = []

    class FakeDownloader:
        errors: list = []
        progress_handler = type("P", (), {})()

        def __init__(self, settings):
            pass

        def download_multiple_songs(self, songs):
            calls.append(songs[0].download_url)
            return [(songs[0], None if len(calls) == 1 else good)]

    monkeypatch.setattr(spotdl_downloader, "Downloader", FakeDownloader)
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    monkeypatch.setattr(job, "original_soundcloud_files", lambda songs: {"spotify:1": "https://soundcloud.com/a/b"})
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path)
    results = job.download_songs([song], options, lambda *a: None)
    assert calls == ["https://cdn.example/original.wav", "https://soundcloud.com/a/b"]
    assert results == [(song, good)]


# --- typed tracks and track data ---------------------------------------------

SPOTIFY_RAW = {
    "name": "CULTURE",
    "artists": [{"name": "Aarne"}],
    "album": {"name": "CULTURE EP", "release_date": "2024-05-17", "total_tracks": 4, "images": [{"url": "c.jpg", "width": 640}]},
    "track_number": 2,
    "duration_ms": 185000,
}


def patch_spotify(monkeypatch):
    from musicdl import job, metadata

    monkeypatch.setattr(job, "init_spotify_quietly", lambda options, info: True)
    monkeypatch.setattr(metadata, "find_on_spotify", lambda song: metadata.track_info(SPOTIFY_RAW))
    monkeypatch.setattr(metadata, "find_free_text", lambda text: metadata.track_info(SPOTIFY_RAW) if "culture" in text.lower() else None)


def test_typed_tracks_get_full_data_and_are_searched(monkeypatch, tmp_path):
    from musicdl import job

    patch_spotify(monkeypatch)
    found = make_result("c1", "CULTURE", ["Aarne"], 185)
    messages = []
    summary = job.run_job(
        job.JobOptions(source_kind="text", source="aarne - culture\nculture\nnonsense words", out_dir=tmp_path, dry_run=True, cache_path=None),
        job.JobEvents(info=messages.append),
        provider_factory=lambda: StubProvider({"aarne - culture": [found]}),
        ffmpeg_check=lambda i: None,
    )
    assert summary.total == 1  # both lines are the same track, the third is not understood
    match = summary.matches[0]
    assert match.found and match.song.name == "CULTURE" and match.song.album_name == "CULTURE EP"
    assert match.song.year == 2024 and match.song.cover_url == "c.jpg" and match.song.track_number == 2
    assert any("пропустил строк: 1" in m for m in messages)


def test_empty_typed_text_is_an_error(tmp_path):
    from musicdl import job

    with pytest.raises(job.JobError):
        job.run_job(job.JobOptions(source_kind="text", source="  \n ", out_dir=tmp_path), provider_factory=lambda: StubProvider({}))


def test_csv_tracks_get_missing_tags_from_spotify(monkeypatch, tmp_path):
    from musicdl import job

    patch_spotify(monkeypatch)
    csv_file = tmp_path / "t.csv"
    csv_file.write_text("Track name,Artist name\nCULTURE,Aarne\n", encoding="utf-8")
    downloaded = []

    def downloader(songs, options, on_status):
        downloaded.extend(songs)
        return fake_download(songs, options, on_status)

    options = job.JobOptions(source_kind="csv", source=str(csv_file), out_dir=tmp_path / "out", cache_path=None)
    found = make_result("c1", "CULTURE", ["Aarne"], 185)
    job.run_job(options, provider_factory=lambda: StubProvider({"aarne - culture": [found]}), downloader=downloader, ffmpeg_check=lambda i: None)
    song = downloaded[0]
    assert song.album_name == "CULTURE EP" and song.year == 2024 and song.cover_url == "c.jpg"
    assert song.track_number == 2 and song.tracks_count == 4  # CSV placeholders replaced
    assert song.name == "CULTURE" and song.url.startswith("https://musicdl.local/")  # identity unchanged


def test_album_falls_back_to_source_album_then_title():
    from musicdl.csv_import import TrackRow, row_to_song
    from musicdl.matching import MatchResult
    from musicdl.pipeline import prepare_for_download

    plain = row_to_song(TrackRow(line=2, title="Single", artists=["X"]))
    with_source = row_to_song(TrackRow(line=3, title="Song", artists=["X"]))
    songs = prepare_for_download(
        [
            MatchResult(song=plain, url="https://www.youtube.com/watch?v=a"),
            MatchResult(song=with_source, url="https://www.youtube.com/watch?v=b", album="Source Album"),
        ]
    )
    assert [s.album_name for s in songs] == ["Single", "Source Album"]
