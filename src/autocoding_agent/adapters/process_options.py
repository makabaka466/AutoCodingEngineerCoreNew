"""Cross-platform subprocess options shared by runtime integrations."""

from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_window_options() -> dict[str, Any]:
    """Prevent a child process from opening a console window on Windows."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }
