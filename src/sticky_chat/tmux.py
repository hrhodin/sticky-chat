"""Everything that talks to tmux goes through here."""

from __future__ import annotations

import os
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
            raise RuntimeError(
                f"tmux {' '.join(args)}: {proc.stderr.strip() or proc.returncode}")
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

    def capture(self, pane: str, start: int, end: int) -> list[str]:
        """Rows as displayed. Coordinates: 0 = top visible row, negative = history."""
        return self.capture_with(pane, start, end, "")[0]

    def capture_with(self, pane: str, start: int, end: int,
                     template: str) -> tuple[list[str], str]:
        """The rows, and one format read taken straight after them.

        Both in the same invocation: the second question is free, and asking
        it here rather than in a call of its own leaves almost no room for a
        scroll to land between the two. The format prints one line and prints
        it last, so it is the last line of the output.
        """
        if end < start:
            return [], (self.fmt(pane, template) if template else "")
        commands = [["capture-pane", "-p", "-t", pane,
                     "-S", str(start), "-E", str(end)]]
        if template:
            commands.append(["display-message", "-p", "-t", pane, template])
        rows = self.many(*commands).split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        after = rows.pop() if (template and rows) else ""
        return [r.rstrip() for r in rows], after
