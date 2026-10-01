import socket
import threading
import time

import pytest

from musicdl.job import JobEvents, JobOptions, run_job
from musicdl.matching import find_matches
from musicdl.network import ConnectionGuard, looks_like_network_error
from musicdl.pipeline import MIN_FILE_SIZE, expected_path, split_existing

from .stubs import StubProvider, make_result
from .test_job import FAKE_MP3, RESULTS, SAMPLE, fake_download, options


class Flaky:
    """Online state that tests can switch; counts the checks."""

    def __init__(self, online=True):
        self.online = online
        self.checks = 0

    def __call__(self):
        self.checks += 1
        return self.online


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("x"),
        socket.gaierror(11001, "getaddrinfo failed"),
        TimeoutError(),
        RuntimeError("HTTPSConnectionPool(host='music.youtube.com'): Max retries exceeded"),
        RuntimeError("Failed to complete request."),
    ],
)
def test_network_errors_recognised(exc):
    assert looks_like_network_error(exc)


def test_other_errors_not_network():
    assert not looks_like_network_error(ValueError("bad csv"))
    assert not looks_like_network_error(KeyError("videoDetails"))


def test_chained_network_error_recognised():
    try:
        try:
            raise ConnectionResetError("reset")
        except ConnectionResetError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        assert looks_like_network_error(exc)


def test_guard_waits_and_reports_once():
    net = Flaky(online=False)
    changes = []
    guard = ConnectionGuard(changes.append, check=net, interval=0.02)
    threading.Timer(0.1, lambda: setattr(net, "online", True)).start()
    results = []
    threads = [threading.Thread(target=lambda: results.append(guard.wait_until_online())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)
    assert results == [True, True, True]
    assert changes == [False, True]  # one notice each way, not one per thread


def test_guard_cancel_stops_waiting():
    cancel = threading.Event()
    guard = ConnectionGuard(cancel=cancel, check=lambda: False, interval=0.02)
    threading.Timer(0.05, cancel.set).start()
    assert guard.wait_until_online() is False


class DropsOnce(StubProvider):
    """Fails like a lost connection for the first N searches."""

    failures = 0

    def get_results(self, term, **kw):
        if DropsOnce.failures > 0:
            DropsOnce.failures -= 1
            raise ConnectionError("Network is unreachable")
        return super().get_results(term, **kw)


def test_search_waits_for_connection_and_retries():
    DropsOnce.failures = 1
    net = Flaky(online=False)
    threading.Timer(0.1, lambda: setattr(net, "online", True)).start()
    changes = []
    guard = ConnectionGuard(changes.append, check=net, interval=0.02)
    hit = make_result("good", "Smells Like Teen Spirit", ["Nirvana"], 301)
    from musicdl.csv_import import TrackRow, row_to_song

    song = row_to_song(TrackRow(line=2, title="Smells Like Teen Spirit", artists=["Nirvana"]))
    [match] = find_matches(
        [song], 1, lambda: DropsOnce({"nirvana - smells like teen spirit": [hit]}), guard=guard
    )
    assert match.url == hit.url
    assert changes == [False, True]


def test_download_retried_after_connection_drop(tmp_path):
    calls = []

    def drops_first_time(songs, opts, on_status):
        calls.append([s.name for s in songs])
        if len(calls) == 1:
            out = []
            for i, song in enumerate(songs):
                if i == 0:  # first one completes, the rest are cut off
                    path = expected_path(song, opts.out_dir)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(FAKE_MP3)
                    out.append((song, path))
                else:
                    partial = expected_path(song, opts.out_dir)
                    partial.write_bytes(b"ID3partial")
                    out.append((song, None))
            return out
        return fake_download(songs, opts, on_status)

    statuses = []
    summary = run_job(
        options(tmp_path),
        JobEvents(download_status=lambda key, status, pct: statuses.append(status)),
        provider_factory=lambda: StubProvider(RESULTS),
        downloader=drops_first_time,
        ffmpeg_check=lambda info: None,
        online_check=lambda: True,
    )
    assert calls == [["Bohemian Rhapsody", "Smells Like Teen Spirit"], ["Smells Like Teen Spirit"]]
    assert "Retry" in statuses
    assert summary.succeeded == 2
    assert all(p.stat().st_size >= MIN_FILE_SIZE for p in tmp_path.glob("*.mp3"))


def test_download_failure_after_retries_leaves_no_partial_file(tmp_path):
    def always_partial(songs, opts, on_status):
        out = []
        for song in songs:
            path = expected_path(song, opts.out_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ID3")
            out.append((song, path))
        return out

    summary = run_job(
        options(tmp_path),
        provider_factory=lambda: StubProvider(RESULTS),
        downloader=always_partial,
        ffmpeg_check=lambda info: None,
        online_check=lambda: True,
    )
    assert summary.succeeded == 0 and len(summary.failed) == 4
    assert list(tmp_path.glob("*.mp3")) == []


def test_partial_files_are_redownloaded(tmp_path):
    from musicdl.csv_import import TrackRow, row_to_song

    song = row_to_song(TrackRow(line=2, title="A", artists=["X"]))
    expected_path(song, tmp_path).write_bytes(b"ID3 cut")
    todo, existing = split_existing([song], tmp_path)
    assert todo == [song] and existing == []
    assert not expected_path(song, tmp_path).exists()


def test_spotify_list_loading_retried(tmp_path, monkeypatch):
    import musicdl.spotify_input as spotify_input

    attempts = []

    def flaky_songs(url, threads):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("Failed to complete request.")
        return []

    monkeypatch.setattr(spotify_input, "init_spotify", lambda *a: False)
    monkeypatch.setattr(spotify_input, "load_credentials", lambda *a: (None, None))
    monkeypatch.setattr(spotify_input, "songs_from_url", flaky_songs)
    net = Flaky(online=False)
    threading.Timer(0.05, lambda: setattr(net, "online", True)).start()
    import musicdl.network as network

    monkeypatch.setattr(network, "CHECK_INTERVAL", 0.02)
    opts = JobOptions(source_kind="url", source="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", out_dir=tmp_path)
    from musicdl.job import JobError

    with pytest.raises(JobError, match="ни одного трека"):  # empty list after the successful retry
        run_job(opts, online_check=net)
    assert len(attempts) == 2
