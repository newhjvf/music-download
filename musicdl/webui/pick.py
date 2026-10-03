"""Helper process: show a native file/folder dialog and write the choice to a file.

    python -m musicdl.webui.pick csv|folder INITIAL RESULT_FILE

Runs in its own process so Tk never lives in the web server (Tk must stay in
one thread, and the dialog has to come to the front of the browser window).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv) -> int:
    import tkinter as tk
    from tkinter import filedialog

    kind, initial, result_file = argv[0], argv[1], argv[2]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # otherwise the dialog can open behind the browser window
    start = initial if initial and Path(initial).exists() else str(Path.home())
    if kind == "csv":
        folder = start if Path(start).is_dir() else str(Path(start).parent)
        chosen = filedialog.askopenfilename(
            parent=root,
            title="CSV-файл из TuneMyMusic",
            initialdir=folder,
            filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
        )
    else:
        chosen = filedialog.askdirectory(parent=root, title="Куда сохранять музыку", initialdir=start)
    root.destroy()
    Path(result_file).write_text(chosen or "", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
