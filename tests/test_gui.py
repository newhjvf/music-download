import functools
import os
import time

import pytest

tk = pytest.importorskip("tkinter")

from musicdl import gui, job  # noqa: E402

from .stubs import StubProvider, make_result  # noqa: E402
from .test_job import SAMPLE, fake_download  # noqa: E402


@pytest.mark.parametrize(
    "message,percent,text",
    [("Downloading", 40, "загрузка 40%"), ("Done", 100, "✔ скачано"), ("Error", 0, "✖ ошибка загрузки"), ("Other", 0, "Other")],
)
def test_human_status(message, percent, text):
    assert gui.human_status(message, percent) == text


@pytest.fixture
def root(tmp_path, monkeypatch):
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        pytest.skip("no display")
    monkeypatch.setattr(gui, "SETTINGS_FILE", tmp_path / "settings.json")
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    window.withdraw()
    yield window
    window.destroy()
    assert not errors, errors


def wait(app, root, timeout=10):
    end = time.time() + timeout
    while app.worker and app.worker.is_alive() and time.time() < end:
        root.update()
        time.sleep(0.02)
    for _ in range(20):
        root.update()
        time.sleep(0.02)


def test_window_download_flow(root, tmp_path, monkeypatch):
    other = [make_result("other", "Another One Bites the Dust", ["Queen"], 215)]  # a source that answers, just not with the track
    results = {
        "queen - bohemian rhapsody": [make_result("bohe", "Bohemian Rhapsody", ["Queen"], 355)],
        "nirvana - smells like teen spirit": other,
        "кино - кукушка": other,
        "queen, david bowie - under pressure": other,
        "queen - under pressure": other,
    }
    run = functools.partial(
        job.run_job, provider_factory=lambda: StubProvider(results), downloader=fake_download, ffmpeg_check=lambda i: None
    )
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: None)
    app = gui.App(root, run_job=run)
    app.csv_path.set(str(SAMPLE))
    app.out_dir.set(str(tmp_path / "music"))
    app.start(dry_run=False)
    wait(app, root)

    statuses = [app.table.set(item, "status") for item in app.table.get_children()]
    assert statuses == ["✔ скачано", "✖ не найдено", "✖ не найдено", "✖ не найдено"]
    assert (tmp_path / "music" / "Queen - Bohemian Rhapsody.mp3").exists()
    assert "скачано 1 из 4" in app.status.get()
    assert str(app.report_button.cget("state")) == "normal"
    assert (tmp_path / "settings.json").exists()


def test_validation_messages(root, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, msg: shown.append(msg))
    app = gui.App(root, run_job=lambda *a, **k: None)
    app.mode.set("csv")
    app.csv_path.set("")
    app.start(dry_run=True)
    app.mode.set("url")
    app.link.set("https://example.com")
    app.start(dry_run=True)
    assert "CSV" in shown[0] and "Spotify" in shown[1]
    assert app.worker is None


def test_update_splash_status_from_worker_thread(root, tmp_path, monkeypatch):
    """Regression: status texts come from a worker thread; Tk must only be
    touched from the main thread ("main thread is not in main loop")."""
    from musicdl import updater

    shown = []
    monkeypatch.setattr(updater, "install_root", lambda *a: tmp_path)

    def fake_check(status):
        status("Скачиваю обновление…")
        time.sleep(0.2)
        status("Устанавливаю новые компоненты…")
        return False

    monkeypatch.setattr(updater, "check_and_update", fake_check)
    original_set = tk.StringVar.set

    def recording_set(self, value):
        shown.append(value)
        return original_set(self, value)

    monkeypatch.setattr(tk.StringVar, "set", recording_set)
    errors = []
    monkeypatch.setattr(gui.logging.getLogger("musicdl"), "exception", lambda *a, **k: errors.append(a))
    assert gui.run_update_check(root) is False
    assert errors == []
    assert "Скачиваю обновление…" in shown and "Устанавливаю новые компоненты…" in shown


def test_update_splash_restarts_after_update(root, tmp_path, monkeypatch):
    from musicdl import updater

    monkeypatch.setattr(updater, "install_root", lambda *a: tmp_path)
    monkeypatch.setattr(updater, "check_and_update", lambda status: True)
    started = []
    monkeypatch.setattr(gui.subprocess, "Popen", lambda args, cwd=None: started.append((args, cwd)))
    assert gui.run_update_check(root) is True
    assert started and started[0][0][-1] == "--updated" and started[0][1] == str(tmp_path)


def test_offline_banner_and_retry_row(root, tmp_path, monkeypatch):
    app = gui.App(root, run_job=lambda *a, **k: None)
    app._on_connection(False)
    assert "Нет интернета" in app.status.get()
    app._on_info("Ищу треки…")
    assert "Нет интернета" in app.status.get()  # the warning stays visible
    app._on_connection(True)
    assert "снова есть" in app.status.get()

    from musicdl.csv_import import TrackRow, row_to_song

    song = row_to_song(TrackRow(line=2, title="A", artists=["X"]))
    app.counters["missing"].set("0")
    app.counters["done"].set("0")
    app._on_songs([song], [])
    app._on_phase("download", 1)
    app._on_download(song.url, "Error", 0)
    assert app.counters["missing"].get() == "1"
    app._on_download(song.url, "Retry", 0)
    assert app.counters["missing"].get() == "0"
    app._on_download(song.url, "Done", 100)
    item = app.rows[song.url]
    assert app.table.set(item, "status") == "✔ скачано"
    assert app.counters["done"].get() == "1"


def test_text_mode_validation_options_and_settings(root, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, msg: shown.append(msg))
    app = gui.App(root, run_job=lambda *a, **k: None)
    app.mode.set("text")
    app._on_mode_change()
    app.start(dry_run=True)
    assert "названия треков" in shown[0] and app.worker is None
    app.query_box.insert("1.0", "Aarne - CULTURE\nBaby Cute - hooligang")
    assert app._validate() is None
    options = app.options(dry_run=True)
    assert options.source_kind == "text" and options.source == "Aarne - CULTURE\nBaby Cute - hooligang"
    assert int(app.threads.get()) == 3
    app._save()
    assert "Aarne - CULTURE" in gui.load_settings()["query"]
