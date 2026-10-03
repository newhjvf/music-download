"""No black console windows when spotDL / yt-dlp start ffmpeg.

The window runs under ``pythonw.exe`` (no console of its own), so every
console program it starts (ffmpeg.exe) opens its own console window."""

from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def hide_child_consoles(popen_class=subprocess.Popen, platform: str = sys.platform) -> bool:
    """Make every process started through ``popen_class`` (default
    ``subprocess.Popen``) run without a console window. Windows only,
    idempotent. Returns True if the patch is (now) in place."""
    if platform != "win32":
        return False
    if getattr(popen_class.__init__, "_musicdl_no_window", False):
        return True
    original = popen_class.__init__

    def init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    init._musicdl_no_window = True  # type: ignore[attr-defined]
    popen_class.__init__ = init  # type: ignore[method-assign]
    return True
