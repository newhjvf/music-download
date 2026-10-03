import subprocess

from musicdl.winproc import CREATE_NO_WINDOW, hide_child_consoles


class FakePopen:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


def test_adds_no_window_flag_on_windows_once():
    assert hide_child_consoles(FakePopen, "win32")
    assert hide_child_consoles(FakePopen, "win32")  # idempotent: flag not applied twice
    assert FakePopen().kwargs["creationflags"] == CREATE_NO_WINDOW
    assert FakePopen(creationflags=0x200).kwargs["creationflags"] == 0x200 | CREATE_NO_WINDOW


def test_does_nothing_elsewhere():
    original = subprocess.Popen.__init__
    assert not hide_child_consoles(subprocess.Popen, "linux")
    assert subprocess.Popen.__init__ is original
