"""Fixtures shared by the suite.

The pure-function tests import sticky as a module. The integration tests drive
a real tmux server on a private socket, so they can run with no terminal
attached; they are marked `integration` and need tmux 3.7 or newer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "sticky-chat"

# One socket per run. The suite kills its server on the way in and out, so a
# fixed name would have two runs on one machine tearing down each other's
# session - which is easy to do while developing, and is what a CI runner
# with parallel jobs does by default.
SOCKET = f"stickytest-{os.getpid()}"
OUTER = f"stickyouter-{os.getpid()}"

# The script the annotated pane runs: a header, thirty numbered rows and a
# trailer, so a test can point at a line by its payload number.
PANE_SCRIPT = (
    r"printf 'HEADER alpha\n'; "
    r"for i in $(seq 1 30); do printf '  row %02d payload-%02d\n' $i $i; done; "
    r"printf 'TRAILER omega\n'; sleep 600"
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: drives a real tmux server (needs tmux 3.7+)")


@pytest.fixture(scope="session")
def sticky():
    """sticky-chat imported as a module, for the pure-function tests."""
    sys.path.insert(0, str(ROOT / "src"))
    import sticky_chat

    return sticky_chat


MIN_TMUX = (3, 7)               # floating panes, which the note prompt is


def tmux_version(text: str) -> tuple[int, ...]:
    """The version out of `tmux -V`, as numbers. Empty if it cannot be read.

    Releases are `tmux 3.7c`; a development build says `tmux next-3.8`. The
    letter is a patch level and the `next-` prefix means newer than the
    release it names, so both are safely read as their leading numbers.
    """
    found = re.search(r"(\d+)\.(\d+)", text)
    return tuple(int(part) for part in found.groups()) if found else ()


@pytest.fixture(scope="session")
def tmux_available():
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    out = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout
    version = tmux_version(out)
    if version and version < MIN_TMUX:
        pytest.skip(f"{out.strip()} is older than tmux "
                    f"{'.'.join(str(n) for n in MIN_TMUX)}, which the note "
                    f"prompt needs for floating panes")
    return out.strip()


@pytest.fixture(scope="session")
def tmux(tmux_available):
    """Run a tmux command on the test server and return its stdout."""

    def run(*args, check=True):
        done = subprocess.run(["tmux", "-L", SOCKET, *args],
                              capture_output=True, text=True)
        if check and done.returncode != 0:
            raise RuntimeError(f"tmux {args}: {done.stderr.strip()}")
        return done.stdout

    return run


@pytest.fixture(scope="session")
def sticky_home(tmp_path_factory):
    """Where notes and the generated config live for the whole run."""
    home = tmp_path_factory.mktemp("sticky-home")
    return home


@pytest.fixture(scope="session")
def run_sticky(sticky_home):
    """Invoke the CLI the way a user would, against the test server."""

    def run(*args, stdin=None, check=True, home=None):
        env = {**os.environ, "STICKY_HOME": str(home or sticky_home)}
        done = subprocess.run(
            [sys.executable, str(LAUNCHER), "--socket", SOCKET, *args],
            input=stdin, capture_output=True, text=True, env=env)
        if check and done.returncode != 0:
            raise RuntimeError(f"sticky {args}: {done.stderr.strip()}")
        return done.stdout

    return run


@pytest.fixture
def project(tmp_path):
    """A fresh project directory, so no two tests share a note store."""
    path = tmp_path / "project"
    path.mkdir()
    return str(path)


@pytest.fixture(scope="session")
def notes_of(sticky, sticky_home):
    """The notes belonging to exactly one project, wherever its store is.

    Several stores can name the same project - a fork keeps a copy of its own -
    so the store is found by path rather than by scanning for the first match.
    """

    def read(project, directory=None):
        if directory is None:
            local = Path(project) / ".sticky" / "notes.json"
            directory = local.parent if local.exists() else (
                Path(sticky_home) / sticky.project_slug(os.path.abspath(project)))
        path = Path(directory) / "notes.json"
        if not path.exists():
            return []
        return json.loads(path.read_text())["notes"]

    return read


@pytest.fixture(scope="session")
def server(tmux, sticky_home):
    """A tmux server with one pane of known output, killed at the end."""
    subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)
    tmux("new-session", "-d", "-s", "sticky", "-x", "80", "-y", "20",
         "sh", "-c", PANE_SCRIPT)
    # A key binding runs from the server's own environment, not from the one
    # `run_sticky` sets, so without this a test that drives a real binding
    # writes its notes into the developer's ~/.sticky.
    tmux("set-environment", "-g", "STICKY_HOME", str(sticky_home))
    time.sleep(0.8)
    yield tmux("display-message", "-p", "-t", "sticky:", "#{pane_id}").strip()
    subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)


@pytest.fixture(scope="session")
def pane_view(tmux, server):
    """The pane's absolute line numbers and its visible rows."""
    raw = tmux("display-message", "-p", "-t", server,
               "#{history_size}\t#{pane_height}").strip().split("\t")
    history, height = int(raw[0]), int(raw[1])
    rows = tmux("capture-pane", "-p", "-t", server, "-S", "0",
                "-E", str(height - 1)).rstrip("\n").split("\n")
    return {"pane": server, "history": history, "height": height, "rows": rows}


@pytest.fixture(scope="session")
def target(pane_view):
    """A stable single row in the pane, with its absolute line number."""
    rows = pane_view["rows"]
    index = next(i for i, r in enumerate(rows) if "payload-20" in r)
    return {"index": index, "abs": pane_view["history"] + index,
            "text": rows[index].rstrip()}
