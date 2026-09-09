"""One function per subcommand."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .agents import CLAUDE, Agent, agent_named, require_agent
from .clipboard import to_clipboard
from .config import (
    DEFAULT_SIDEBAR_WIDTH,
    FOCUS_STYLE,
    client_area,
    fit_windows,
    install_hooks,
    pin_clients,
    prime_rows,
    virtual_window,
    write_config,
)
from .placement import (
    CONTEXT_ROWS,
    LANDMARK_WINDOW,
    find_landmark,
    in_prompt_box,
    parse_view,
    placements,
)
from .store import (
    Store,
    agent_of,
    claude_pane,
    known_windows,
    launched_as,
    names_a_session,
    open_store,
    record_window,
    resolve_project,
    session_dir,
    session_named,
    sidebar_showing,
    tabs_showing,
    windows_dir,
    with_words,
    without_session,
)
from .tmux import SESSION_NAME, Tmux
from .util import (
    BOLD,
    COL_COMMITTED,
    COL_PENDING,
    DIM,
    RESET,
    complete_path,
    die,
    prompt_line,
    self_path,
    shell_quote,
    terminal_size,
    terminal_width,
    truncate,
    visible,
)


def leave_copy_mode(tm: Tmux, pane: str, args) -> None:
    """Drop out of copy mode, but only if you were at the live prompt.

    Taking a note while scrolled back means you are reading; jumping to the
    bottom would lose your place, and the highlight is the marker for where
    you were. At the bottom there is nothing to lose, so get out of the way
    and let the next keystroke reach Claude.
    """
    if getattr(args, "scroll", None):
        return
    tm.ok("send-keys", "-X", "-t", pane, "cancel")


def nudge(tm: Tmux, pane: str | None, store: Store | None = None) -> None:
    """Tell the sidebar its notes changed instead of leaving it to notice.

    The notes file is the sidebar's only source of truth and it finds a
    change by looking at the timestamp, once a tick. Saying so here means
    the note appears at once. Nothing depends on the signal arriving: the
    file is right either way, and the sidebar reads it eventually.

    A note taken from a plain shell has no pane to ask, so the sidebars are
    asked instead which of them is showing this store.
    """
    pid = sidebar_pid(tm, pane) if pane else ""
    if not pid.isdigit() and store is not None:
        pid = sidebar_showing(tm, directory=store.dir)[1]
    if not pid.isdigit():
        return
    try:
        os.kill(int(pid), signal.SIGUSR1)
    except OSError:
        pass                              # gone, or not ours to signal


def sidebar_pid(tm: Tmux, pane: str) -> str:
    """The process drawing the sidebar beside this pane, asked of tmux.

    Not the `@sticky_sidebar_pid` option: that is a number remembered from
    when a sidebar started, and nothing clears it when one goes. The system
    reuses pids, and SIGUSR1 to a process that never asked for it is a
    process killed - so the pid comes from tmux, which only reports panes
    that exist.
    """
    role, partner = tm.formats((pane, "#{@sticky_role}"),
                               (pane, "#{@sticky_partner}"))
    side = pane if role.strip() == "sidebar" else partner.strip()
    if not side:
        return ""
    return tm.fmt(side, "#{pane_pid}").strip()


def open_prompt(tm: Tmux, command: list[str], pane: str | None,
                height: str = "30%") -> str:
    """Put a prompt on screen as a floating pane. Returns its pane id.

    A floating pane rather than a popup, because a popup is not a pane. tmux
    hands every mouse event to the popup and drops the ones that land outside
    it - `popup_key_cb` returns 0 for those - so nothing can tell that you
    clicked away: not a binding, not a hook, not the popup itself. A floating
    pane sits over the layout in the same way and is still a pane, so
    clicking off it is an ordinary change of active pane, which
    `window-pane-changed` reports and `cmd_sweep` acts on.

    It does not block either. The pane runs on its own, and whatever the
    prompt has to do when it is finished, it does itself.
    """
    made = ["new-pane", "-x", "70%", "-y", height, "-X", "15%", "-Y", "30%",
            "-P", "-F", "#{pane_id}"]
    if pane:
        made += ["-t", pane]
    made.append(" ".join(shell_quote(c) for c in command))
    where = tm.run(*made).strip()
    if where:
        # What marks it for the sweep that closes it when you click away.
        tm.ok("set-option", "-p", "-t", where, "@sticky_role", "note")
        if pane:
            # And what lets a command run from in here find its way home.
            tm.ok("set-option", "-p", "-t", where, "@sticky_partner", pane)
            store = tm.option(pane, "@sticky_store")
            if store:
                tm.ok("set-option", "-p", "-t", where, "@sticky_store", store)
    return where


def cmd_add(args) -> int:
    tm = Tmux(args.socket)
    quote = args.text if args.text is not None else sys.stdin.read()
    quote = quote.rstrip("\n")
    if not quote.strip():
        die("empty selection, nothing to annotate")
    # Selecting is copying, the way it is everywhere else: a drag over Claude
    # reaches the clipboard whether or not it turns into a note.
    to_clipboard(quote)

    pane = args.pane
    agent = agent_of(tm, pane)
    project = resolve_project(tm, pane, args.project)
    store = open_store(tm, pane, project, args.store)

    rows: list[str] = []
    before: list[str] = []
    after: list[str] = []
    landmark = None
    abs_line = args.sy
    span = 1

    if pane and tm.pane_exists(pane):
        # The binding passes the history size as it was when the selection was
        # made; querying it now would race with output arriving in between.
        history = (args.hsize if getattr(args, "hsize", None) is not None
                   else int(tm.fmt(pane, "#{history_size}") or 0))
        cursor_abs = None
        try:
            cursor_abs = history + int(tm.fmt(pane, "#{cursor_y}") or 0)
        except (RuntimeError, ValueError):
            pass
        sy, ey = args.sy, args.ey
        sx, ex = args.sx, args.ex
        if (sy, sx) > (ey, ex):
            sy, ey, sx, ex = ey, sy, ex, sx
        abs_line, span = sy, max(1, ey - sy + 1)
        rel_start, rel_end = sy - history, ey - history
        rows = tm.capture(pane, rel_start, rel_end)
        before = tm.capture(pane, max(-history, rel_start - CONTEXT_ROWS),
                            rel_start - 1)
        after = tm.capture(pane, rel_end + 1, rel_end + CONTEXT_ROWS)
        lookback = tm.capture(pane, max(-history, rel_start - LANDMARK_WINDOW),
                              rel_start - 1)
        landmark = find_landmark(lookback, agent)

        # A mouse drag over your own prompt is a plain copy: the text is in
        # the paste buffer already, so there is nothing left to do.
        if args.auto and in_prompt_box(ey, cursor_abs, agent):
            leave_copy_mode(tm, pane, args)
            return 0
    else:
        rows = quote.split("\n")
        span = len(rows)

    if not rows:
        rows = quote.split("\n")

    joined = "\n".join(rows)
    note = {
        "id": hashlib.sha256(
            f"{time.time()}{quote}{abs_line}".encode()).hexdigest()[:8],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "pending",
        "note": args.note or "",
        "quote": quote,
        "rows": rows,
        "sx": args.sx,
        "ex": args.ex,
        "partial": quote.strip() != joined.strip(),
        "before": before,
        "after": after,
        "landmark": landmark,
        "abs_line": abs_line,
        "pane": pane or "",             # a row means nothing in another pane
        "anchor_abs": abs_line,
        "span": span,
        "committed_at": None,
    }

    notes = store.load()
    notes.append(note)
    store.save(notes)

    if args.note:
        print(note["id"])
        nudge(tm, pane, store)
        if pane:
            leave_copy_mode(tm, pane, args)
        return 0

    # Ask for the note text beside the selection that made it.
    self_cmd = [self_path(), "note", note["id"],
                "--project", project, "--store", store.dir]
    if args.socket:
        self_cmd += ["--socket", args.socket]
    if pane:
        self_cmd += ["--pane", pane]
    try:
        open_prompt(tm, self_cmd, pane)
    except RuntimeError as exc:
        print(f"sticky: could not open the prompt ({exc}); note saved as "
              f"{note['id']}, set text with: "
              f"sticky-chat note {note['id']}",
              file=sys.stderr)
        return 1
    if pane:
        leave_copy_mode(tm, pane, args)
    return 0


def cmd_note(args) -> int:
    tm = Tmux(args.socket)
    store = Store(args.project or os.getcwd(), directory=args.store)
    notes = store.load()
    note = next((n for n in notes if n["id"] == args.id), None)
    if note is None:
        die(f"no note {args.id}")

    width = terminal_width()
    title = "Edit note" if args.edit else "Sticky note"
    hint = "Enter saves \u00b7 C-s sends \u00b7 alt-enter sends and enters"
    print(f"{BOLD}{title}{RESET}  {DIM}{hint}{RESET}\n")
    for row in note["quote"].split("\n")[:8]:
        print(f"  {DIM}{truncate(row, width - 4)}{RESET}")
    print()
    before = note["note"]

    def keep(typed: str) -> None:
        """Save what was typed when the prompt is closed from outside.

        Clicking away closes the pane, which arrives here as SIGHUP - so
        without this the note would go with it. Empty means nothing was
        meant: an untouched note is taken back rather than left as a blank
        row.
        """
        text = typed.strip()
        if text:
            store.update(args.id, note=text)
        elif not args.edit:
            store.remove(args.id)
        nudge(tm, getattr(args, "pane", None), store)

    def done(handover: bool) -> int:
        """Say the notes changed, and hand over when there is one to send.

        The prompt runs in a pane of its own now, so whatever used to happen
        after the prompt returned has to happen here instead - there is no
        longer anything waiting on it.
        """
        pane = getattr(args, "pane", None)
        nudge(tm, pane, store)
        if handover and pane:
            partner = tm.option(pane, "@sticky_partner")
            if partner:
                tm.ok("select-pane", "-t", partner)
        return 0

    outcome, typed = prompt_line("note> ", before if args.edit else "",
                                 on_hangup=keep)
    text = typed.strip()

    if outcome in ("send", "send_now"):
        # Save it and hand the batch over without the trip through the
        # sidebar: C-s here is `s` there and Alt-Enter is `S`, from where you
        # are already typing. C-S would have been the obvious second key, but
        # a terminal sends the same byte for it as for C-s.
        if text:
            store.update(args.id, note=text, status="pending",
                         committed_at=None)
        elif not args.edit:
            store.remove(args.id)
        send = [self_path(), "commit", "--quiet"]
        if args.socket:
            send += ["--socket", args.socket]
        if getattr(args, "pane", None):
            send += ["--pane", args.pane]
        if outcome == "send_now":
            send.append("--send")
        subprocess.run(send, capture_output=True, text=True)
        return done(False)
    if outcome == "copy":                  # the selection was the point
        to_clipboard(note["quote"])
        if not args.edit:
            store.remove(args.id)
        return done(False)
    if outcome == "cancel":
        if args.edit:
            return done(False)             # leave the note exactly as it was
        if not text:
            store.remove(args.id)          # nothing typed: nothing meant
            return done(False)
        # Something was typed, so keep it struck out rather than throw it
        # away; clicking its [x] in the sidebar brings it back.
        store.update(args.id, note=text, deleted=True)
        return done(False)
    if not text:
        store.remove(args.id)
        return done(False)
    fields = {"note": text}
    if args.edit and text != before.strip() and note["status"] == "committed":
        # Changed after being sent: it has to go out again.
        fields.update(status="pending", committed_at=None)
    store.update(args.id, **fields)
    # Saved: the next thing you are likely to want is to send it, so hand
    # over to the sidebar where s and S do that.
    return done(True)


# ------------------------------------------------------------------- commit


def render_commit(note: dict) -> str:
    lines = note["quote"].split("\n")
    text = note["note"].strip()
    if len(lines) == 1:
        return f"{lines[0].strip()} - {text}"
    body = "\n".join("> " + line.rstrip() for line in lines)
    return f"{body}\n- {text}"


def expand_paste(tm: Tmux, pane: str, agent: Agent = CLAUDE) -> None:
    """Undo Claude's folding of a long paste into "[Pasted text #1 +N lines]".

    Anything over three lines is shown as a placeholder, which hides the very
    thing you are about to send. Claude's own hint under it says to paste
    again to expand, and a second paste replaces the placeholder with the
    text. Detect rather than guess the threshold, so this keeps working if it
    ever moves.

    An agent whose profile describes no such placeholder is left alone: a
    second paste into one that never folded would send the block twice.
    """
    if agent.paste_placeholder is None:
        return
    for _ in range(3):
        time.sleep(0.2)
        try:
            height = int(tm.fmt(pane, "#{pane_height}") or 0)
            # Only the prompt box, never the transcript above it: a
            # conversation that discusses the placeholder would otherwise look
            # like a placeholder, and the block would be pasted again and
            # again.
            shown = "\n".join(tm.capture(
                pane, max(0, height - agent.prompt_box_rows), height - 1))
        except (RuntimeError, ValueError):
            return
        if not agent.paste_placeholder.search(shown):
            return
        tm.ok("paste-buffer", "-p", "-b", "sticky", "-t", pane)


def cmd_commit(args) -> int:
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    notes = store.load()

    if pane and tm.pane_exists(pane):
        try:
            placements(tm, pane, notes)
        except RuntimeError:
            pass

    pending = [n for n in notes if n["status"] == "pending"
               and n["note"].strip() and not n.get("deleted")]
    if not pending:
        print("sticky: nothing pending", file=sys.stderr)
        return 0
    pending.sort(key=lambda n: n.get("abs_line", 0))

    text = "\n\n".join(render_commit(n) for n in pending)
    if not args.quiet:          # a key binding's stdout lands in the pane
        print(text)

    if not args.no_paste and pane and tm.pane_exists(pane):
        fd, tmp = tempfile.mkstemp(prefix="sticky-commit-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            # Reading back is over the moment you send: leave copy mode, or
            # the block would land in a prompt you cannot see.
            tm.ok("send-keys", "-X", "-t", pane, "cancel")
            tm.run("load-buffer", "-b", "sticky", tmp)
            tm.run("paste-buffer", "-p", "-b", "sticky", "-t", pane)
            expand_paste(tm, pane, agent_of(tm, pane))
            tm.ok("delete-buffer", "-b", "sticky")
            if args.send:
                time.sleep(0.15)      # let Claude take the bracketed paste in
                tm.run("send-keys", "-t", pane, "Enter")
            # Either way you end up at Claude's prompt, which is where the
            # answer is going to appear.
            tm.ok("select-pane", "-t", pane)
        finally:
            os.unlink(tmp)

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    ids = {n["id"] for n in pending}
    for note in notes:
        if note["id"] in ids:
            note["status"] = "committed"
            note["committed_at"] = stamp
    store.save(notes)
    nudge(tm, pane, store)
    return 0


def cmd_uncommit(args) -> int:
    """Unmark a batch: the notes go back to pending, ready to send again.

    Nothing is taken back from Claude - the block is already in its prompt,
    and if you pressed Enter it has already been read. This is for when the
    send did not land: you cleared the prompt, or the paste went astray, and
    the notes are marked as gone when they never went. It fixes the marks.
    """
    tm = Tmux(args.socket)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    notes = store.load()
    committed = [n for n in notes if n["status"] == "committed"]
    if not committed:
        print("sticky: nothing is marked as sent", file=sys.stderr)
        return 0

    if args.id:
        wanted = {n["id"] for n in committed if n["id"] in set(args.id)}
    elif args.all:
        wanted = {n["id"] for n in committed}
    else:
        # the last send: every note that carries the newest timestamp
        newest = max(n.get("committed_at") or "" for n in committed)
        wanted = {n["id"] for n in committed
                  if (n.get("committed_at") or "") == newest}
    if not wanted:
        die("no committed note with that id")

    for note in notes:
        if note["id"] in wanted:
            note["status"] = "pending"
            note["committed_at"] = None
    store.save(notes)
    nudge(tm, args.pane, store)
    if not args.quiet:
        print(f"sticky: {len(wanted)} note(s) pending again")
    return 0


# --------------------------------------------------------------------- list


def cmd_list(args) -> int:
    tm = Tmux(args.socket)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    notes = store.load()
    if not args.all:
        notes = [n for n in notes if n["status"] == "pending"]
    if not notes:
        print("(no notes)")
        return 0
    width = terminal_width()
    for note in sorted(notes, key=lambda n: n.get("abs_line", 0)):
        colour = COL_COMMITTED if note["status"] == "committed" else COL_PENDING
        head = note["quote"].split("\n")[0].strip()
        extra = f" (+{note['span'] - 1} rows)" if note["span"] > 1 else ""
        print(f"{colour}{note['id']}{RESET} {DIM}{truncate(head, width - 30)}"
              f"{extra}{RESET}")
        print(f"         {note['note']}")
    return 0


def cmd_rm(args) -> int:
    tm = Tmux(args.socket)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    if not store.remove(args.id):
        die(f"no note {args.id}")
    nudge(tm, args.pane, store)
    return 0


def cmd_clear(args) -> int:
    tm = Tmux(args.socket)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    notes = store.load()
    keep = [n for n in notes if n["status"] != "committed"] if args.committed else []
    store.save(keep)
    nudge(tm, args.pane, store)
    print(f"removed {len(notes) - len(keep)} note(s)")
    return 0


def cmd_place(args) -> int:
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane)
    if not pane:
        die("need --pane")
    store = open_store(tm, pane, args.project,
                       getattr(args, 'store', None))
    result = placements(tm, pane, store.load())
    print(json.dumps([
        {"id": p["id"], "row": p["row"], "match": p["match"],
         "status": p["note"]["status"], "note": p["note"]["note"],
         "quote": p["note"]["quote"]}
        for p in result], indent=1, ensure_ascii=False))
    return 0


def pane_env(agent: Agent) -> list[str]:
    """The `-e NAME=VALUE` arguments a new agent pane is opened with.

    Two kinds. What the agent is told about the tmux around it comes from
    its profile - Claude reads the session and the prefix, so that it leaves
    our key alone - and an agent that asks for nothing gets nothing. The
    STICKY_ variables are ours: a test run or a --local checkout sets them,
    and a pane started without them would write its notes somewhere else.
    """
    env: list[str] = []
    for name, value in agent.env:
        env += ["-e", f"{name}={value}"]
    for var in ("STICKY_HOME", "STICKY_LOCAL"):
        if os.environ.get(var):
            env += ["-e", f"{var}={os.environ[var]}"]
    return env


def open_sidebar(tm: Tmux, binary: str, left: str, project: str,
                 width: int, store_dir: str | None = None) -> str:
    """Split a sidebar beside `left` and wire the two panes together."""
    command = [binary, "sidebar", "--pane", left, "--project", project,
               "--socket", tm.socket]
    if store_dir:
        command += ["--store", store_dir]
    sidebar_cmd = " ".join(shell_quote(c) for c in command)
    right = tm.run("split-window", "-h", "-d", "-l", str(width),
                   "-t", left, "-c", project, "-P", "-F", "#{pane_id}",
                   sidebar_cmd).strip()
    tm.set_option(left, "@sticky_role", "claude")
    tm.set_option(left, "@sticky_partner", right)
    tm.set_option(left, "@sticky_project", project)
    tm.set_option(right, "@sticky_role", "sidebar")
    # Which pane the keyboard is in is otherwise a single column of border,
    # easy to lose. tmux paints a pane's own ground for us - empty rows and
    # all - and `window-active-style` on this one pane means it does it
    # exactly while the sidebar is the pane you are typing in. A grey rather
    # than something brighter or darker, so that it reads as a difference on
    # a dark ground and on a light one; `@sticky_focus_style` in user.conf
    # overrides it, and an empty value turns it off.
    style = tm.run("show-options", "-gqv", "@sticky_focus_style").strip()
    if style != "off":
        tm.ok("set-option", "-p", "-t", right, "window-active-style",
              style or FOCUS_STYLE)
    tm.set_option(right, "@sticky_width", str(width))
    tm.set_option(right, "@sticky_partner", left)
    tm.set_option(right, "@sticky_project", project)
    if store_dir:
        tm.set_option(left, "@sticky_store", store_dir)
        tm.set_option(right, "@sticky_store", store_dir)
    return right


def mark_launch(tm: Tmux, pane: str, agent: Agent, launched: float,
                known: str = "") -> None:
    """Tell the pane when it started, so its id can be found later.

    Only for a profile whose id is its own to choose: an agent we hand an id
    to has nothing to discover, and this is the difference between one tmux
    call and none on the path every Claude tab takes. The launch time is
    recorded rather than inferred because it is what keeps yesterday's
    transcript out - a file older than the tab is somebody else's
    conversation - and `#{pane_start_time}` is not a format tmux has.

    `known` is an id already learned, carried over when a tab is reopened, so
    that the sidebar goes looking only where nothing has been found yet.
    """
    if not agent.can_discover:
        return
    tm.set_option(pane, "@sticky_launched", repr(launched))
    if known:
        tm.set_option(pane, "@sticky_agent_session", known)


def cmd_fork(args) -> int:
    """Branch the conversation into a new tab, carrying the open notes over.

    The agent replays the transcript into a fresh conversation - Claude with
    `--continue --fork-session`, codex with `codex fork --last` - and the
    fork gets its own copy of whatever was still pending, so asking there
    does not mark anything answered back here.
    """
    tm = Tmux(args.socket)
    source = claude_pane(tm, args.pane)
    if not source:
        die("run this from a sticky window, or pass --pane")
    agent = agent_of(tm, source)
    if not agent.can_fork:
        # Without a way to branch the conversation, a "fork" would be an
        # empty tab wearing the notes of one that is still talking. Say so
        # rather than open that.
        message = (f"{agent.label} cannot branch a conversation, so there "
                   f"is no fork to make; C-g c opens a new tab")
        if args.client:
            tm.ok("display-message", "-c", args.client, f"sticky: {message}")
        die(message)
    project = resolve_project(tm, source, args.project)
    origin = open_store(tm, source, project)

    carried = [dict(note, status="pending", committed_at=None)
               for note in origin.load()
               if note["status"] == "pending" and not note.get("deleted")
               and note["note"].strip()]

    inherited = launched_as(tm, source, agent)
    # The fork is a new conversation, so it needs an id of its own rather
    # than the one the original was started with - when the agent takes one.
    # An agent that names its own conversations is handed nothing, and the
    # branch it makes is remembered under a key of ours, exactly as a tab
    # told to continue without being told which conversation.
    base = without_session(inherited, agent)
    session = str(uuid.uuid4()) if agent.names_sessions else ""
    words = list(agent.fork_flags)
    if session:
        words += [agent.session_flag, session]
    command = with_words(base, words, agent)
    key = session or f"tab-{uuid.uuid4().hex[:12]}"

    # Its notes hang off its own id, like any other conversation's, so
    # resuming the fork later comes back to the copy rather than to the
    # notes it was cut from.
    fork_dir = session_dir(project, key)
    fork = Store(project, directory=fork_dir)
    fork.save(carried)

    # A fork of a tall window should be tall too, or its notes would sit at a
    # different offset from the ones they were copied from.
    rows = tm.fmt(source, "#{@sticky_virtual_rows}")
    virtual = int(rows) if rows.strip().isdigit() else 0
    launch = prime_rows(command, virtual) if virtual else command

    name = os.path.basename(project.rstrip("/")) or "root"
    env = pane_env(agent)
    launched = time.time()              # before the pane, as in `open_tab`
    left = tm.run("new-window", "-d", "-t", f"{SESSION_NAME}:",
                  "-n", f"{name}-fork",
                  "-c", project, *env, "-P", "-F", "#{pane_id}",
                  launch).strip()
    if not tm.pane_exists(left):
        if args.client:
            tm.ok("display-message", "-c", args.client,
                  f"sticky: {command} exited immediately")
        die(f"the command exited immediately: {command}")

    width = tm.option(source, "@sticky_width") or str(DEFAULT_SIDEBAR_WIDTH)
    # Set before the sidebar starts, not after: the first thing it does is
    # ask the pane which agent this is, and a sidebar that asked too early
    # draws the whole tab as the default one.
    # What a tab opened from this one should repeat: the branch has already
    # happened, and cutting a second copy of the transcript is nobody's idea
    # of "another tab like this one".
    tm.set_option(left, "@sticky_agent_cmd",
                  with_words(base, [agent.session_flag, session], agent)
                  if session else base)
    tm.set_option(left, "@sticky_session", key)
    tm.set_option(left, "@sticky_agent", agent.name)
    # A branch is a new conversation with a new id, and on an agent that
    # names its own it is exactly the id we do not have - so a fork looks for
    # one the same way a fresh tab does.
    mark_launch(tm, left, agent, launched)
    open_sidebar(tm, self_path(), left, project, int(width), fork_dir)
    # And what `resume` re-runs. Claude's whole line is kept because
    # `resume_command` drops the branch words from it and asks for the id
    # instead; with no id to ask for there is nothing to drop them by, so
    # they are dropped here rather than at every restart from now on.
    record_window(key, project=project,
                  command=command if session else base, store=fork_dir,
                  continued=not session,
                  width=int(width), virtual_rows=virtual, agent=agent.name,
                  name=f"{name}-fork")

    window = tm.fmt(left, "#{session_name}:#{window_index}")
    if virtual:
        virtual_window(tm, window, virtual)
    if args.client:
        tm.ok("switch-client", "-c", args.client, "-t", window)
    else:
        tm.ok("switch-client", "-t", window)
    if not args.quiet:
        print(f"{left} {fork_dir}")
    return 0


def jump_to_note(tm: Tmux, claude: str, note: dict, client: str | None) -> int:
    """Scroll the Claude pane to the text a note was taken from.

    Absolute line numbers do not move as output is appended, so the position
    a note last matched at is still the right place to look. Enter copy mode,
    zero the offset, then scroll back far enough to put the line in the
    middle of the rows on screen - `goto-line` is avoided because what it
    means depends on an option.
    """
    raw = tm.fmt(claude, "#{history_size}\t#{pane_height}")
    history, height, _ = parse_view(raw)
    target = note.get("anchor_abs", note.get("abs_line"))
    if target is None:
        return 0
    # Mid-pane is not mid-screen once the window is taller than the
    # terminal: the viewport is pinned to the bottom, so centring in the
    # pane puts the text above everything you can see and the note leaves
    # the map it was clicked in. Centre in the rows on screen instead;
    # without a tall window the two are the same row.
    shown = min(height, client_area(tm, claude) or height)
    middle = height - shown // 2
    back = history - int(target) + middle
    back = max(0, min(back, history))
    if not back:
        return 0                      # already at the bottom: nothing hidden

    tm.ok("copy-mode", "-t", claude)
    tm.ok("send-keys", "-X", "-t", claude, "history-bottom")
    tm.ok("send-keys", "-X", "-N", str(back), "-t", claude, "scroll-up")
    rows = note.get("rows") or []
    if rows and rows[0] not in tm.capture(claude, 0, height - 1) and client:
        tm.ok("display-message", "-c", client,
              "sticky: that text is not in the pane's history any more")
    return 0


DISMISS_GRACE = 0.5         # how long a click stays "the one that closed it"


def dismissing(tm: Tmux, pane: str) -> bool:
    """True when this click is the one getting rid of a note prompt.

    Clicking away from a prompt finishes the note. It should not also press
    whatever was under the pointer - reaching for the nearest place to click
    is not the same as asking to strike a note out. So the first click is
    spent on the prompt and the second does what it says.

    Asked two ways because the answer arrives from two directions: the
    prompt may still be there, or `cmd_sweep` may have got to it first and
    left the time it did behind.
    """
    try:
        roles = tm.run("list-panes", "-t", pane, "-F",
                       "#{@sticky_role}").split()
    except RuntimeError:
        roles = []
    if "note" in roles:
        return True
    when = tm.run("show-options", "-gqv", "@sticky_dismissed").strip()
    try:
        return time.time() - float(when) < DISMISS_GRACE
    except ValueError:
        return False


def cmd_click(args) -> int:
    """A click in the sidebar.

    Single click shows you where the note came from, double click edits it,
    and the [x] button at the right edge strikes it out. tmux sends a MouseUp
    for the second press of a double click as well, so a double click arrives
    as jump, edit, jump; the guard file drops that trailing jump.
    """
    tm = Tmux(args.socket)
    pane = args.pane
    if not pane:
        return 0
    if tm.fmt(pane, "#{pane_in_mode}") == "1":
        return 0                        # that was a selection, not a click
    if dismissing(tm, pane):
        return 0                        # that click was closing the prompt
    store = open_store(tm, pane, args.project,
                       getattr(args, 'store', None))
    data = store.load_hits()
    if data.get("pane") and data["pane"] != pane:
        return 0
    if args.id:
        hit = next((h for h in data.get("hits", []) if h["id"] == args.id),
                   {"id": args.id, "x": 10 ** 6, "onscreen": False})
    else:
        hit = next((h for h in data.get("hits", []) if h["row"] == args.y),
                   None)
    if hit is None:
        return 0
    notes = store.load()
    note = next((n for n in notes if n["id"] == hit["id"]), None)
    if note is None:
        return 0

    guard = os.path.join(store.dir, ".last-edit")
    if args.edit:
        try:
            with open(guard, "w") as fh:
                fh.write(str(time.time()))
        except OSError:
            pass
    elif args.x >= hit.get("x", 10 ** 6):
        note["deleted"] = not note.get("deleted")
        store.save(notes)
        nudge(tm, pane)
        return 0
    else:
        try:
            with open(guard) as fh:
                recent = time.time() - float(fh.read()) < 1.0
        except (OSError, ValueError):
            recent = False
        if recent:
            return 0                    # the tail of a double click
        if hit.get("onscreen") and not args.id:
            return 0                    # a click on something you can see
        return jump_to_note(tm, claude_pane(tm, pane) or pane, note,
                            args.client)

    command = [self_path(), "note", hit["id"], "--edit",
               "--project", store.project, "--store", store.dir]
    if args.socket:
        command += ["--socket", args.socket]
    claude = claude_pane(tm, pane) or pane
    if claude:
        command += ["--pane", claude]
    open_prompt(tm, command, claude)
    return 0


YEAR = 365 * 24 * 3600


def resume_command(record: dict, agent: Agent | None = None) -> str:
    """The command that reopens a remembered tab on its own conversation."""
    agent = agent or agent_named(record.get("agent"))
    command = record.get("command", agent.command)
    # Two ids, and only one of them is a conversation. `session` is the key
    # the notes are filed under - the id we chose, or a `tab-` name of ours
    # when the agent chose its own - and `agent_session` is what the agent
    # called the conversation, read back off the transcript it opened. They
    # are separate fields because the notes must keep the key they were
    # written under while only the agent's own id may go on a command line:
    # a tab started with a bare --continue has a key that no agent has ever
    # heard of.
    named = record.get("agent_session") or (
        "" if record.get("continued") else record.get("session", ""))
    if agent.can_resume and named:
        # The id is about to be appended, so any flag already naming one has
        # to go with its value - otherwise the command line says --resume
        # twice.
        naming = {agent.session_flag} if agent.session_flag else set()
        naming |= set(agent.resume_flags)
        words, out = command.split(), []
        skip = False
        for word in words:
            if skip:
                skip = False
                continue
            if word in naming:
                skip = True
                continue
            if word in agent.bare_session_flags:
                continue                # the fork already happened
            out.append(word)
        return with_words(" ".join(out), [agent.resume_flags[0], named], agent)
    if agent.continue_flags and not names_a_session(command, agent):
        # No id to ask for: the agent named its own conversation and nothing
        # ever learned what it called it. Asking for the last conversation in
        # this directory is the closest there is, and it is what
        # `continue_flags` spells - without this a codex tab came back as a
        # bare `codex`, which is a new conversation wearing the old notes.
        return with_words(command, list(agent.continue_words), agent)
    # Either the line already says which conversation to open - the bare
    # --continue the tab was started with, kept exactly as it is - or this
    # agent has no way of being asked for one at all, and a flag it does not
    # have is never put on the line.
    return command


def open_tab(tm: Tmux, record: dict, command: str, client: str | None) -> str:
    """Put a remembered tab back on screen. Empty string if it would not start."""
    project = record.get("project") or os.getcwd()
    if not os.path.isdir(project):
        return ""
    virtual = int(record.get("virtual_rows") or 0)
    agent = agent_named(record.get("agent"))
    env = pane_env(agent)
    launch = prime_rows(command, virtual) if virtual else command
    # Before the pane, so that a transcript the agent opens a moment later is
    # newer than this and one written yesterday is not.
    launched = time.time()
    config = write_config(self_path())
    name = record.get("name") or os.path.basename(project.rstrip("/"))
    if not tm.server_running():
        left = tm.run("-f", config, "new-session", "-d", "-s", SESSION_NAME,
                      "-n", name, "-c", project, *env,
                      "-P", "-F", "#{pane_id}", launch).strip()
    else:
        left = tm.run("new-window", "-d", "-t", f"{SESSION_NAME}:", "-n", name,
                      "-c", project, *env, "-P", "-F", "#{pane_id}",
                      launch).strip()
    time.sleep(0.4)                     # a refused session dies about now
    if not tm.pane_exists(left):
        return ""
    install_hooks(tm)                   # this may be a server we just started
    width = int(record.get("width") or DEFAULT_SIDEBAR_WIDTH)
    tm.set_option(left, "@sticky_agent_cmd", command)
    tm.set_option(left, "@sticky_session", record.get("session", ""))
    tm.set_option(left, "@sticky_agent", agent.name)   # before the sidebar
    mark_launch(tm, left, agent, launched, record.get("agent_session", ""))
    open_sidebar(tm, self_path(), left, project, width,
                 record.get("store") or None)
    if virtual:
        virtual_window(tm, tm.fmt(left, "#{session_name}:#{window_index}"),
                       virtual)
    record_window(record.get("session", ""), project=project)
    return left


def reopen_tab(tm: Tmux, record: dict, client: str | None) -> tuple[str, str]:
    """Put one remembered tab back on its own conversation, and say how it went.

    The new pane, and what is worth telling whoever asked - empty when there
    is nothing to say. The conversation may be gone by now: deleted, expired,
    or an id the agent will not take, all of which look the same from here,
    which is a tab that dies the moment it starts. What comes back then is
    the same project and the same notes around a new conversation, and that
    is a difference the person who asked should hear about.

    Both `resume` and the `C-g o` picker come through here, so the two cannot
    drift apart on which line a record is reopened with.
    """
    agent = agent_named(record.get("agent"))
    command = resume_command(record, agent)
    left = open_tab(tm, record, command, client)
    if left or not agent.can_resume:
        return left, ""
    # What comes off the line is the id as it appears *there*, which for a
    # discovered one is not the key the notes are filed under.
    asked = session_named(command, agent)
    fresh = " ".join(w for w in command.split()
                     if w not in agent.resume_flags
                     and (not asked or w != asked))
    left = open_tab(tm, record, fresh, client)
    return left, ("that conversation is gone, started a new one" if left
                  else "")


def ask(question: str, choices: str) -> str:
    """One keypress from the terminal, lower-cased. Empty if there is none."""
    if not sys.stdin.isatty():
        return ""
    sys.stdout.write(f"{question} [{choices}] ")
    sys.stdout.flush()
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"
    return answer[:1] if answer else choices[0]


def cmd_quit(args) -> int:
    """Close every window and stop the agents in them.

    Nothing is saved here that was not already saved: the records are written
    as tabs open and kept warm while they run. What quitting adds is which
    tabs were open at the end, so `resume --last` can offer that set rather
    than everything ever opened.
    """
    tm = Tmux(args.socket)
    if not tm.server_running():
        if not args.quiet:
            print("sticky: nothing running")
        return 0

    listing = tm.run("list-panes", "-a", "-F",
                     "#{@sticky_role}\t#{@sticky_session}\t"
                     "#{@sticky_project}\t#{@sticky_agent}")
    open_now = set()
    projects = []
    stopping = []
    for line in listing.splitlines():
        parts = [*line.split("\t"), "", "", "", ""][:4]
        if parts[0] == "claude" and parts[1]:
            open_now.add(parts[1])
            projects.append(os.path.basename(parts[2].rstrip("/")) or parts[2])
            # Named because you are about to end them: "stop Claude?" beside
            # a codex tab is a question about something that is not there.
            short = agent_named(parts[3]).short
            if short not in stopping:
                stopping.append(short)

    if not args.yes:
        listed = ", ".join(projects) if projects else "the session"
        ending = " and ".join(stopping) if stopping else "the agents"
        answer = ask(f"sticky: close {listed} and stop {ending}?", "n/y")
        if answer != "y":
            print("sticky: left alone")
            return 0

    # Remember the shape of this moment, so resume can offer it back whole.
    for record in known_windows():
        session = record.get("session", "")
        if not session:
            continue
        record_window(session, open_at_quit=session in open_now)

    tm.ok("kill-server")
    if not args.quiet:
        print(f"sticky: closed {len(open_now)} tab(s). "
              f"Bring them back with: sticky-chat resume --last")
    return 0


def cmd_resume(args) -> int:
    """Reopen the tabs that were open before, each on its own conversation.

    Every tab is offered in turn, because after a restart you rarely want all
    of them back; answering `a` takes the rest without asking again.
    """
    records = known_windows()
    if getattr(args, "last", False):
        records = [r for r in records if r.get("open_at_quit")]
        if not records:
            print("sticky: no tabs were open at the last quit")
            return 0
    if not records:
        print("sticky: no tabs remembered yet")
        return 0

    stale = [r for r in records if time.time() - r.get("last_seen", 0) > YEAR]
    if stale and not args.all:
        answer = ask(f"sticky: {len(stale)} tab(s) untouched for a year. "
                     f"Forget them?", "n/y")
        if answer == "y":
            for record in stale:
                path = os.path.join(windows_dir(),
                                    f"{record.get('session')}.json")
                try:
                    os.unlink(path)
                except OSError:
                    pass
            records = [r for r in records if r not in stale]

    tm = Tmux(args.socket)
    # Asking for the set you just closed is answer enough; --all means the
    # same for everything remembered.
    take_all = args.all or getattr(args, "last", False)
    opened = 0
    for record in records:
        project = record.get("project", "?")
        if not take_all:
            seen = time.strftime("%d %b", time.localtime(
                record.get("last_seen", 0)))
            answer = ask(f"  {project}  (last open {seen})", "y/n/a/q")
            if answer == "q":
                break
            if answer == "a":
                take_all = True
            elif answer != "y":
                continue
        agent = agent_named(record.get("agent"))
        if not names_a_session(resume_command(record, agent), agent):
            # The line says nothing about which conversation to open, so
            # this tab comes back - same project, same notes - with a new
            # one in it. Said only when it is true: an agent that can be
            # asked for the last conversation here, or for one by the id
            # read off its transcript, is not starting fresh at all.
            print(f"sticky: {project}: {agent.label} cannot be asked for a "
                  f"conversation by name; the tab and its notes come back, "
                  f"the conversation starts fresh")
        left, said = reopen_tab(tm, record, args.client)
        if said:
            print(f"sticky: {project}: {said}")
        if left:
            opened += 1
        else:
            print(f"sticky: {project}: could not start", file=sys.stderr)

    if not opened:
        return 0
    if args.detach:
        return 0
    return attach(tm, None, args.client)


def tell(tm: Tmux, client: str | None, message: str) -> None:
    """Say something where it will be read.

    A command run from a key binding has no terminal of its own, and one run
    from a prompt pane has one that closes with it a moment later; tmux's
    message line outlives both. Without a client - `sticky-chat reopen` typed
    in a shell - the terminal that is there is the right place after all.
    """
    if client:
        tm.ok("display-message", "-c", client, f"sticky: {message}")
    else:
        print(f"sticky: {message}", file=sys.stderr)


def ago(when: float) -> str:
    """How long since a tab was last seen, in one short phrase.

    Coarse on purpose. The picker is answering "which of these was I in this
    morning", not "when exactly", and a column of timestamps would cost the
    room the rest of the row needs.
    """
    gap = time.time() - when
    if gap < 90:
        return "just now"
    if gap < 3600:
        return f"{int(gap // 60)}m ago"
    if gap < 86400:
        return f"{int(gap // 3600)}h ago"
    if gap < 30 * 86400:
        return f"{int(gap // 86400)}d ago"
    return time.strftime("%d %b", time.localtime(when))


def tab_name(record: dict) -> str:
    """What to call a remembered tab: its window name, or its project's."""
    return (record.get("name")
            or os.path.basename(record.get("project", "").rstrip("/"))
            or "?")


def comes_back(record: dict, agent: Agent, command: str) -> str:
    """What reopening this tab brings back, for the picker's last column.

    Three answers, and `resume_command` has already chosen between them: a
    line naming an id reopens that very conversation, a line asking for one
    without naming it - `--continue`, `codex resume --last` - reopens the
    last conversation in that project, and a line saying nothing about
    conversations at all brings the tab and its notes back around a fresh
    one. Read off the command that is about to run rather than worked out
    again, so the row cannot promise what the line does not do.
    """
    if session_named(command, agent):
        return "same conversation"
    if names_a_session(command, agent):
        return "its last conversation"
    return "a new conversation"


def column(text: str, width: int) -> str:
    """`text` cut to `width` columns and padded out to them.

    Padded by what is drawn rather than by how many characters there are:
    the rows carry colour, and a project name is whatever the directory is
    called, which need not be one column per character.
    """
    text = truncate(text, width)
    return text + " " * max(0, width - visible(text))


def tab_row(number: int, record: dict) -> str:
    """One line of the picker: which tab, on what, how old, what comes back."""
    agent = agent_named(record.get("agent"))
    command = resume_command(record, agent)
    return (f"{number:>3}  {column(tab_name(record), 22)}  "
            f"{column(agent.name, 7)} {ago(record.get('last_seen', 0)):>8}  "
            f"{DIM}{comes_back(record, agent, command)}{RESET}")


def reopenable(tm: Tmux) -> list[dict]:
    """The remembered tabs worth offering, newest first.

    Two are left out. One already on screen, because reopening it would put
    a second tab on a conversation that is still running - asked of the panes
    rather than guessed from the record, which a live sidebar keeps warm. And
    one whose project directory has gone, because `open_tab` refuses those
    and a row that cannot be chosen is a row in the way.
    """
    here = tabs_showing(tm)
    out = []
    for record in known_windows():
        session = record.get("session", "")
        if not session or session in here or record.get("store") in here:
            continue
        if os.path.isdir(record.get("project", "")):
            out.append(record)
    return out


def pick_tab(records: list[dict]) -> dict | None:
    """List the remembered tabs and read which one to open. None to cancel.

    Numbered newest first, because the tab you want back is nearly always
    the one that went away last - so Enter with nothing typed takes the first
    of them. A number that is not on the list is said so with what you typed
    still on the line to correct, exactly as a path that does not exist is at
    the new-tab prompt: the alternative is a prompt that closes having opened
    nothing, leaving you to work out which of the two happened.

    Only what fits in the pane is offered. Choosing by a number you cannot
    see is not choosing, and `resume` is still there for the whole list.
    """
    width, height = terminal_size()
    shown = records[:max(1, height - 4)]     # the title, the gap, the prompt
    print(f"{BOLD}Reopen a tab{RESET}  {DIM}Enter takes the first \u00b7 "
          f"a number picks \u00b7 Esc cancels{RESET}\n")
    for number, record in enumerate(shown, 1):
        print(truncate(tab_row(number, record), width))
    if len(shown) < len(records):
        print(f"{DIM}  \u2026 and {len(records) - len(shown)} older; "
              f"sticky-chat resume offers them all{RESET}")
    print()
    typed = ""
    while True:
        outcome, typed = prompt_line("tab> ", typed)
        if outcome != "save":
            return None                 # Esc, or the pane closed: open nothing
        text = typed.strip()
        if not text:
            return shown[0]
        if text.isdigit() and 1 <= int(text) <= len(shown):
            return shown[int(text) - 1]
        print(f"{DIM}no tab numbered {text}{RESET}\n")


def open_reopen_prompt(tm: Tmux, binary: str, args) -> int:
    """Put the picker on screen as a floating pane, carrying its context.

    The same shape as the new-tab prompt and for the same reason: the key
    binding arrives through run-shell, which is the thing that expands the
    `#{}` in what it runs, so by the time we are here the socket, the client
    and the pane are real strings that can simply be handed over. Naming a
    popup in the config instead hands sticky those braces as text - and a
    popup would swallow the click that closes it besides.
    """
    command = [binary, "reopen"]
    if args.socket:
        command += ["--socket", args.socket]
    if getattr(args, "client", None):
        command += ["--client", args.client]
    try:
        open_prompt(tm, command, getattr(args, "source_pane", None), "40%")
    except RuntimeError:
        tell(tm, getattr(args, "client", None),
             "could not open the reopen prompt")
        return 1
    return 0


def cmd_reopen(args) -> int:
    """Pick one remembered tab and put it back, without leaving the session.

    `resume` is these same records asked about one at a time, in a terminal,
    at the start of a session. This is the one you want now: `C-g o` opens
    the picker in the same floating pane the new-tab key uses, and opening
    is `resume`'s own path, so a tab comes back here exactly as it would
    there.
    """
    tm = Tmux(args.socket)
    records = reopenable(tm)
    if not records:
        # Asked before the pane rather than inside it, so the key does not
        # flash an empty prompt open to say there was nothing to pick.
        tell(tm, args.client,
             "nothing to reopen: no remembered tab that is not already open")
        return 0
    if args.ask:
        return open_reopen_prompt(tm, self_path(), args)

    record = pick_tab(records)
    if record is None:
        return 0                        # cancelled: nothing was opened
    left, said = reopen_tab(tm, record, args.client)
    if not left:
        tell(tm, args.client, f"{tab_name(record)}: could not start")
        return 1
    if said:
        tell(tm, args.client, f"{tab_name(record)}: {said}")
    if args.detach:
        print(left)
        return 0
    return attach(tm, tm.fmt(left, "#{session_name}:#{window_index}"),
                  args.client)


def cmd_reload(args) -> int:
    """Re-apply everything sticky owns to a session that is already running.

    The generated config is rewritten and sourced, and every sidebar is
    closed and reopened so it comes back running the current code. The Claude
    panes are left alone, so this is safe to run mid-conversation - it is the
    loop to use while working on sticky itself.
    """
    tm = Tmux(args.socket)
    config = write_config(self_path())
    if not tm.server_running():
        if not args.quiet:
            print(f"sticky: wrote {config} (no server running)")
        return 0
    tm.run("source-file", config)
    install_hooks(tm)

    fields = ("#{pane_id}\t#{@sticky_role}\t#{@sticky_partner}\t"
              "#{@sticky_project}\t#{@sticky_width}\t#{@sticky_store}")
    rows = [(line.split("\t") + [""] * 6)[:6]
            for line in tm.run("list-panes", "-a", "-F", fields).splitlines()
            if line.strip()]
    sidebars = {row[2]: row for row in rows if row[1] == "sidebar"}

    restarted = 0
    for row in rows:
        pane, role, _partner, project, width, store = row
        if role != "claude":
            continue
        old = sidebars.get(pane)
        if old:
            width = width or old[4]
            store = store or old[5]
            project = project or old[3]
            tm.ok("kill-pane", "-t", old[0])
        open_sidebar(tm, self_path(), pane,
                     project or resolve_project(tm, pane, None),
                     int(width or DEFAULT_SIDEBAR_WIDTH), store or None)
        restarted += 1

    if not args.quiet:
        print(f"sticky: reloaded {config}, restarted {restarted} sidebar(s)")
    return 0


def cmd_restore(args) -> int:
    """Put the sidebar back after it was closed."""
    tm = Tmux(args.socket)
    if not tm.server_running():
        die("no sticky session; run: sticky-chat start")
    target = args.pane or f"{SESSION_NAME}:"
    listing = tm.run("list-panes", "-t", target, "-F",
                     "#{pane_id}\t#{@sticky_role}\t#{@sticky_project}")
    panes = [line.split("\t") for line in listing.splitlines() if line.strip()]
    for row in panes:
        if len(row) > 1 and row[1] == "sidebar":
            if not args.quiet:
                print(f"sticky: the sidebar is already open ({row[0]})")
            return 0
    left = next((row[0] for row in panes if len(row) > 1 and row[1] == "claude"),
                panes[0][0] if panes else None)
    if not left:
        die("no pane to attach a sidebar to")
    project = resolve_project(tm, left, args.project)
    width = tm.option(left, "@sticky_width") or str(args.sidebar_width)
    right = open_sidebar(tm, self_path(), left, project, int(width),
                         tm.option(left, "@sticky_store") or None)
    if not args.quiet:
        print(right)
    return 0


def ask_project(tm: Tmux, source: str | None, given: str | None) -> str:
    """Ask which project a new tab is for, completing paths on Tab.

    tmux's own command-prompt cannot complete a filename, so a path with one
    letter wrong came back as a failed run-shell somewhere off screen, and no
    tab. A prompt of our own can: it has a real terminal, so Tab works, and a
    path that does not exist is said so on the spot, with what you typed
    still on the line to correct. Empty string means it was cancelled.
    """
    start = given or ""
    if not start and source:
        try:
            start = tm.fmt(source, "#{pane_current_path}")
        except RuntimeError:
            start = ""
    print(f"{BOLD}New tab{RESET}  {DIM}Tab completes \u00b7 Enter opens "
          f"\u00b7 Esc cancels{RESET}\n")
    text = start or os.getcwd()
    while True:
        outcome, typed = prompt_line("project> ", text,
                                     complete=complete_path)
        if outcome != "save":
            return ""
        text = typed.strip()
        if not text:
            return ""
        path = os.path.abspath(os.path.expanduser(text))
        if os.path.isdir(path):
            return path
        print(f"{DIM}no such directory: {path}{RESET}\n")


def open_project_prompt(tm: Tmux, binary: str, args) -> int:
    """Open the prompt for a new tab's project, carrying its flags along.

    The key binding arrives through run-shell, which has no terminal of its
    own, so the asking needs a pane. It is opened from here rather than named
    in the config because only run-shell expands the `#{}` in what it runs -
    by the time we are here the socket, the client and the pane are real
    strings, and can simply be handed over.
    """
    command = [binary, "new", "--ask-here"]
    if args.socket:
        command += ["--socket", args.socket]
    if getattr(args, "client", None):
        command += ["--client", args.client]
    if getattr(args, "source_pane", None):
        command += ["--from", args.source_pane]
    if args.virtual_rows:
        command += ["--virtual-rows", str(args.virtual_rows)]
    if args.sidebar_width != DEFAULT_SIDEBAR_WIDTH:
        command += ["--sidebar-width", str(args.sidebar_width)]
    if getattr(args, "local", False):
        command.append("--local")
    if getattr(args, "menus", False):
        command.append("--menus")
    if getattr(args, "agent", None):
        command += ["--agent", args.agent]
    if getattr(args, "agent_cmd", None):
        command += ["--agent-cmd", args.agent_cmd]
    if args.dir:
        command.append(args.dir)

    try:
        open_prompt(tm, command, getattr(args, "source_pane", None), "40%")
    except RuntimeError:
        tm.ok("display-message", "sticky: could not open the project prompt")
        return 1
    return 0


def cmd_sweep(args) -> int:
    """Close the panes that have nothing left to do.

    Two of them. A sidebar whose Claude has ended, because a tab is the two
    panes together - run from `pane-exited`, and asked the other way round,
    of the sidebars, because the pane that died cannot be asked anything by
    then and `#{pane_id}` in a hook is whatever happens to be active rather
    than what went. And a note prompt you have clicked away from - run from
    `window-pane-changed`, which is the event a popup could never give us.
    """
    tm = Tmux(args.socket)
    if not tm.server_running():
        return 0
    rows = [line.split("\t") for line in tm.run(
        "list-panes", "-a", "-F",
        "#{pane_id}\t#{@sticky_role}\t#{@sticky_partner}\t"
        "#{?pane_active,1,0}").splitlines()
        if line.strip()]
    alive = {row[0] for row in rows}
    closed = 0
    for row in rows:
        pane, role, partner, active = [*row, "", "", ""][:4]
        orphaned = role == "sidebar" and partner and partner not in alive
        # A prompt you have clicked away from is a prompt you are done with.
        # Closing it sends SIGHUP to what is running there, which is what
        # saves the note: this is the whole of "click outside to finish".
        finished = role == "note" and active != "1"
        if orphaned or finished:
            tm.ok("kill-pane", "-t", pane)
            closed += 1
        if finished:
            # The click that dismissed it is on its way to `cmd_click` as
            # well. One click should do one thing, so leave a mark it can
            # see - the server is the only thing both processes share, and
            # they are racing, so the pane being gone is not enough.
            tm.ok("set-option", "-g", "@sticky_dismissed", str(time.time()))
    if not getattr(args, "quiet", False):
        print(f"sticky: closed {closed} pane(s) with nothing left to do")
    return 0


def cmd_start(args) -> int:
    tm = Tmux(args.socket)
    binary = self_path()
    if getattr(args, "ask", False):
        return open_project_prompt(tm, binary, args)
    if getattr(args, "ask_here", False):
        project = ask_project(tm, getattr(args, "source_pane", None), args.dir)
        if not project:
            return 0                    # cancelled: nothing was opened
    else:
        project = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(project):
        die(f"not a directory: {project}")
    config = write_config(binary)
    if args.local:
        os.makedirs(os.path.join(project, ".sticky"), exist_ok=True)

    extra = list(getattr(args, "claude_args", None) or [])
    inherited = ""
    source = getattr(args, "source_pane", None)
    # Which agent is in the pane, and with it every difference between one
    # agent and the next. A tab opened from a running one takes that tab's
    # profile unless told otherwise, so `C-g c` beside a Gemini tab opens
    # another Gemini tab rather than quietly falling back to Claude.
    agent = require_agent(getattr(args, "agent", None)
                          or (agent_of(tm, source).name if source else ""))
    if not agent.tested:
        # Say which part is unproven where that is known. "Untested" over a
        # profile that has in fact been started and resumed reads as a
        # warning about the wrong thing.
        print(f"sticky: the {agent.name} profile is not fully tested: "
              f"{agent.caveat}." if agent.caveat else
              f"sticky: nobody has run sticky-chat on the {agent.name} "
              f"profile: what is in it was read off {agent.label}'s own "
              f"documentation rather than tried. Expect to fix it.",
              file=sys.stderr)
    if source and not extra and not args.agent_cmd:
        # Opened from a running tab: the same agent with the same arguments,
        # but not the same conversation. The id belongs to the tab still
        # running it, and the agent will not open it twice.
        inherited = without_session(
            launched_as(tm, claude_pane(tm, source) or source, agent), agent)
    if inherited:
        command = inherited
    else:
        program = args.agent_cmd or agent.command
        if not program:
            die(f"the {agent.name} profile has no command of its own; say "
                f"what to run with --agent-cmd")
        base = [program]
        if not args.agent_cmd and not args.menus:
            # The agent answers in prose instead of opening a question menu,
            # which a sidebar full of notes has no way to answer.
            base += list(agent.no_menu_args)
        command = " ".join(shell_quote(c) for c in base + extra)

    # An agent picks a conversation id of its own unless told one. Telling it
    # means the tab can be asked for by name after a reboot; anything the
    # user said about sessions wins over that, and an agent whose profile
    # knows no such flag is left to name its own conversation - the tab then
    # gets the `tab-` key below, exactly as a bare --continue does.
    session = getattr(args, "session", "") or session_named(command, agent)
    if (agent.names_sessions and not session
            and not names_a_session(command, agent)):
        session = str(uuid.uuid4())
        command = with_words(command, [agent.session_flag, session], agent)

    # Notes belong to the conversation, not to the directory it runs in, so a
    # new tab starts with an empty sidebar and a resumed one - same id, same
    # directory - finds its notes waiting. A bare --continue is the one tab
    # whose id we cannot know yet: it gets a directory of its own anyway, and
    # the window record below is what leads `resume` back to it.
    unknown = f"tab-{uuid.uuid4().hex[:12]}"
    store_dir = (getattr(args, "store", None)
                 or session_dir(project, session or unknown))
    name = os.path.basename(project.rstrip("/")) or "root"
    env = pane_env(agent)

    # Capture the new pane's id directly: window names are not unique, so
    # looking one up by name can land in the wrong window.
    launch = (prime_rows(command, args.virtual_rows)
              if args.virtual_rows else command)
    launched = time.time()              # before the pane, as in `open_tab`
    if not tm.server_running():
        left = tm.run("-f", config, "new-session", "-d", "-s", SESSION_NAME,
                      "-n", name, "-c", project, *env,
                      "-P", "-F", "#{pane_id}", launch).strip()
    else:
        left = tm.run("new-window", "-d", "-t", f"{SESSION_NAME}:", "-n", name,
                      "-c", project, *env,
                      "-P", "-F", "#{pane_id}", launch).strip()
    time.sleep(0.4)                     # a refused session dies about now
    if not tm.pane_exists(left):
        # From a key binding there is no terminal to print to, so say it in
        # tmux's own message line instead of failing silently.
        if getattr(args, "client", None):
            tm.ok("display-message", "-c", args.client,
                  f"sticky: {command} exited immediately")
        die(f"the command exited immediately: {command}\n"
            f"       check that it runs on its own, or say what to run "
            f"with --agent-cmd")
    install_hooks(tm)                   # the server exists now, so it can
    window = tm.fmt(left, "#{session_name}:#{window_index}")
    tm.set_option(left, "@sticky_agent_cmd", command)
    tm.set_option(left, "@sticky_session", session or unknown)
    tm.set_option(left, "@sticky_agent", agent.name)   # before the sidebar
    mark_launch(tm, left, agent, launched)             # and before it, too
    open_sidebar(tm, binary, left, project, args.sidebar_width, store_dir)
    # A bare --continue leaves the id with the agent, so the tab is remembered
    # under a key of our own and marked as what it is. Without a key nothing
    # is written at all, and the notes taken in it are lost at the next
    # restart along with any way of finding the directory they went to.
    record_window(session or unknown, project=project, command=command,
                  store=store_dir, continued=not session, agent=agent.name,
                  width=args.sidebar_width, virtual_rows=args.virtual_rows,
                  name=name)

    if args.virtual_rows:
        virtual_window(tm, window, args.virtual_rows)

    if args.detach:
        print(left)
        return 0
    return attach(tm, window, getattr(args, "client", None))


def attach(tm: Tmux, window: str | None, client: str | None = None) -> int:
    flag = "-S" if "/" in tm.socket else "-L"
    if client or os.environ.get("TMUX"):
        if window:
            target = ["-c", client] if client else []
            tm.run("switch-client", *target, "-t", window)
        return 0
    if window:
        try:
            tm.run("select-window", "-t", window)
        except RuntimeError:
            pass
    cmd = ["tmux", flag, tm.socket, "attach-session", "-t", SESSION_NAME]
    return subprocess.call(cmd)


def cmd_fit(args) -> int:
    """Put the sidebars and the viewport back; run from tmux's hooks."""
    tm = Tmux(args.socket)
    try:
        listing = tm.run("list-panes", "-a", "-F",
                         "#{pane_id}\t#{@sticky_role}\t#{@sticky_width}\t"
                         "#{window_zoomed_flag}\t#{pane_pid}")
    except RuntimeError:
        return 0
    woken = []
    for line in listing.splitlines():
        parts = [*line.split("\t"), "", "", "", "", ""][:5]
        if parts[1] != "sidebar":
            continue
        woken.append(parts[4])
        if parts[3] == "1":
            continue
        try:
            width = int(parts[2])
        except ValueError:
            continue
        tm.ok("resize-pane", "-t", parts[0], "-x", str(width))
    fit_windows(tm)
    pin_clients(tm)
    # The hooks that call this are the ones that change how much room the
    # sidebar has. Nothing else would tell it, and it no longer looks.
    for pid in woken:
        if pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGUSR1)
            except OSError:
                pass
    return 0


def cmd_attach(args) -> int:
    tm = Tmux(args.socket)
    if not tm.server_running():
        die("no sticky session; run: sticky-chat start")
    return attach(tm, None)
