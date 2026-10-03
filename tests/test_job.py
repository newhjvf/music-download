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


def test_tracks_are_downloaded_one_at_a_time(monkeypatch, tmp_path):
    from musicdl import job

    captured = {}

    class FakeDownloader:
        errors: list = []
        progress_handler = type("P", (), {})()

        def __init__(self, settings):
            captured.update(settings)

        def download_multiple_songs(self, songs):
            return []

    import spotdl.download.downloader as spotdl_downloader

    monkeypatch.setattr(spotdl_downloader, "Downloader", FakeDownloader)
    monkeypatch.setattr(job, "ensure_deno", lambda: None)
    monkeypatch.setattr(job, "log_download_causes", lambda: None)
    options = job.JobOptions(source_kind="csv", source="x", out_dir=tmp_path, threads=8)
    job.download_songs([object()], options, lambda *a: None)
    assert captured["threads"] == 1


def test_ensure_deno_never_raises(monkeypatch):
    import spotdl.utils.deno as deno
    from musicdl import job

    monkeypatch.setattr(deno, "is_deno_installed", lambda *a: False)
    monkeypatch.setattr(deno, "download_deno", lambda: (_ for _ in ()).throw(OSError("offline")))
    job.ensure_deno()  # logs a warning, does not raise
