"""Everything that talks to tmux goes through here."""

from __future__ import annotations

import os
import re
import subprocess
import sys

DEFAULT_SOCKET = "sticky"


def die(message: str) -> None:
    """Say what is wrong and stop. Here rather than in util, which imports
    nothing from here and should stay that way."""
    print(f"sticky: {message}", file=sys.stderr)
    raise SystemExit(1)
SESSION_NAME = "sticky"

# --------------------------------------------------------------------- tmux

MIN_TMUX = (3, 7)               # floating panes, which every prompt is


def tmux_version(text: str) -> tuple[int, ...]:
    """The version out of `tmux -V` or `#{version}`, as numbers.

    Releases are `tmux 3.7c`; a development build says `tmux next-3.8`. The
    letter is a patch level and the `next-` prefix means newer than the
    release it names, so both are safely read as their leading numbers.
    Empty when there is nothing to read, which callers take as "fine": a
    version string nobody can parse is no reason to refuse to start.
    """
    found = re.search(r"(\d+)\.(\d+)", text)
    return tuple(int(part) for part in found.groups()) if found else ()


def too_old(text: str) -> bool:
    version = tmux_version(text)
    return bool(version) and version < MIN_TMUX


def old_tmux(socket: str | None = None) -> str:
    """What is wrong with the tmux sticky would run on, or "" if nothing.

    Two versions, because they can differ: the `tmux` on PATH, and the
    server already running on the socket. Installing a newer tmux leaves
    the old server up, and every command then reaches a server without
    floating panes - so a tmux -V that says 3.7c is no proof on its own.
    An old tmux does not fail at the start; it fails at the first prompt, as
    `returned 1` and nothing else, which is why this is said up front.
    """
    need = ".".join(str(n) for n in MIN_TMUX)
    tm = Tmux(socket)
    try:
        client = subprocess.run(["tmux", "-V"], capture_output=True,
                                text=True).stdout.strip()
    except FileNotFoundError:
        return ""                       # Tmux.run says that one itself
    if too_old(client):
        return (f"{client} is older than tmux {need}, which sticky needs "
                f"for floating panes: no note prompt, no new-tab prompt, no "
                f"reopen picker. A newer tmux built into ~/.local works "
                f"without root - see Requirements in the README.")
    if tm.server_running():
        try:
            server = tm.run("display-message", "-p", "#{version}").strip()
        except RuntimeError:
            return ""
        if too_old(server):
            return (f"the sticky server already running is tmux {server}, "
                    f"older than {need}, though {client} is on your PATH. "
                    f"It was started before the newer tmux was installed; "
                    f"`sticky-chat quit` and start again to move onto it.")
    return ""


def why(said: str) -> str:
    """What to do about it, for the tmux errors that have one answer.

    Only the ones where the message names a cause the user cannot be
    expected to connect to a fix. "missing or unsuitable terminal" is the
    one that comes up: tmux will not start without a terminfo entry for
    $TERM, and a terminal whose entry ships with the terminal - Ghostty,
    kitty, WezTerm - has none on the machine you just ssh'd into. The
    message on its own reads as "tmux is broken here", which is why this
    says the two ways out instead.
    """
    if "missing or unsuitable terminal" not in said:
        return ""
    term = os.environ.get("TERM", "") or "your TERM"
    return (f"\n  no terminfo entry for {term} here, and tmux will not start "
            f"without one. Any of:\n"
            f"    TERM=xterm-256color sticky-chat ...   (works now, this "
            f"machine)\n"
            f"    infocmp -x {term} | ssh THIS-HOST -- tic -x -   (once, run "
            f"on\n"
            f"      the machine the terminal runs on - and on macOS that "
            f"entry is\n"
            f"      inside the app bundle, so TERMINFO=/Applications/"
            f"<Terminal>.app/\n"
            f"      Contents/Resources/terminfo has to go in front of "
            f"infocmp)\n"
            f"    let the terminal do it on connect: Ghostty's\n"
            f"      shell-integration-features = ssh-env,ssh-terminfo, or "
            f"kitty's\n"
            f"      kitten ssh")


def no_floating_panes(tm: Tmux) -> str:
    """One status-line sentence, if the server is too old for a prompt.

    Short, because it is read on tmux's message line rather than a
    terminal; `old_tmux` is the long form, said at startup.
    """
    try:
        server = tm.run("display-message", "-p", "#{version}").strip()
    except RuntimeError:
        return ""
    if not too_old(server):
        return ""
    need = ".".join(str(n) for n in MIN_TMUX)
    return (f"tmux {server} has no floating panes; sticky needs {need} or "
            f"newer (README: Requirements)")


class Tmux:
    def __init__(self, socket: str | None = None):
        self.socket = socket or os.environ.get("STICKY_SOCKET") or DEFAULT_SOCKET

    def _base(self) -> list[str]:
        flag = "-S" if "/" in self.socket else "-L"
        return ["tmux", flag, self.socket]

    def run(self, *args: str, input_text: str | None = None,
            check: bool = True) -> str:
        try:
            proc = subprocess.run(
                self._base() + list(args),
                input=input_text,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # The one dependency a Python package cannot declare, failing at
            # the first thing sticky asks of it. Said plainly here rather
            # than as a traceback about `tmux` from inside subprocess.
            die("tmux is not on your PATH. Install it with "
                "`brew install tmux`, `apt install tmux` or "
                "`dnf install tmux`; sticky-chat drives it and cannot "
                "run without it.")
        if check and proc.returncode != 0:
            said = proc.stderr.strip() or str(proc.returncode)
            raise RuntimeError(f"tmux {' '.join(args)}: {said}{why(said)}")
        return proc.stdout

    def ok(self, *args: str) -> bool:
        try:
            self.run(*args)
            return True
        except RuntimeError:
            return False

    def server_running(self) -> bool:
        return self.ok("has-session", "-t", SESSION_NAME)

    def fmt(self, target: str, template: str) -> str:
        return self.run("display-message", "-p", "-t", target, template).rstrip("\n")

    def pane_exists(self, pane: str) -> bool:
        """True when that pane is still on the server.

        The id that comes back is what settles it, not the exit status:
        asking about a pane that has gone is not an error in every tmux -
        from 3.8 it succeeds and prints nothing - and a check that reads the
        status alone would answer yes for a pane nobody can see. Every
        caller has a `%id` from `#{pane_id}`, so comparing is enough.
        """
        try:
            return self.run("display-message", "-p", "-t", pane,
                            "#{pane_id}").strip() == pane
        except RuntimeError:
            return False

    def option(self, pane: str, name: str) -> str:
        try:
            return self.run("show-options", "-pqv", "-t", pane, name).strip()
        except RuntimeError:
            return ""

    def set_option(self, pane: str, name: str, value: str):
        self.run("set-option", "-p", "-t", pane, name, value)

    def many(self, *commands: list[str]) -> str:
        """Several tmux commands in one invocation, output in order.

        What a tmux call costs is the process it starts - a capture of fifty
        rows and a one-word question both take about four milliseconds - so
        asking four things at once costs what asking one does. tmux takes the
        commands separated by a lone ";", and each prints its own output,
        with `display-message -p` always contributing exactly one line even
        when its target has gone. That is what makes the output splittable
        by position.
        """
        args: list[str] = []
        for command in commands:
            if args:
                args.append(";")
            args += command
        return self.run(*args)

    def formats(self, *asks: tuple[str, str]) -> list[str]:
        """One answer per (target, template), asked together."""
        if not asks:
            return []
        lines = self.many(
            *[["display-message", "-p", "-t", target, template]
              for target, template in asks]).split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines + [""] * (len(asks) - len(lines))

    def capture(self, pane: str, start: int, end: int,
                joined: bool = False) -> list[str]:
        """Rows as displayed. Coordinates: 0 = top visible row, negative = history.

        `joined` asks tmux to put a line that wrapped back together. Only
        tmux knows which rows are the continuation of one before them, and a
        sentence read across the break is one whose halves never meet -
        which is a note placed on the wrong row, or a time nobody finds. It
        is off by default because the rows a note is anchored to are the
        rows on the screen, and joining them renumbers the screen.
        """
        return self.capture_with(pane, start, end, "", joined)[0]

    def capture_with(self, pane: str, start: int, end: int,
                     template: str, joined: bool = False) -> tuple[list[str], str]:
        """The rows, and one format read taken straight after them.

        Both in the same invocation: the second question is free, and asking
        it here rather than in a call of its own leaves almost no room for a
        scroll to land between the two. The format prints one line and prints
        it last, so it is the last line of the output.
        """
        if end < start:
            return [], (self.fmt(pane, template) if template else "")
        commands = [["capture-pane", "-p", *(["-J"] if joined else []),
                     "-t", pane, "-S", str(start), "-E", str(end)]]
        if template:
            commands.append(["display-message", "-p", "-t", pane, template])
        rows = self.many(*commands).split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        after = rows.pop() if (template and rows) else ""
        return [r.rstrip() for r in rows], after
