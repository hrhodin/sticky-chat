"""Where notes live, and which store a pane is showing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time

from .agents import CLAUDE, Agent, agent_named
from .tmux import Tmux
from .util import die

# ---------------------------------------------------------------- constants

STATE_HOME = os.path.expanduser(os.environ.get("STICKY_HOME", "~/.sticky"))


# -------------------------------------------------------------------- store


SESSION_FLAGS = CLAUDE.session_flags      # the default profile's, for callers


def names_a_session(command: str, agent: Agent = CLAUDE) -> bool:
    """True when the command line already says which conversation to open."""
    return any(word in agent.session_flags for word in command.split())


def windows_dir() -> str:
    return os.path.join(STATE_HOME, "windows")


def record_window(session: str, **fields) -> None:
    """Remember a tab so it can be opened again after the machine restarts.

    One file per tab: no central list to lock, and a tab that never comes
    back simply leaves its file behind for `resume` to offer or forget.
    """
    if not session:
        return
    path = os.path.join(windows_dir(), f"{session}.json")
    try:
        private_dir(windows_dir())
        record = {}
        if os.path.exists(path):
            with open(path) as fh:
                record = json.load(fh)
        record.update(session=session, last_seen=time.time(), **fields)
        record.setdefault("created", record["last_seen"])
        fd, tmp = tempfile.mkstemp(dir=windows_dir(), prefix=".win-")
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=1)
        os.replace(tmp, path)
    except (OSError, ValueError, json.JSONDecodeError):
        pass                            # remembering is a convenience, not a duty


def known_windows() -> list[dict]:
    """Every remembered tab, most recently seen first."""
    out = []
    try:
        names = os.listdir(windows_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(windows_dir(), name)) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("last_seen", 0), reverse=True)


def private_dir(path: str) -> str:
    """Make a directory only you can read.

    Notes quote whatever was on your screen, so they can hold anything a
    command printed. The files themselves are written through mkstemp and are
    already 0600; without this the directory names - which carry your project
    names - would still be listable by every other account on the machine.
    """
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass                              # a shared or mounted home may refuse
    return path


def project_slug(path: str) -> str:
    path = os.path.abspath(path)
    base = os.path.basename(path.rstrip("/")) or "root"
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", base)
    return f"{base}-{hashlib.sha256(path.encode()).hexdigest()[:6]}"


class Store:
    """Notes live in ~/.sticky/<slug>/ by default.

    If the project has a .sticky/ directory (created by `sticky start --local`)
    or STICKY_LOCAL is set, they live in the project instead. Detection is
    automatic so no subcommand has to be told which layout is in use.
    """

    def __init__(self, project: str, local: bool | None = None,
                 directory: str | None = None):
        self.project = os.path.abspath(project)
        if local is None:
            local = (os.path.isdir(os.path.join(self.project, ".sticky"))
                     or bool(os.environ.get("STICKY_LOCAL")))
        self.local = local
        self.dir = (os.path.abspath(directory) if directory
                    else os.path.join(self.project, ".sticky") if local
                    else os.path.join(STATE_HOME, project_slug(self.project)))
        self.path = os.path.join(self.dir, "notes.json")

    def load(self) -> list[dict]:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data.get("notes", []) if isinstance(data, dict) else data

    def save(self, notes: list[dict]):
        private_dir(self.dir)
        payload = {"project": self.project, "notes": notes}
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".notes-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @property
    def hits_path(self) -> str:
        return os.path.join(self.dir, "sidebar-rows.json")

    def save_hits(self, pane: str, hits: list[dict]):
        """Where each note is drawn, so a mouse click can find it."""
        private_dir(self.dir)
        payload = {"pane": pane, "hits": hits}
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".rows-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.hits_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load_hits(self) -> dict:
        try:
            with open(self.hits_path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"pane": "", "hits": []}

    def mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def update(self, note_id: str, **fields) -> bool:
        notes = self.load()
        for note in notes:
            if note["id"] == note_id:
                note.update(fields)
                self.save(notes)
                return True
        return False

    def remove(self, note_id: str) -> bool:
        notes = self.load()
        keep = [n for n in notes if n["id"] != note_id]
        if len(keep) == len(notes):
            return False
        self.save(keep)
        return True


def session_named(command: str, agent: Agent = CLAUDE) -> str:
    """The conversation id a command line asks for, when it says which.

    `--continue` names a conversation without naming it: the id only exists
    once the agent has picked one, so that case comes back empty. So does an
    agent whose profile has no way to name a conversation at all.
    """
    words = command.split()
    for flag in agent.id_flags:
        if flag in words:
            index = words.index(flag)
            if index + 1 < len(words) and not words[index + 1].startswith("-"):
                return words[index + 1]
    return ""


def without_session(command: str, agent: Agent = CLAUDE) -> str:
    """The same command line with any conversation it names taken out.

    A new tab that copies its arguments from a running one must not copy the
    id as well: an agent will not open a session that is already open, so the
    tab dies the moment it starts and leaves its sidebar alone on screen.
    """
    words, out, skip = command.split(), [], False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        if word in agent.id_flags:
            # The id is the word after the flag - unless the next word is
            # another flag, which is what gemini's bare `--resume` looks
            # like: one spelling that both names a conversation and asks for
            # the last one, so the value cannot be assumed to be there.
            nxt = words[index + 1] if index + 1 < len(words) else "-"
            skip = not nxt.startswith("-")
            continue
        if word in agent.bare_session_flags:
            continue
        out.append(word)
    return " ".join(out)


def with_words(command: str, words: list[str], agent: Agent = CLAUDE) -> str:
    """The command line with the session words put where this agent takes them.

    Last for Claude, which spells them as flags and does not mind where they
    sit; first for an agent that spells them as a subcommand, where anything
    in front of them makes them an argument to something else. Every place
    that adds words to a line goes through here, so the difference is settled
    once rather than at each of them.
    """
    if not words:
        return command
    if not agent.words_first:
        return " ".join([command, *words])
    program, _, rest = command.partition(" ")
    return " ".join([program, *words, rest]).rstrip()


def session_dir(project: str, session: str) -> str:
    """Where one conversation's notes live.

    A note quotes a particular conversation, so it belongs to that
    conversation rather than to the directory it was started in. Naming the
    directory after the id is what makes the two ends meet: a second tab on
    the same project opens an empty sidebar, and resuming this one finds its
    notes again without having to be told where they were.
    """
    return os.path.join(Store(project).dir, session)


def resolve_project(tm: Tmux, pane: str | None, explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    if pane:
        recorded = tm.option(pane, "@sticky_project")
        if recorded:
            return recorded
        try:
            where = tm.fmt(pane, "#{pane_current_path}")
        except RuntimeError:
            where = ""
        if where:                       # empty when the pane has gone
            return where
    return os.getcwd()


def sidebars_showing(tm: Tmux, project: str = "",
                     directory: str = "") -> list[tuple[str, str]]:
    """Every sidebar drawing this project or this store: its dir and its pid.

    One scan answers both questions a command without a pane has - where its
    note should go, and who to tell about it - and returns all the answers
    rather than the first, because "either will do" is true of taking a note
    and false of throwing one away.
    """
    try:
        rows = tm.run("list-panes", "-a", "-F",
                      "#{@sticky_role}\t#{@sticky_project}\t"
                      "#{@sticky_store}\t#{pane_pid}").splitlines()
    except RuntimeError:
        return []
    found = []
    for line in rows:
        role, where, store, pid = [*line.split("\t"), "", "", "", ""][:4]
        if role != "sidebar" or not store:
            continue
        if store == directory or (project and not directory
                                  and where == project):
            found.append((store, pid.strip()))
    return found


def sidebar_showing(tm: Tmux, project: str = "",
                    directory: str = "") -> tuple[str, str]:
    """The first sidebar drawing this project or store, or two empties."""
    found = sidebars_showing(tm, project, directory)
    return found[0] if found else ("", "")


def tabs_showing(tm: Tmux) -> set[str]:
    """Which remembered tabs are on screen: their ids, and their note stores.

    What it is for is not offering a tab that is already open: reopening one
    would be a second tab on a conversation that is still running, which the
    agent either refuses or - worse - takes. `last_seen` cannot answer that
    question, since a sidebar keeps its own record warm while it runs, so the
    panes are asked instead. The agent's pane carries `@sticky_session` and
    the sidebar beside it `@sticky_store`, and a record matching either is a
    tab you are already looking at.
    """
    try:
        rows = tm.run("list-panes", "-a", "-F",
                      "#{@sticky_session}\t#{@sticky_store}").splitlines()
    except RuntimeError:
        return set()                      # no server: nothing is open
    return {part for line in rows for part in line.split("\t") if part}


def open_store(tm: Tmux, pane: str | None, project: str | None = None,
               directory: str | None = None) -> Store:
    """The notes this pane is showing.

    Notes belong to a conversation, and the pane is what says which one. With
    no pane - a note piped in from a plain shell - the tab open on that
    project is asked instead, because a note that lands where no sidebar is
    looking is a note you cannot see or send.
    """
    if not directory and pane:
        directory = tm.option(pane, "@sticky_store") or None
    project = resolve_project(tm, pane, project)
    if not directory and not pane:
        open_here = sidebars_showing(tm, project=project)
        if len(open_here) > 1:
            die(f"{len(open_here)} tabs are open on {project}; say which one "
                f"with --pane or --store")
        if open_here:
            directory = open_here[0][0]
    return Store(project, directory=directory)


def agent_of(tm: Tmux, pane: str | None) -> Agent:
    """Which agent profile this tab was started on.

    Recorded on the agent's own pane by `start`, so every later command -
    fork, resume, the sidebar, the placer - reads back the same profile
    rather than assuming the default. Any pane of the tab can be asked: the
    sidebar and a note prompt both carry `@sticky_partner`, which is where
    the option is. A tab opened by an older sticky-chat has neither, and
    comes back as the default, which is what it was.
    """
    if not pane:
        return agent_named("")
    name = tm.option(pane, "@sticky_agent")
    if not name:
        partner = tm.option(pane, "@sticky_partner")
        name = tm.option(partner, "@sticky_agent") if partner else ""
    return agent_named(name)


def launched_as(tm: Tmux, pane: str, agent: Agent = CLAUDE) -> str:
    """The command line a tab is running, for a new tab to repeat.

    A tab opened before sticky started recording the command line still knows
    it: tmux kept what the pane was launched with. Falling back to a bare
    command name would quietly drop flags the original was given, and a tab
    that silently lost --dangerously-skip-permissions is a tab that starts
    asking again halfway through a task.
    """
    recorded = tm.option(pane, "@sticky_agent_cmd")
    if recorded:
        return recorded
    started = tm.fmt(pane, "#{pane_start_command}").strip().strip('"')
    if "; exec " in started:                  # the virtual-rows prologue
        started = started.split("; exec ", 1)[1]
    return started or agent.command


def claude_pane(tm: Tmux, pane: str | None) -> str | None:
    """Given any pane in a sticky window, return the Claude pane's id."""
    if not pane:
        return None
    role = tm.option(pane, "@sticky_role")
    if role == "claude":
        return pane
    partner = tm.option(pane, "@sticky_partner")
    return partner or pane
