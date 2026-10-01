"""Window with buttons (tkinter, ships with Python on Windows).

Start: double-click ``start.bat`` or run ``musicdl-gui``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from musicdl import __version__

APP_DIR = Path.home() / ".musicdl"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "log.txt"
DEFAULT_OUT = Path.home() / "Music" / "musicdl"

BITRATES = ["128k", "192k", "256k", "320k"]

# spotDL progress messages -> what the user sees
DOWNLOAD_STATUS = {
    "Searching": "поиск…",
    "Getting metadata": "подготовка…",
    "Downloading": "загрузка",
    "Converting": "конвертация в mp3…",
    "Embedding metadata": "запись тегов…",
    "Done": "✔ скачано",
    "Skipped": "✔ уже есть",
    "Error": "✖ ошибка загрузки",
}


def human_status(message: str, percent: int) -> str:
    text = DOWNLOAD_STATUS.get(message, message or "загрузка")
    if text == "загрузка" and 0 < percent < 100:
        text = f"загрузка {percent}%"
    return text


def load_settings() -> Dict[str, Any]:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_path(path: Path) -> None:
    """Open a folder/file with the system's default program."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # pylint: disable=no-member
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    """The main window. Work runs in a background thread; it sends events
    through ``self.events`` and the UI applies them in ``_poll``."""

    def __init__(self, root: tk.Tk, run_job: Optional[Callable] = None) -> None:
        from musicdl.job import run_job as default_run_job

        self.root = root
        self.run_job = run_job or default_run_job
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.rows: Dict[str, str] = {}  # song.url -> tree item
        self.links: Dict[str, str] = {}  # tree item -> YouTube URL
        self.searched = self.search_total = 0
        self.report: Optional[Path] = None

        settings = load_settings()
        self.mode = tk.StringVar(value=settings.get("mode", "csv"))
        self.csv_path = tk.StringVar(value=settings.get("csv_path", ""))
        self.link = tk.StringVar(value=settings.get("link", ""))
        self.out_dir = tk.StringVar(value=settings.get("out_dir", str(DEFAULT_OUT)))
        self.bitrate = tk.StringVar(value=settings.get("bitrate", "320k"))
        self.threads = tk.IntVar(value=int(settings.get("threads", 4)))
        self.only_verified = tk.BooleanVar(value=bool(settings.get("only_verified", False)))
        self.status = tk.StringVar(value="Выберите CSV-файл или вставьте ссылку Spotify, затем нажмите «Скачать».")

        root.title(f"musicdl {__version__} — скачивание музыки")
        root.minsize(760, 520)
        root.geometry("980x640")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._on_mode_change()
        self.root.after(100, self._poll)

    # ----------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        source = ttk.LabelFrame(main, text=" 1. Откуда брать треки ", padding=8)
        source.grid(row=0, column=0, columnspan=3, sticky="ew", **pad)
        source.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            source, text="CSV-файл из TuneMyMusic", value="csv", variable=self.mode, command=self._on_mode_change
        ).grid(row=0, column=0, sticky="w")
        self.csv_entry = ttk.Entry(source, textvariable=self.csv_path)
        self.csv_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.csv_button = ttk.Button(source, text="Выбрать файл…", command=self.choose_csv)
        self.csv_button.grid(row=0, column=2, sticky="ew")

        ttk.Radiobutton(
            source, text="Ссылка Spotify", value="url", variable=self.mode, command=self._on_mode_change
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.link_entry = ttk.Entry(source, textvariable=self.link)
        self.link_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.paste_button = ttk.Button(source, text="Вставить", command=self.paste_link)
        self.paste_button.grid(row=1, column=2, sticky="ew", pady=(6, 0))

        target = ttk.LabelFrame(main, text=" 2. Куда сохранять ", padding=8)
        target.grid(row=1, column=0, columnspan=3, sticky="ew", **pad)
        target.columnconfigure(0, weight=1)
        ttk.Entry(target, textvariable=self.out_dir).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(target, text="Выбрать папку…", command=self.choose_out_dir).grid(row=0, column=1)

        options = ttk.Frame(target)
        options.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(options, text="Качество:").pack(side="left")
        ttk.Combobox(options, textvariable=self.bitrate, values=BITRATES, width=6, state="readonly").pack(
            side="left", padx=(4, 16)
        )
        ttk.Label(options, text="Одновременно:").pack(side="left")
        ttk.Spinbox(options, from_=1, to=8, textvariable=self.threads, width=4).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(
            options, text="Только официальные треки (без клипов и каверов)", variable=self.only_verified
        ).pack(side="left")

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", **pad)
        self.check_button = ttk.Button(actions, text="🔍 Проверить (без скачивания)", command=lambda: self.start(True))
        self.check_button.pack(side="left")
        self.download_button = ttk.Button(actions, text="⬇  Скачать", command=lambda: self.start(False))
        self.download_button.pack(side="left", padx=8)
        ttk.Label(actions, text="«Проверить» показывает, что найдётся, ничего не скачивая.", foreground="#666").pack(
            side="left", padx=8
        )

        table_frame = ttk.Frame(main)
        table_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", **pad)
        main.rowconfigure(3, weight=1)
        columns = ("n", "track", "found", "duration", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=12)
        for column, title, width, stretch in (
            ("n", "№", 40, False),
            ("track", "Трек", 280, True),
            ("found", "Найдено на YouTube Music", 300, True),
            ("duration", "Длит.", 60, False),
            ("status", "Статус", 150, False),
        ):
            self.table.heading(column, text=title)
            self.table.column(column, width=width, stretch=stretch, anchor="e" if column in ("n", "duration") else "w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.tag_configure("ok", foreground="#1a7f37")
        self.table.tag_configure("bad", foreground="#c62828")
        self.table.tag_configure("muted", foreground="#777")
        self.table.bind("<Double-1>", self._open_row_link)
        ttk.Label(main, text="Двойной щелчок по строке открывает найденное видео в браузере.", foreground="#666").grid(
            row=7, column=0, columnspan=3, sticky="w", padx=8
        )

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(main, textvariable=self.status, wraplength=900).grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        bottom = ttk.Frame(main)
        bottom.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Button(bottom, text="📂 Открыть папку с музыкой", command=self.open_out_dir).pack(side="left")
        self.report_button = ttk.Button(
            bottom, text="Открыть список ненайденных", command=self.open_report, state="disabled"
        )
        self.report_button.pack(side="left", padx=8)
        ttk.Button(bottom, text="Журнал ошибок", command=lambda: self._open_if_exists(LOG_FILE)).pack(side="right")

    def _on_mode_change(self) -> None:
        csv_mode = self.mode.get() == "csv"
        self.csv_entry.configure(state="normal" if csv_mode else "disabled")
        self.csv_button.configure(state="normal" if csv_mode else "disabled")
        self.link_entry.configure(state="disabled" if csv_mode else "normal")
        self.paste_button.configure(state="disabled" if csv_mode else "normal")

    # ---------------------------------------------------------------- actions
    def choose_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="CSV-файл из TuneMyMusic", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if path:
            self.csv_path.set(path)

    def paste_link(self) -> None:
        try:
            self.link.set(self.root.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo("musicdl", "В буфере обмена нет текста. Скопируйте ссылку в Spotify: «Поделиться → Копировать ссылку».")

    def choose_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Куда сохранять музыку", initialdir=self.out_dir.get() or str(Path.home()))
        if path:
            self.out_dir.set(path)

    def open_out_dir(self) -> None:
        out = Path(self.out_dir.get())
        out.mkdir(parents=True, exist_ok=True)
        open_path(out)

    def open_report(self) -> None:
        if self.report:
            self._open_if_exists(self.report)

    def _open_if_exists(self, path: Path) -> None:
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo("musicdl", f"Файла пока нет:\n{path}")

    def _open_row_link(self, _event) -> None:
        link = self.links.get(self.table.focus())
        if link:
            import webbrowser

            webbrowser.open(link)

    def _validate(self) -> Optional[str]:
        if self.mode.get() == "csv":
            path = self.csv_path.get().strip()
            if not path:
                return "Выберите CSV-файл (кнопка «Выбрать файл…»)."
            if not Path(path).is_file():
                return f"Файл не найден:\n{path}"
        else:
            from musicdl.spotify_input import SpotifyInputError, check_url

            try:
                check_url(self.link.get())
            except SpotifyInputError:
                return (
                    "Это не похоже на ссылку Spotify на трек, альбом или плейлист.\n"
                    "Скопируйте её в Spotify: «Поделиться → Копировать ссылку»."
                )
        if not self.out_dir.get().strip():
            return "Выберите папку для музыки."
        try:
            if not 1 <= int(self.threads.get()) <= 8:
                raise ValueError
        except (tk.TclError, ValueError):
            return "«Одновременно» — число от 1 до 8."
        return None

    def options(self, dry_run: bool):
        from musicdl.job import JobOptions

        csv_mode = self.mode.get() == "csv"
        return JobOptions(
            source_kind="csv" if csv_mode else "url",
            source=self.csv_path.get().strip() if csv_mode else self.link.get().strip(),
            out_dir=Path(self.out_dir.get().strip()),
            threads=int(self.threads.get()),
            bitrate=self.bitrate.get(),
            dry_run=dry_run,
            only_verified=bool(self.only_verified.get()),
            env_file=Path.cwd() / ".env",
        )

    def start(self, dry_run: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        problem = self._validate()
        if problem:
            messagebox.showwarning("musicdl", problem)
            return
        self._save()
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        self.links.clear()
        self.report = None
        self.report_button.configure(state="disabled")
        self.progress.configure(value=0, maximum=1)
        self._set_busy(True)
        self.status.set("Запуск…")

        options = self.options(dry_run)
        self.worker = threading.Thread(target=self._work, args=(options,), daemon=True)
        self.worker.start()

    def _work(self, options) -> None:
        from musicdl.job import JobError, JobEvents

        post = self.events.put
        events = JobEvents(
            info=lambda message: post(("info", message)),
            songs_loaded=lambda todo, existing: post(("songs", todo, existing)),
            matched=lambda match: post(("match", match, options.dry_run)),
            download_status=lambda key, status, percent: post(("download", key, status, percent)),
            phase=lambda name, count: post(("phase", name, count)),
        )
        try:
            summary = self.run_job(options, events)
        except JobError as exc:
            post(("error", str(exc), None))
        except Exception as exc:  # unexpected: show message, keep details in the log
            logging.getLogger("musicdl").error("Unexpected error", exc_info=True)
            post(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
        else:
            post(("done", summary, options.dry_run))

    # ------------------------------------------------------------ UI updates
    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                getattr(self, f"_on_{event[0]}")(*event[1:])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_info(self, message: str) -> None:
        self.status.set(message)

    def _on_songs(self, todo: List, existing: List) -> None:
        from musicdl.pipeline import expected_path

        number = 0
        for song in existing:
            number += 1
            item = self.table.insert(
                "", "end", values=(number, f"{song.artist} - {song.name}", expected_path(song, Path(self.out_dir.get())).name, "", "✔ уже есть"), tags=("muted",)
            )
            self.rows[song.url] = item
        for song in todo:
            number += 1
            self.rows[song.url] = self.table.insert(
                "", "end", values=(number, f"{song.artist} - {song.name}", "", "", "в очереди")
            )

    def _on_match(self, match, dry_run: bool) -> None:
        from musicdl.pipeline import format_duration

        item = self.rows.get(match.song.url)
        self.progress.step(1)
        self.searched += 1
        self.status.set(f"Ищу на YouTube Music: {self.searched} из {self.search_total}…")
        if item is None:
            return
        if match.found:
            found = f"{match.author} - {match.title}" if match.title else match.url
            if not match.verified:
                found += "  (видео)"
            self.links[item] = match.url
            self.table.item(
                item,
                values=(self.table.set(item, "n"), self.table.set(item, "track"), found, format_duration(match.duration), "✔ найдено" if dry_run else "ждёт загрузки"),
                tags=("ok",) if dry_run else (),
            )
        else:
            not_found = match.error == "not found"
            reason = "✖ не найдено" if not_found else "✖ ошибка поиска"
            detail = "—" if not_found else (match.error or "")
            self.table.item(
                item,
                values=(self.table.set(item, "n"), self.table.set(item, "track"), detail, "", reason),
                tags=("bad",),
            )

    def _on_download(self, key: str, status: str, percent: int) -> None:
        item = self.rows.get(key)
        if item is None:
            return
        text = human_status(status, percent)
        if self.table.set(item, "status") in ("✔ скачано", "✖ ошибка загрузки") and status not in ("Done", "Error"):
            return
        tags = ("ok",) if status in ("Done", "Skipped") else ("bad",) if status == "Error" else ()
        if status in ("Done", "Error") and self.table.set(item, "status") not in ("✔ скачано", "✖ ошибка загрузки"):
            self.progress.step(1)
        self.table.set(item, "status", text)
        self.table.item(item, tags=tags)

    def _on_phase(self, name: str, count: int) -> None:
        self.progress.configure(value=0, maximum=max(1, count))
        if name == "search":
            self.searched, self.search_total = 0, count

    def _on_done(self, summary, dry_run: bool) -> None:
        self._set_busy(False)
        self.progress.configure(value=float(self.progress.cget("maximum")))
        failed = len(summary.failed)
        if dry_run:
            text = f"Проверка закончена: найдено {summary.succeeded} из {summary.processed}."
        else:
            text = f"Готово: скачано {summary.succeeded} из {summary.processed}."
        if summary.existing:
            text += f" Уже были скачаны раньше: {summary.existing}."
        if failed:
            text += f" Не получилось: {failed} — см. «Открыть список ненайденных»."
        self.status.set(text)
        self.report = summary.report
        self.report_button.configure(state="normal" if summary.report else "disabled")
        if failed and summary.processed and failed == summary.processed:
            messagebox.showwarning(
                "musicdl",
                "Не удалось найти или скачать ни одного трека.\n\n"
                "Чаще всего это значит, что YouTube временно ограничил доступ. "
                "Попробуйте позже, включите VPN или уменьшите «Одновременно» до 1–2.",
            )

    def _on_error(self, message: str, details: Optional[str]) -> None:
        self._set_busy(False)
        self.status.set(f"Ошибка: {message}")
        extra = f"\n\nПодробности записаны в журнал:\n{LOG_FILE}" if details else ""
        messagebox.showerror("musicdl", message + extra)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.check_button.configure(state=state)
        self.download_button.configure(state=state)
        self.root.configure(cursor="watch" if busy else "")

    def _save(self) -> None:
        save_settings(
            {
                "mode": self.mode.get(),
                "csv_path": self.csv_path.get(),
                "link": self.link.get(),
                "out_dir": self.out_dir.get(),
                "bitrate": self.bitrate.get(),
                "threads": int(self.threads.get()),
                "only_verified": bool(self.only_verified.get()),
            }
        )

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("musicdl", "Идёт работа. Закрыть программу? Недокачанные треки можно докачать потом."):
                return
        self._save()
        self.root.destroy()


def _setup_io() -> None:
    """pythonw.exe has no console: send output and logs to ~/.musicdl/log.txt."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    if sys.stdout is None or sys.stderr is None or "pythonw" in Path(sys.executable).name.lower():
        sys.stdout = log
        sys.stderr = log
    logging.basicConfig(
        stream=log,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("spotdl", "yt_dlp", "urllib3", "ytmusicapi"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _enable_hidpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # older Windows
            pass


def main() -> int:
    _setup_io()
    _enable_hidpi()
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
