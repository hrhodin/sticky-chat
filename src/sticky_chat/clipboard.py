"""Putting a selection on the system clipboard."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def clipboard_command() -> list[str] | None:
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def to_clipboard(text: str) -> bool:
    """Put the selection on the system clipboard as well as tmux's buffer.

    A drag that opens the note prompt should still have copied, so this runs
    before anything else can go wrong and never raises.
    """
    command = clipboard_command()
    if not command:
        return False
    try:
        subprocess.run(command, input=text, text=True, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
