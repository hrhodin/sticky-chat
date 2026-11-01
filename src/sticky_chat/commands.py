"""One function per subcommand."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .agents import (
    AGENTS,
    CLAUDE,
    DEFAULT_AGENT,
    Agent,
    agent_named,
    installed_agents,
    known_name,
    require_agent,
    unix_time,
)
from .clipboard import to_clipboard
from .config import (
    DEFAULT_SIDEBAR_WIDTH,
    FOCUS_STYLE,
    client_area,
    count_rows,
    fit_windows,
    install_hooks,
    pin_clients,
    prime_rows,
    room_for_new_windows,
    use_client_size,
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
from .sandbox import (
    BWRAP,
    DEFAULT_IMAGE,
    FENCE_NOTES,
    FENCES,
    OFF,
    agent_home,
    build_image,
    carry_history,
    default_fence,
    dockerfile_path,
    engine_named,
    ensure_image,
    fence_choices,
    fence_named,
    fenced_by,
    home_of,
    image_built,
    inner_command,
    make_home,
    refence,
    rejoin,
    remember,
    remembered,
    runs_in_image,
    split_command,
    suggested_fence,
    unavailable,
    unreachable,
    warm_up,
)
from .sidebar import AUTO_FOREVER, AUTO_STEP
from .store import (
    Store,
    agent_of,
    claude_pane,
    in_tab_order,
    known_windows,
    last_run,
    launched_as,
    names_a_session,
    open_store,
    record_window,
    resolve_project,
    session_dir,
    session_named,
    sidebar_showing,
    tab_pane,
    tabs_showing,
    windows_dir,
    with_words,
    without_session,
)
from .tmux import SESSION_NAME, Tmux
from .util import (
    BOLD,
    CLOCK_HIT,
    COL_COMMITTED,
    COL_PENDING,
    DIM,
    RESET,
    RESET_ROWS,
    complete_path,
    die,
    log_line,
    prompt_line,
    reset_time,
    self_path,
    shell_quote,
    terminal_size,
    terminal_width,
    truncate,
    visible,
    when_to_send,
    wrap,
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
    hint = ("Enter saves \u00b7 C-s sends \u00b7 alt-enter sends and enters"
            "\u00b7 C-c copies")
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


def ask_when(tm: Tmux, pane: str | None) -> str:
    """Ask when the batch should go. "" means cancelled: nothing is set.

    The line starts filled in with what the agent itself said about its
    limit, read off the pane the notes were taken in - `reset_time` is
    where that reading is done, and why getting it wrong costs nothing. A
    time that cannot be read is said so on the spot with what you typed
    still there to correct, which is how the project prompt does it.
    """
    guess = ""
    if pane:
        try:
            # Joined, because the longest of these notices is wider than
            # a pane with a sidebar beside it: unwrapped, the word "limit"
            # and the time it is about land on different rows and neither
            # half is a notice on its own. And a little history as well as
            # the screen, because opening the sidebar narrows the pane -
            # tmux reflows it, and the first half of something printed
            # before that has been pushed above the top row.
            #
            # `reset_time` takes the last rows of what comes back that have
            # anything on them: a pane with room to spare ends in blanks.
            height = int(tm.fmt(pane, "#{pane_height}") or 0)
            guess = reset_time(tm.capture(pane, -RESET_ROWS, height - 1,
                                          joined=True))
        except (RuntimeError, ValueError):
            guess = ""                   # a pane gone: still worth asking
    print(f"{BOLD}Send later{RESET}  {DIM}Enter sets it \u00b7 Esc "
          f"cancels{RESET}\n")
    print(f"{DIM}  4h \u00b7 90m \u00b7 8pm \u00b7 00:32 \u00b7 "
          f"2026-09-16 00:32{RESET}")
    if guess:
        print(f"{DIM}  the pane says the limit resets at {guess}{RESET}")
    set_for = (tm.option(pane, "@sticky_send_at") if pane else "")
    set_for = set_for.partition(":")[0]
    if set_for.replace(".", "", 1).isdigit():
        # The only way to call one off without leaving the tab, so the
        # word is said where it is needed and nowhere else.
        print(f"{DIM}  cancel calls off the one set for "
              f"{time.strftime('%H:%M', time.localtime(float(set_for)))}"
              f"{RESET}")
    print()

    text = guess
    while True:
        outcome, typed = prompt_line("send at> ", text)
        if outcome != "save":
            return ""                    # Esc, and anything but Enter
        text = typed.strip()
        if not text:
            return ""
        if text.lower() == "cancel" or when_to_send(text):
            return text
        print(f"{DIM}cannot read a time from {text!r}{RESET}\n")


def cmd_commit(args) -> int:
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane)
    store = open_store(tm, args.pane, args.project,
                       getattr(args, 'store', None))
    notes = store.load()

    # `t` in the sidebar, in two steps, the way the project prompt is: this
    # one opens a pane, and what runs in it is this same command with the
    # other flag. A key binding has no terminal of its own to ask on.
    if getattr(args, "ask_at", False):
        command = [self_path(), "commit", "--ask-at-here"]
        for flag, value in (("--socket", args.socket), ("--pane", pane),
                            ("--project", args.project),
                            ("--store", getattr(args, "store", None))):
            if value:
                command += [flag, value]
        if getattr(args, "to_tab", None):
            # Where it goes is asked here, not in there: the prompt is
            # about when, and a flag accepted and quietly dropped is worse
            # than one that was never taken.
            command += ["--to-tab", str(args.to_tab)]
        try:
            # Taller than the note prompt's default, because this one has
            # a line of shapes and a line of where the guess came from
            # above the line you type on.
            open_prompt(tm, command, pane, "40%")
        except RuntimeError:
            tm.ok("display-message", "sticky: could not open the prompt")
            return 1
        return 0

    # Later, rather than now. The deadline lives on the pane, so it
    # survives the sidebar being restarted and dies with the tab it belongs
    # to; the sidebar is what watches the clock, being the one thing here
    # that is already awake and already has a select timeout to cap.
    asked = getattr(args, "ask_at_here", False)
    said = ask_when(tm, pane) if asked else getattr(args, "at", None)
    if said:
        def told(message: str) -> int:
            """Say what was done, where it will be read.

            The prompt's pane closes as this returns, so anything printed
            there goes with it: from in here the answer belongs on the
            status line. The sidebar says the rest for as long as it is
            true: a row naming what is going and how long until it, and
            `↳ at HH:MM` in the footer.
            """
            if asked:
                tm.ok("display-message", f"sticky: {message}")
            elif not args.quiet:
                print(f"sticky: {message}")
            return 0

        if said.strip().lower() == "cancel":
            tm.ok("set-option", "-p", "-u", "-t", pane, "@sticky_send_at")
            nudge(tm, pane, store)
            return told("not sending on a timer any more")
        when = when_to_send(said)
        if not when:
            die(f"cannot read a time from {said!r}: try 4h, 90m, 8pm, "
                f"00:32 or 2026-09-16 00:32")
        waiting = [n for n in notes if n["status"] == "pending"
                   and n["note"].strip() and not n.get("deleted")]
        tm.ok("set-option", "-p", "-t", pane, "@sticky_send_at",
              f"{when:.0f}:{getattr(args, 'to_tab', None) or ''}")
        nudge(tm, pane, store)
        return told(
            f"{len(waiting)} note(s) will go at "
            f"{time.strftime('%H:%M on %a %d %b', time.localtime(when))}")
    if asked:
        return 0                          # Esc: no timer, nothing changed

    # Where the notes are read from and where they are handed to are two
    # different questions. Normally the same tab answers both; `--to-tab`
    # says the notes were taken here and belong over there - a shell you
    # were reading output in, handed to whichever agent should see it.
    target = pane
    wanted = getattr(args, "to_tab", None)
    if wanted:
        target = tab_pane(tm, wanted)
        if not target:
            message = f"no agent in tab {wanted}"
            if getattr(args, "client", None):
                tm.ok("display-message", "-c", args.client,
                      f"sticky: {message}")
            print(f"sticky: {message}", file=sys.stderr)
            return 1

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

    if not args.no_paste and target and tm.pane_exists(target):
        fd, tmp = tempfile.mkstemp(prefix="sticky-commit-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            # Reading back is over the moment you send: leave copy mode, or
            # the block would land in a prompt you cannot see.
            tm.ok("send-keys", "-X", "-t", target, "cancel")
            tm.run("load-buffer", "-b", "sticky", tmp)
            tm.run("paste-buffer", "-p", "-b", "sticky", "-t", target)
            expand_paste(tm, target, agent_of(tm, target))
            tm.ok("delete-buffer", "-b", "sticky")
            if args.send:
                time.sleep(0.15)      # let Claude take the bracketed paste in
                tm.run("send-keys", "-t", target, "Enter")
            # Either way you end up at the prompt it went to, which is
            # where the answer is going to appear - another tab included.
            tm.ok("select-pane", "-t", target)
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


def tab_index(tm: Tmux, pane: str) -> int | None:
    """Which tab a pane is in, as a number, or None if it cannot be asked.

    Recorded so that a set of tabs put back by `resume` comes back in the
    order it sat in rather than the order it was last touched. A number
    rather than a name: two tabs on one project have the same name, and the
    number is also what you press in the sidebar to send notes to it.
    """
    try:
        said = tm.fmt(pane, "#{window_index}").strip()
    except RuntimeError:
        return None
    return int(said) if said.isdigit() else None


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

    Here they are struck out. Carrying a question to a fork is a way of
    saying it belongs over there, and two tabs both holding it pending is
    two places to answer it and one of them wrong. Struck rather than
    removed, because striking is a thing you can undo: `x` on the row
    brings one back if the fork was not where it belonged after all.
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
    tm.set_option(left, "@sticky_home", home_of(base))
    tm.set_option(left, "@sticky_session", key)
    tm.set_option(left, "@sticky_agent", agent.name)
    # On the window, not the pane: the status line resolves a format
    # against whichever pane is active, and half of a sticky window is
    # the sidebar.
    tm.ok("set-option", "-w", "-t", left, "@sticky_mark", agent.mark)
    count_rows(tm)      # the tab is an agent tab as of now
    # A branch is a new conversation with a new id, and on an agent that
    # names its own it is exactly the id we do not have - so a fork looks for
    # one the same way a fresh tab does.
    mark_launch(tm, left, agent, launched)
    open_sidebar(tm, self_path(), left, project, int(width), fork_dir)

    # The notes went with the fork, so here they are struck out rather than
    # left standing. Two tabs both holding the same question pending is two
    # places to answer it and one of them wrong, and what a fork is for is
    # asking it over there. Struck rather than removed because striking is
    # a thing you can undo: `x` on the row brings one back if the fork was
    # not where it belonged after all.
    if carried:
        moved = {note["id"] for note in carried}
        notes = origin.load()
        for note in notes:
            if note["id"] in moved:
                note["deleted"] = True
        origin.save(notes)
        nudge(tm, source)
    # And what `resume` re-runs. Claude's whole line is kept because
    # `resume_command` drops the branch words from it and asks for the id
    # instead; with no id to ask for there is nothing to drop them by, so
    # they are dropped here rather than at every restart from now on.
    record_window(key, project=project,
                  command=command if session else base, store=fork_dir,
                  continued=not session,
                  width=int(width), virtual_rows=virtual, agent=agent.name,
                  name=f"{name}-fork", index=tab_index(tm, left))

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


def stand_down(tm: Tmux, pane: str, store) -> int:
    """The clock row clicked: off if it is on, on if it is off.

    The time itself is not thrown away, only moved aside, because a clock
    you have turned off is one you may well want back and the hour it was
    set for is not a thing anybody should have to remember. Nothing reads
    the value where it is parked except the row that draws it struck out.

    Off is off: the sidebar arms a tab by scanning it when it goes quiet,
    and would otherwise find the same notice a minute later and set the
    whole thing going again.
    """
    target = claude_pane(tm, pane) or pane
    # A double click arrives as two presses, and turning a thing off and
    # straight back on again is not what anybody meant by it.
    guard = os.path.join(store.dir, ".last-clock")
    try:
        with open(guard) as fh:
            if time.time() - float(fh.read()) < 1.0:
                return 0
    except (OSError, ValueError):
        pass
    try:
        with open(guard, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass

    armed = (tm.option(target, "@sticky_send_at") or "").strip()
    stood = (tm.option(target, "@sticky_send_at_off") or "").strip()
    if armed:
        tm.ok("set-option", "-p", "-t", target, "@sticky_send_at_off", armed)
        tm.ok("set-option", "-p", "-u", "-t", target, "@sticky_send_at")
        tm.ok("set-option", "-w", "-u", "-t", target, "@sticky_waiting")
    elif stood:
        tm.ok("set-option", "-p", "-t", target, "@sticky_send_at", stood)
        tm.ok("set-option", "-p", "-u", "-t", target, "@sticky_send_at_off")
        if stood.partition(":")[2].partition(":")[2]:
            # Only the kind that restarts an agent wears the clock on its
            # tab; a batch of notes on a timer never has.
            tm.ok("set-option", "-w", "-t", target, "@sticky_waiting", "1")
    else:
        return 0
    nudge(tm, pane)
    return 0


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
    if hit["id"] == CLOCK_HIT:
        return stand_down(tm, pane, store)
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
        # The agent's own half of the line: a sandboxed tab comes back
        # sandboxed, because what fences it is kept and only what it runs is
        # rewritten.
        fence, inside = split_command(command)
        words, out = inside.split(), []
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
        return with_words(rejoin(fence, " ".join(out)),
                          [agent.resume_flags[0], named], agent)
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
    make_home(command)                  # a tab put back is fenced as it was
    # Before the pane, so that a transcript the agent opens a moment later is
    # newer than this and one written yesterday is not.
    launched = time.time()
    config = write_config(self_path())
    name = record.get("name") or os.path.basename(project.rstrip("/"))
    # The room the agent will draw its replay into. A tab is opened
    # detached and draws before anybody looks at it, and what it draws at 80
    # columns stays wrapped at 80 for good - see `room_for_new_windows`.
    cols, rows = room_for_new_windows(tm)
    if not tm.server_running():
        left = tm.run("-f", config, "new-session", "-d", "-s", SESSION_NAME,
                      "-x", str(cols), "-y", str(rows),
                      "-n", name, "-c", project, *env,
                      "-P", "-F", "#{pane_id}", launch).strip()
    else:
        use_client_size(tm)
        left = tm.run("new-window", "-d", "-t", f"{SESSION_NAME}:", "-n", name,
                      "-c", project, *env, "-P", "-F", "#{pane_id}",
                      launch).strip()
    time.sleep(0.4)                     # a refused session dies about now
    if not tm.pane_exists(left):
        return ""
    install_hooks(tm)                   # this may be a server we just started
    width = int(record.get("width") or DEFAULT_SIDEBAR_WIDTH)
    tm.set_option(left, "@sticky_agent_cmd", command)
    tm.set_option(left, "@sticky_home", home_of(command))
    tm.set_option(left, "@sticky_session", record.get("session", ""))
    tm.set_option(left, "@sticky_agent", agent.name)      # before the sidebar
    # On the window, not the pane: the status line resolves a format
    # against whichever pane is active, and half of a sticky window is
    # the sidebar.
    tm.ok("set-option", "-w", "-t", left, "@sticky_mark", agent.mark)
    count_rows(tm)      # the tab is an agent tab as of now
    mark_launch(tm, left, agent, launched, record.get("agent_session", ""))
    open_sidebar(tm, self_path(), left, project, width,
                 record.get("store") or None)
    if virtual:
        virtual_window(tm, tm.fmt(left, "#{session_name}:#{window_index}"),
                       virtual)
    record_window(record.get("session", ""), project=project,
                  index=tab_index(tm, left))
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
    # Only the tabs that were up are stamped, and they are stamped as of
    # now, which is when they went. A record merely carrying an older
    # quit's mark has it cleared without being touched: moving its
    # `last_seen` would file a tab closed weeks ago under tonight's run,
    # which is the one thing `last_run` reads.
    for record in known_windows():
        session = record.get("session", "")
        if not session:
            continue
        was_open = session in open_now
        if was_open or record.get("open_at_quit"):
            record_window(session, touch=was_open, open_at_quit=was_open)

    tm.ok("kill-server")
    if not args.quiet:
        print(f"sticky: closed {len(open_now)} tab(s). "
              f"Bring them back with: sticky-chat resume")
    return 0


def run_summary(records: list[dict]) -> str:
    """The last run in one phrase: how many tabs, when, and which.

    Named rather than counted, because "9 tabs" is not something anybody
    can say yes to. Three names is what leaves room for the question on the
    same line; the rest are counted, and the picker lists them properly.
    """
    names = [tab_name(r) for r in records]
    shown = ", ".join(names[:3])
    if len(names) > 3:
        shown += f" and {len(names) - 3} more"
    when = ago(max(r.get("last_seen", 0) for r in records))
    was = "1 tab was" if len(names) == 1 else f"{len(names)} tabs were"
    return f"{was} open {when}: {shown}"


def not_showing(records: list[dict], tm: Tmux) -> list[dict]:
    """The remembered tabs that are not already on screen.

    Reopening a tab that is still running is a second tab on a live
    conversation, which the agent either refuses or - worse - takes. The
    panes are asked rather than the records, because a record's `last_seen`
    is kept warm by the very sidebar that would make it look closed.
    """
    here = tabs_showing(tm)
    return [r for r in records if r.get("session") not in here
            and r.get("store") not in here]


def forget_window(session: str) -> None:
    try:
        os.unlink(os.path.join(windows_dir(), f"{session}.json"))
    except OSError:
        pass


def cmd_resume(args) -> int:
    """Reopen the tabs that were open when sticky last stopped.

    The last run, not the whole history. What you want back after a reboot
    is the desk you left, and every tab you have ever opened offered one at
    a time is a list you answer `q` to. The set goes back as a set, because
    that is how it went away - one question for all of it, `p` to go
    through it tab by tab instead.

    `--all` is the whole history, one at a time, for finding something
    older; `--yes` asks nothing, which is what a login item wants.
    """
    records = known_windows()
    if not records:
        print("sticky: no tabs remembered yet")
        return 0
    yes = bool(getattr(args, "yes", False))

    if args.all:
        stale = [r for r in records
                 if time.time() - r.get("last_seen", 0) > YEAR]
        if stale and not yes:
            answer = ask(f"sticky: {len(stale)} tab(s) untouched for a year. "
                         f"Forget them?", "n/y")
            if answer == "y":
                for record in stale:
                    forget_window(record.get("session", ""))
                records = [r for r in records if r not in stale]
        take_all = yes
    else:
        records = last_run(records)
        take_all = True                 # asked for as a set, just below

    tm = Tmux(args.socket)
    wanted = not_showing(records, tm)
    if not wanted:
        print("sticky: those tabs are open already" if records
              else "sticky: nothing left to reopen")
        return 0

    if not args.all and not yes:
        answer = ask(f"sticky: {run_summary(wanted)}. Reopen them, "
                     f"or p to go through them one at a time?", "y/n/p")
        if answer == "p":
            take_all = False
        elif answer != "y":
            print("sticky: left closed")
            return 0

    if not take_all:
        # Said once, above the walk, rather than four letters at the end of
        # every row that you are left to guess at. `n` is the one that
        # skips; `q` stops the asking, and opens nothing more.
        print("sticky: y reopens it \u00b7 n skips it \u00b7 "
              "a reopens all the rest \u00b7 q stops here")

    opened = 0
    # In the order they sat, not the order they were last touched. `wanted`
    # comes newest-first, which is right for the list above and wrong for
    # the reopening: it is what had a session coming back shuffled.
    for record in in_tab_order(wanted):
        project = record.get("project", "?")
        if not take_all:
            seen = time.strftime("%d %b", time.localtime(
                record.get("last_seen", 0)))
            # Named as the summary above named it, not by its path: the
            # question is which of those tabs this is.
            answer = ask(f"  {tab_name(record)}  (last open {seen})",
                         "y/n/a/q")
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
              f"sticky-chat resume --all offers them all{RESET}")
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


# What a quote can be before it is not worth searching for, and how many
# of them one search will carry.
#
# The search runs inside the tmux server, which does one thing at a time,
# and it runs over the whole scrollback: while it is going, every pane and
# every keystroke waits for it. Measured against an 1867-row history, forty
# whole rows of quote came to a 6599-character expression and blocked the
# server for 1690ms - on every entry into copy mode, which is every turn of
# the wheel. Eight quotes of forty characters is a tenth of that pattern and
# a tenth of the wait, and what it costs is the ninth-oldest note on screen
# not lighting up. The first line of a quote is distinctive long before its
# fortieth character.
MARK_QUOTES = 8

# The most alternative-times-lines of searching a scroll may cost, which is
# how the number of quotes is decided on a pane with a long history: eight
# quotes over two hundred thousand rows is seconds of a frozen server, and
# one quote over the same rows is a fifth of that. A tab with little history
# still gets all eight.
MARK_BUDGET = 120_000


MARK_CHARS = 40


def mark_pattern(notes: list[dict], within: str = "",
                 limit: int = MARK_QUOTES) -> str:
    """One regular expression matching every line that has a note on it.

    tmux's copy-mode search takes an extended regular expression and paints
    every match with `copy-mode-match-style`, so the whole set goes in as
    one alternation. What is matched is the row as it was captured rather
    than the quote as it was stored: the quote is what you selected, which
    may be half a line, and a half-line highlight reads as a mistake.

    Only the notes still open, newest first, and only the ones whose text is
    on `within` - the lines you can see. Everything else is in the sidebar's
    band, which is where a list of notes belongs.

    The number of alternatives is the price. tmux scans the *whole* history
    for a search and marks every match in it, which on a pane holding two
    hundred thousand lines takes seconds inside the server - and the server
    is where every other pane is drawn from. Measured at sixty thousand
    lines: a quarter of a second for one alternative, nearly a second for
    eight, four when the machine is busy. So a quote that is not in front of
    you is never searched for, which in the ordinary case means no search at
    all - and a pattern matching nothing is the most expensive search there
    is.
    """
    def newest(note: dict) -> float:
        # Through `unix_time`, which never raises and takes a number or an
        # ISO-8601 string either way. A store writes `created` as
        # `2026-10-06T00:00:24` and a `float()` of that put a traceback in
        # the pane on every single scroll - the one shape the tests had not
        # used, because they wrote the field as a number.
        return -unix_time(note.get("created"))

    seen, out = set(), []
    for note in sorted(notes, key=newest):
        if note.get("deleted") or note.get("status") == "committed":
            # The ones still open. A note already sent is a note you have
            # dealt with, and lighting it costs the same as lighting one you
            # have not.
            continue
        rows = note.get("rows") or [note.get("quote", "")]
        line = (rows[0] or "").strip()[:MARK_CHARS]
        if len(line) < 3 or line in seen:
            # Two words are matched all over a pane and light up rows that
            # nobody annotated, which is worse than lighting up none.
            continue
        if within and line not in within:
            continue                    # nothing it could light is on screen
        seen.add(line)
        out.append(re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", line))
        if len(out) >= max(1, limit):
            break
    return "|".join(out)


def cmd_marks(args) -> int:
    """Light up the lines that have notes, in the pane you scrolled back in.

    tmux will not restyle a pane that is being written to, but a pane you
    have scrolled back in is in copy mode, and copy mode paints its search
    matches. So the search is set for you on the way in: reading back is
    exactly when knowing which lines you have already marked is worth
    something, and it costs a keystroke you were pressing anyway.
    """
    tm = Tmux(args.socket)
    pane = args.pane
    if not pane:
        return 0
    # Three questions in one invocation: is this pane reading back at all,
    # is a selection being made in it, and where the view and the cursor
    # are - because everything below has to put those two back.
    try:
        state = tm.fmt(pane, "#{pane_in_mode}\t#{selection_present}"
                             "\t#{scroll_position}\t#{copy_cursor_y}"
                             "\t#{history_size}\t#{pane_height}")
    except RuntimeError:
        return 0
    mode, _, rest = state.partition("\t")
    selecting, _, rest = rest.partition("\t")
    scrolled, _, rest = rest.partition("\t")
    cursor, _, deep = rest.partition("\t")
    deep, _, tall = deep.partition("\t")
    if mode.strip() != "1":
        return 0                        # not reading back: nothing to paint
    if selecting.strip() == "1":
        # A selection is being made right now. A search moves the cursor,
        # and a moving cursor with an anchor down takes everything between
        # the two with it: you reach for three words and get the afternoon.
        return 0
    if tm.run("show-options", "-gqv", "@sticky_marks").strip().lower() in (
            "off", "0", "no", "false"):
        return 0
    project = resolve_project(tm, pane, args.project)
    store = open_store(tm, pane, project, getattr(args, "store", None))
    # The rows a mark could possibly land on, in one capture, before tmux is
    # asked to search for anything. This is the whole of the fix for a
    # scroll that froze the session: a search costs the entire history and
    # marks every match in it, so the question worth answering first is
    # whether there is anything to find - and for notes taken further back
    # than you are reading, there is not.
    rows = int(deep) if deep.strip().isdigit() else 0
    if rows > MARK_BUDGET:
        # Further than the lighting is worth. One alternative over two
        # hundred thousand rows is still most of a second with the server
        # blocked, and the server is where every other pane is drawn from -
        # so a tab that has printed that much goes unlit, and its notes are
        # in the sidebar where the list of them belongs.
        return 0
    # The lines you are looking at, and nothing else. One capture of one
    # screen, taken where the scroll has you rather than where the pane is
    # live: reading back ten thousand rows, what is worth lighting is the
    # ten thousandth, not the last. A quote that is not in here is not
    # searched for, which is what stops a scroll freezing the session.
    try:
        back = int(scrolled) if scrolled.strip().isdigit() else 0
        high = int(tall) - 1 if tall.strip().isdigit() else 0
        near = "\n".join(tm.capture(pane, -back, high - back, joined=True))
    except RuntimeError:
        near = ""
    # And how many to ask for, which is the price of the search: a pane with
    # a short history can afford all of them, one with a long one cannot.
    pattern = mark_pattern(store.load(), near,
                           MARK_BUDGET // rows if rows > MARK_BUDGET // MARK_QUOTES
                           else MARK_QUOTES)
    if not pattern:
        return 0

    def number(text: str, fallback: int = 0) -> int:
        text = text.strip()
        return int(text) if text.lstrip("-").isdigit() else fallback

    # A search jumps to a match, and being yanked somewhere else is not what
    # scrolling back means. Both halves of where you were have to go back:
    # the view, and the cursor inside it. Putting only the view back leaves
    # the cursor wherever the match was, which is the next selection you
    # make starting somewhere you have never been.
    was, sat = number(scrolled), number(cursor)
    tm.ok("send-keys", "-t", pane, "-X", "search-backward", pattern)
    after = tm.fmt(pane, "#{scroll_position}\t#{copy_cursor_y}")
    now, _, seat = after.partition("\t")
    now, seat = number(now, was), number(seat, sat)
    if now != was:
        tm.ok("send-keys", "-t", pane, "-X", "-N", str(abs(was - now)),
              "scroll-up" if was > now else "scroll-down")
    if seat != sat:
        tm.ok("send-keys", "-t", pane, "-X", "-N", str(abs(sat - seat)),
              "cursor-up" if sat < seat else "cursor-down")
    if not getattr(args, "quiet", False):
        print("sticky: the annotated lines are lit while you read back")
    return 0


def cmd_trace(args) -> int:
    """Put one line in the sidebar log, from a key binding or a hook.

    A timeline is only worth anything if everything that happened is on the
    same one: the click, the hooks it set off, what `fit` did, and every
    sidebar that woke up because of it. The bindings that call this are
    guarded on the log being on, so nothing is started while it is off.
    """
    tm = Tmux(args.socket)
    path = tm.run("show-options", "-gqv", "@sticky_log").strip()
    if path:
        log_line(path, f"-- {args.label}")
    return 0


def cmd_log(args) -> int:
    """Turn the sidebar's log on or off, or say where it is going.

    A tab that marks itself unread when nothing happened is a thing you can
    only catch in the act: the answer is always "what changed on screen",
    and the screen has moved on by the time anybody asks. So the sidebars
    keep a diff of every change they act on, and the switch is a global
    option they read out of the query they were making anyway - which is
    why this takes effect at once and does not want a reload.
    """
    tm = Tmux(args.socket)
    if not tm.server_running():
        die("no sticky session; run: sticky-chat start")
    if args.off:
        tm.ok("set-option", "-gu", "@sticky_log")
        print("sticky: log off")
        return 0
    if not args.path:
        where = tm.run("show-options", "-gqv", "@sticky_log").strip()
        print(f"sticky: log going to {where}" if where else "sticky: log off")
        return 0
    path = os.path.abspath(os.path.expanduser(args.path))
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        die(f"not a directory: {directory}")
    tm.ok("set-option", "-g", "@sticky_log", path)
    print(f"sticky: log going to {path}")
    return 0


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


def ask_agent(here: str) -> str:
    """Which agent, on one line. Empty if it was cancelled.

    Its own function because two callers ask it: the new-tab prompt, which
    asks all three questions, and `sticky-chat start`, which asks the ones
    the command line did not answer. It opens on the agent in the tab the key
    was pressed in - beside a Gemini tab the answer is almost always Gemini
    again - and Enter takes it, so the common case is the keystrokes it
    always was.

    Only what is installed is offered, because a tab in an agent this machine
    does not have is a tab that exits as it opens. `here` is listed whether
    it is installed or not: it is what the answer will be if Enter is
    pressed, and a list that does not contain the answer on the line reads as
    a bug.
    """
    offered = [agent.name for agent in installed_agents()]
    if here not in offered:
        offered.insert(0, here)

    def complete_agent(word: str) -> tuple[str, list[str]]:
        fits = [name for name in offered if name.startswith(word.lower())]
        if not fits:
            return word, []
        return os.path.commonprefix(fits), fits

    listed = " \u00b7 ".join(offered)
    print(f"\n{DIM}{listed}{RESET}")
    want = here
    while True:
        outcome, typed = prompt_line("agent>   ", want,
                                     complete=complete_agent)
        if outcome != "save":
            return ""
        want = typed.strip() or here
        name = known_name(want)
        if name:
            return name
        print(f"{DIM}no such agent: {want}{RESET}\n")


def said_line(label: str, value: str) -> None:
    """A decision already made, printed where it would have been asked.

    `sticky-chat start --agent codex` has answered the second question, and
    the block reads as the same three lines whether an answer was typed here
    or on the command line - which is what makes it obvious that the one you
    did not pass is the one being asked about.
    """
    print(f"{DIM}{label}{value}{RESET}")


def ask_project(tm: Tmux, source: str | None, given: str | None,
                ask_where: bool = True, agent: str = "",
                fence: str = "") -> tuple[str, str, str]:
    """Ask what a new tab is for, what runs in it, and what fences it.

    Each of the three can arrive already answered, and then it is printed
    rather than asked: `given` with `ask_where=False` is the directory off
    the command line, `agent` is `--agent`, `fence` is `--sandbox`. That is
    what lets `sticky-chat start ~/thing` ask the two questions it used to
    answer for you without asking again about the path you just typed.

    tmux's own command-prompt cannot complete a filename, so a path with one
    letter wrong came back as a failed run-shell somewhere off screen, and no
    tab. A prompt of our own can: it has a real terminal, so Tab works, and a
    path that does not exist is said so on the spot, with what you typed
    still on the line to correct.

    The second line is which agent, because a tab is two decisions and only
    one of them was ever asked - `ask_agent` is the line itself, and `shell`
    being in its list is what retired the separate key for a shell tab.

    The third line is how much of your disk that agent can reach, and it is
    here rather than in a flag because it is a per-tab decision: a container
    for the repository you are letting an agent loose in, nothing for the
    one you are reading through together. It opens on the answer you gave
    last time, or - until you have given one - on the fence in the tab you
    pressed the key in, and what you pick is written down as the next tab's
    default.

    An empty project means it was cancelled.
    """
    start = given or ""
    if not start and source:
        try:
            start = tm.fmt(source, "#{pane_current_path}")
        except RuntimeError:
            start = ""
    print(f"{BOLD}New tab{RESET}  {DIM}Tab completes \u00b7 Enter opens "
          f"\u00b7 Esc cancels{RESET}\n")
    # Started here and asked about below: finding out whether this machine
    # can run a container is a round trip to a daemon, and a prompt that
    # stops for it loses what you typed into the stop. By the time the third
    # question is reached the answer is in. Only when there is a third
    # question - an answer that came with `--sandbox` needs nothing warmed.
    if not fence:
        warm_up()
    text = start or os.getcwd()
    path = os.path.abspath(os.path.expanduser(text))
    # A directory that was named and is not there is asked about after all,
    # because a line with the path still on it is the cheapest way to fix a
    # typo - better than dying and making somebody retype the command.
    if not ask_where and os.path.isdir(path):
        said_line("project> ", path)
    else:
        while True:
            outcome, typed = prompt_line("project> ", text,
                                         complete=complete_path)
            if outcome != "save":
                return "", "", ""
            text = typed.strip()
            if not text:
                return "", "", ""
            path = os.path.abspath(os.path.expanduser(text))
            if os.path.isdir(path):
                break
            print(f"{DIM}no such directory: {path}{RESET}\n")

    here = agent_of(tm, source).name if source else DEFAULT_AGENT
    if agent:
        name = agent
        said_line("\nagent>   ", name)
    else:
        name = ask_agent(here)
        if not name:
            return "", "", ""

    # What the agent may reach. The answer you gave last time wins, because
    # it is an answer rather than an inference - and unlike the agent line,
    # which follows the tab it was pressed in, this one is a standing
    # preference about your own machine. Until there is one, the fence on
    # the tab this was opened from, and failing that the strongest fence
    # this machine can run.
    if fence:
        said_line("sandbox> ", fence)
        return path, name, fence
    was = fenced_by(launched_as(tm, claude_pane(tm, source) or source,
                                agent_named(name))) if source else ""
    fence = remembered() or was or suggested_fence()
    fences = {choice.name: unavailable(choice) for choice in FENCES}
    # Only the ones that can be had, each saying what the agent could reach
    # from inside it - the question being asked. A fence this machine cannot
    # run is left out rather than listed with its excuse: a row you cannot
    # choose is a row to read past, and on a Mac that made the list longer
    # than the choice. Typing its name still answers why, which is where
    # that belongs. Aligned rather than run together, because options read at
    # the moment of answering are a thing to compare, not prose, and wrapped
    # to the pane because the prompt is a floating pane two thirds of a
    # window wide rather than a terminal.
    print()
    room = max(32, terminal_width() - 2)
    for choice in FENCES:
        if fences[choice.name]:
            continue
        rows = wrap(choice.blurb, room - 11, 3) or [choice.blurb]
        print(f"{DIM}  {choice.name:<8} {rows[0]}{RESET}")
        for more in rows[1:]:
            print(f"{DIM}  {'':<8} {more}{RESET}")
    # The same lines whatever this machine can run, because they are about
    # what a fence is rather than about the machine - and the one people are
    # caught by is the middle one: "nothing else of yours" includes the share
    # you had mounted.
    for note in FENCE_NOTES:
        for n, line in enumerate(wrap(note, room - 4, 3)):
            print(f"{DIM}  {' ' if n else chr(0x2022)} {line}{RESET}")

    def complete_fence(word: str) -> tuple[str, list[str]]:
        # Completes what is listed. A fence that cannot be had is still
        # accepted if you type it out, and answered with the reason.
        fits = [f.name for f in FENCES if f.name.startswith(word.lower())
                and not fences[f.name]]
        if not fits:
            return word, []
        return os.path.commonprefix(fits), fits

    offered = fence
    while True:
        outcome, typed = prompt_line("sandbox> ", fence,
                                     complete=complete_fence)
        if outcome != "save":
            return "", "", ""
        # An empty line is the line as it was offered, the way the agent
        # question above reads one - and not an inference of its own, because
        # the answer that costs minutes is not one to arrive at by default.
        fence = typed.strip() or offered
        chosen = fence_named(fence)
        if not chosen:
            print(f"{DIM}no such sandbox: {fence}{RESET}\n")
            continue
        why = fences[chosen.name]
        if why:
            # Said here, where there is a line to correct it on. Said later
            # it is a tab that did not open and a message in a pane that is
            # already closing.
            print(f"{DIM}{chosen.name}: {why}{RESET}\n")
            continue
        remember(chosen.name)
        return path, name, chosen.name


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
    if getattr(args, "sandbox", None):
        command += ["--sandbox", args.sandbox]
    if getattr(args, "sandbox_image", None):
        command += ["--sandbox-image", args.sandbox_image]
    for word in getattr(args, "sandbox_arg", None) or []:
        command += ["--sandbox-arg", word]
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


def fence_tab(command: str, project: str, agent: Agent, args,
              inherited: bool = False) -> str:
    """The command a tab really runs: `command`, with a fence around it.

    Which fence is three answers in order of how explicit they are. `--sandbox`
    said it, and that is the end of it - including `--sandbox off`, which is
    how you get a tab on the host beside a tab in a container. A tab opened
    beside another one keeps what that one had, because "another tab like
    this" means like this one; so does a command line you named yourself,
    which is the one case where we have no idea what the words mean. With
    none of those, the answer given last time - and on a machine that has
    never been asked, a container if this machine can run one.

    A fence that was asked for and cannot be had stops the tab. A fence
    nobody asked for and cannot be had steps aside and says so: the default
    is a preference, and a preference should not be able to make
    `sticky-chat start` fail on a laptop where docker happens to be down.

    "Cannot be had" includes a fence that would open on a command that is
    not in it. The namespace fence replaces your home, and an agent
    installed under your home is then not there: the pane's command would
    fail, and a pane whose command fails is a window and a sidebar gone
    before you have read why. Asked here instead, where there is still a
    tab to keep.
    """
    asked = (getattr(args, "sandbox", "") or "").strip()
    if asked:
        fence = fence_named(asked)
        if not fence:
            die(f"no such sandbox: {asked} (one of {fence_choices()})")
    elif inherited or getattr(args, "agent_cmd", None):
        return command
    else:
        fence = fence_named(default_fence())
    if not fence or not fence.needs:
        return refence(command, fence, project, agent) if fence else command
    why = unavailable(fence)
    extra = list(getattr(args, "sandbox_arg", None) or [])
    if not why:
        why = unreachable(fence, command, project, extra)
    if why and not asked:
        print(f"sticky: no {fence.name} sandbox for this tab: {why}",
              file=sys.stderr)
        return refence(command, fence_named("off"), project, agent)
    if why:
        die(f"--sandbox {fence.name}: {why}")
    if not fence.tested:
        # The same notice an unproven agent profile gets, for the same
        # reason and in the same place: the tab that is about to run it is
        # where somebody is looking.
        print(f"sticky: the {fence.name} sandbox is not proven yet - "
              f"{fence.caveat}.", file=sys.stderr)
    image = (getattr(args, "sandbox_image", "") or DEFAULT_IMAGE)
    # Nobody asked for this fence on this command line - it is the answer on
    # record - and the first tab of an engine has an image to build before it
    # can open: a base image pulled and a few minutes of building, which is
    # the last thing `sticky-chat start` should do without a word. So it is
    # asked, here, where saying no costs nothing; Enter means no, because the
    # expensive half should be the half somebody typed. An explicit
    # `--sandbox` is not asked at all - naming a container is asking for the
    # image it runs - and neither is a tab whose image is already built.
    unbuilt = (fence.uses_image and not asked
               and not image_built(image, fence.cli))
    if unbuilt and ask(f"sticky: {fence.name} has no {image} yet. Build it "
                       f"now? (a few minutes, and a base image to "
                       f"download)", "n/y") != "y":
        print(f"sticky: no {image} for {fence.name} yet, so this tab is not "
              f"fenced - `sticky-chat sandbox --build {fence.name}` builds "
              f"one", file=sys.stderr)
        return refence(command, fence_named("off"), project, agent)
    if fence.uses_image:
        # Only a fence that runs one. There is nothing to build for a
        # namespace, and a first tab that went away for three minutes to
        # build an image it would never run would be hard to forgive.
        problem = ensure_image(image, fence.cli)
        if problem:
            die(problem)
    return refence(command, fence, project, agent, image, extra)


# How long to wait for a container to go before respawning the pane over it,
# and how long the polite signal gets before the one tmux itself would send.
DRAIN_WAIT = 6.0
DRAIN_FIRST = 2.0


def parent_of(pid: int) -> int:
    """The process that started this one. Zero if it cannot be asked.

    Through `ps`, because macOS has no `/proc` - and this is asked a handful
    of times on one path, not in a loop.
    """
    try:
        done = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 0
    said = done.stdout.strip()
    return int(said) if said.isdigit() else 0


def inside_pane(tm: Tmux, pane: str) -> bool:
    """Whether we are running in the pane we are about to restart.

    The one thing a tab cannot do is move itself: restarting the pane kills
    everything in it, this process included, halfway through - which left a
    tab running in a container that sticky believed was on the host, unable
    to be told apart from one that had never moved. Asked of the process
    tree rather than of `TMUX_PANE`, which a key binding sets to the pane it
    was pressed in and would refuse the one caller that is safe.
    """
    try:
        target = int(tm.fmt(pane, "#{pane_pid}") or 0)
    except (RuntimeError, ValueError):
        return False
    if target <= 1:
        return False
    pid = os.getpid()
    for _ in range(64):                 # a tree, not a cycle; but be sure
        if pid == target:
            return True
        pid = parent_of(pid)
        if pid <= 1:
            return False
    return False


def hand_over(tm: Tmux, pane: str, args, want) -> int:
    """Have tmux do the move, because we are standing in the pane.

    The same thing `C-g B` does: the server runs it, so the process keeping
    the books is not the one being killed. Everything that was said on the
    command line is said again, including which client to talk to - found
    here if it was not given, so that the message still lands on a screen.
    """
    client = args.client or ""
    if not client:
        try:
            client = (tm.run("list-clients", "-F", "#{client_name}")
                      .splitlines() or [""])[0].strip()
        except RuntimeError:
            client = ""
    again = [self_path(), "refence", "--sandbox", want.name,
             "--socket", tm.socket, "--pane", pane]
    if client:
        again += ["--client", client]
    if getattr(args, "anyway", False):
        again.append("--anyway")
    if getattr(args, "sandbox_image", None):
        again += ["--sandbox-image", args.sandbox_image]
    for word in getattr(args, "sandbox_arg", None) or []:
        again += ["--sandbox-arg", word]
    if getattr(args, "project", None):
        again += ["--project", args.project]
    tm.ok("run-shell", "-b", " ".join(shell_quote(word) for word in again))
    return 0


def drain(tm: Tmux, pane: str) -> bool:
    """Stop what a pane is running, politely, and wait for it to be gone.

    For one thing only: the pane of a boxed tab runs a container client, and a
    client asked to stop forwards that to the container while a client killed
    outright leaves it running. So the pane is told to hold its place first -
    without `remain-on-exit` the pane would close the moment its command
    exits, taking the tab with it before there is anything to put back.

    Nothing in here names an engine, and nothing needs to: it signals the
    pane's own process, and docker's client and podman's both pass a signal on
    to what they are attached to. That was checked rather than assumed, by
    unboxing a podman tab and finding the container gone with it.

    True if it went. False if it is still there after `DRAIN_WAIT`, and the
    caller respawns over it anyway: a stuck container is worth one `rm` of
    yours, not a tab that refuses to move.

    The namespace fence needs none of this and is not harmed by it. Its pane
    process *is* the sandbox, and `--die-with-parent` means the pid namespace
    goes when bwrap does - including under the SIGKILL `respawn-pane -k`
    would have sent anyway, which is the one signal a docker client does not
    survive usefully. So the conclusion for bwrap is that a signal here is
    enough and nothing is ever left running. What it is not is graceful: the
    signal goes to the pane's pid, which is bwrap, and the agent is a child
    in a pid namespace of its own, so what reaches the agent is the kernel's
    SIGKILL a step later rather than our SIGTERM. The conversation is copied
    before any of this starts, so what that can cost is the turn in flight -
    which a respawn interrupts either way.
    """
    tm.ok("set-option", "-p", "-t", pane, "remain-on-exit", "on")
    try:
        pid = int(tm.fmt(pane, "#{pane_pid}") or 0)
    except (RuntimeError, ValueError):
        return False
    if pid <= 1:
        return False
    # Two signals, because which one works depends on what is in the pane.
    # An agent takes SIGTERM and shuts down; an interactive shell ignores it
    # outright, and a shell that ignores it is a container that stays up. Then
    # SIGHUP, which is what tmux sends when a pane is closed, and which every
    # shell answers - the reason closing a boxed tab has always reaped its
    # container while stopping one politely did not.
    started = time.time()
    for when, what in ((0.0, signal.SIGTERM), (DRAIN_FIRST, signal.SIGHUP)):
        while time.time() - started < when:
            if gone(tm, pane):
                return True
            time.sleep(0.15)
        try:
            os.kill(pid, what)
        except OSError:
            return True                 # already gone
    while time.time() - started < DRAIN_WAIT:
        if gone(tm, pane):
            return True
        time.sleep(0.15)
    return False


def gone(tm: Tmux, pane: str) -> bool:
    """Whether the pane's command has ended. True if the pane itself has."""
    try:
        return tm.fmt(pane, "#{pane_dead}").strip() == "1"
    except RuntimeError:
        return True


def cmd_refence(args) -> int:
    """Put a running tab behind a different fence, without closing it.

    `C-g B` boxes the tab you are in - the agent comes back inside a
    container - and `C-g b` brings it back out. Everything around it stays:
    the same window in the same place, the same sidebar pane, the same notes
    in the same store, the same conversation. tmux respawns the pane rather
    than opening a new tab, which is what keeps the pane id and with it
    every option and every pairing hung off it.

    What does not survive is the agent's process, because there is no moving
    a running one into a container: it restarts, resumes, and replays the
    conversation onto a fresh screen. So a turn in flight is interrupted, and
    the notes re-place themselves against the replay exactly as they do after
    `resume`. The conversation itself is carried over first - see
    `carry_history`, and the tab refuses to move rather than arrive without
    it.

    Nothing here builds an image. A key you pressed should not go quiet for
    three minutes: an image that is not there yet is said so, with the one
    command that makes it.
    """
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane) or args.pane
    if not pane or not tm.pane_exists(pane):
        tell(tm, args.client, "no tab here to move")
        return 1
    want = fence_named(getattr(args, "sandbox", "") or "")
    if not want:
        die(f"say which fence to move to: {fence_choices()}")
    agent = agent_of(tm, pane)
    project = resolve_project(tm, pane, args.project)
    command = launched_as(tm, pane, agent)
    if fenced_by(command) == want.name:
        tell(tm, args.client, "this tab is already running on your machine"
             if want is OFF else "this tab is already in a namespace"
             if want is BWRAP else f"this tab is already in a {want.name} box")
        return 0

    image = getattr(args, "sandbox_image", "") or DEFAULT_IMAGE
    extra = list(getattr(args, "sandbox_arg", None) or [])
    if want.needs:
        why = unavailable(want)
        if why:
            tell(tm, args.client, f"no box for this tab: {why}")
            return 1
        if want.uses_image:
            # Of this engine, which is the question docker's answer cannot
            # stand in for: the two keep separate stores, so a machine that
            # has built the image for docker has not built it for podman.
            if not image_built(image, want.cli):
                tell(tm, args.client,
                     f"{image} is not built for {want.cli} yet: run "
                     f"sticky-chat sandbox --build {want.name}")
                return 1
            # The image installs one agent. Asked before anything is killed,
            # because a pane respawned on a command that is not there dies,
            # and a dead pane takes its window and the sidebar with it.
            program = (shlex.split(inner_command(command)) or [""])[0]
            if program and not runs_in_image(image, program, extra, want.cli):
                # The whole word, path and all: a tab running
                # `/Users/you/bin/claude` is not asking for the `claude` the
                # image has, and saying "no claude in it" of an image that
                # has one sends you looking in the wrong place.
                tell(tm, args.client,
                     f"{image} has no {program} in it: add it to "
                     f"{dockerfile_path()} and run sticky-chat sandbox "
                     f"--build {want.name}")
                return 1
            # A fence that runs the host's own programs answers the same
            # question off the path - no image to check, and no container to
            # start to find out. It is asked all the same, and for the same
            # reason: this is the last moment at which the tab is still
            # whole.
            why = unreachable(want, command, project, extra)
            if why:
                tell(tm, args.client, why)
                return 1

    session = tm.option(pane, "@sticky_session")
    # Which conversation to carry, from the three places that may know: what
    # the agent told us it called this one, what its command line asks for,
    # and the id we chose when the tab was made. The command line counts
    # because a tab that has been boxed before says `--resume <id>` on it,
    # and that is a better answer than a `tab-` key of ours.
    known = (tm.option(pane, "@sticky_agent_session")
             or session_named(command, agent))
    carried, problem = carry_history(agent, project, known or session,
                                     home_of(command),
                                     "" if want is OFF
                                     else agent_home(agent.name))
    if not carried and not getattr(args, "anyway", False):
        tell(tm, args.client, f"{problem}; --anyway moves it anyway and "
                              f"starts a fresh conversation")
        return 1

    fresh = refence(resume_command({"command": command, "agent": agent.name,
                                    "session": session,
                                    "agent_session": known,
                                    "continued": session.startswith("tab-")},
                                   agent),
                    want, project, agent, image, extra)
    make_home(fresh)
    rows = tm.option(pane, "@sticky_virtual_rows")
    virtual = int(rows) if rows.strip().isdigit() else 0
    launched = time.time()              # before the pane, as in `open_tab`
    if inside_pane(tm, pane):
        # From in here the next few lines would be cut short by their own
        # work: tmux does it instead.
        return hand_over(tm, pane, args, want)

    stuck = False
    if home_of(command):
        # Take the container with it. `respawn-pane -k` kills the pane's
        # process outright, and a container client killed outright does not
        # pass the signal on: the container keeps running with nothing
        # attached to it, and unboxing a tab twice would leave two behind.
        # Asked to stop instead, the client forwards it and `--rm` clears up -
        # podman's client as well as docker's, which is why `drain` has
        # nothing engine-shaped in it.
        stuck = not drain(tm, pane)
    try:
        tm.run("respawn-pane", "-k", "-c", project, "-t", pane,
               prime_rows(fresh, virtual) if virtual else fresh)
    except RuntimeError as exc:
        # The old process is already gone by now, so the tab is stopped
        # rather than moved. Say which, and put the pane's own behaviour
        # back: pressing the key again retries from the line still recorded
        # on it.
        tm.ok("set-option", "-p", "-u", "-t", pane, "remain-on-exit")
        tell(tm, args.client, f"the tab stopped and would not restart: {exc}")
        return 1
    tm.ok("set-option", "-p", "-u", "-t", pane, "remain-on-exit")
    tm.set_option(pane, "@sticky_agent_cmd", fresh)
    # The sidebar reads this every time it goes looking for what the agent
    # wrote down, rather than once at the start, so a tab that moves is read
    # in its new home from the next pass on.
    tm.set_option(pane, "@sticky_home", home_of(fresh))
    mark_launch(tm, pane, agent, launched, known)
    if session:
        # The id goes into the record as well where the command line was the
        # one that knew it, so that a later `resume` asks for the same
        # conversation rather than the last one in the directory. Only when
        # there is one: an empty field would wipe what the record holds.
        #
        # A pane with no session of ours - a plain shell window in the
        # session - is boxed just the same and remembered by nobody, which is
        # what it was before.
        record_window(session, command=fresh,
                      **({"agent_session": known} if known else {}))
    # A container that would not stop is left running, and saying so is the
    # whole of the handling: the tab has moved, and what is left is one `rm`
    # that only you can decide to make. Named for the engine it was in rather
    # than the one it is going to, because the thing still running is the old
    # one and `docker ps` finds nothing a podman container left behind. Only
    # of a container at all: a namespace that would not stop politely is one
    # tmux's own kill takes, because `--die-with-parent` does not need our
    # cooperation.
    was = fence_named(fenced_by(command))
    engine = was.cli if was and was.cli else ""
    left = (f" - the container it was in would not stop, so it is still "
            f"running: {engine} ps") if stuck and engine else ""
    if want is OFF:
        said = f"{agent.short} is back on your machine"
    elif want is BWRAP:
        said = (f"{agent.short} is in a namespace: this project and your "
                f"toolchain, and none of the rest of your home")
    else:
        said = (f"{agent.short} is in a {want.name} box: this project and "
                f"nothing else")
    if not getattr(args, "quiet", False) or stuck:
        tell(tm, args.client, said + left)
    return 0


# How long each signal gets before the next one up. Short, because the
# escalation is the whole point: a thing that ignored SIGINT is not going to
# answer it five seconds later.
KILL_GRACE = 2.0


def running_under(top: int, tty: str = "") -> list[tuple[int, str]]:
    """Everything the pane is running, deepest first, as (pid, command).

    Descended from `top` by parentage rather than by process group, which is
    the entire reason this exists: the runaway that prompted it was a `node`
    three levels down in a group of its own, so the terminal's Ctrl-C went to
    the script and never to the thing actually running. A walk down the
    parent links finds it; a signal to the foreground group does not.

    Deepest first, so a parent cannot be told its child has gone and start
    another one. `top` itself is never in the list: it is the pane's own
    process, and killing that is killing the tab.

    Then anything else holding the pane's terminal, which catches the case
    parentage cannot: a job whose parent has exited is re-parented to pid 1
    and leaves the tree, but it keeps the tty it was started on. What
    escapes both is a child that detached from the terminal as well - a
    `setsid`, a daemon - and nothing about it says which tab it came from.
    That one is a `pkill` of yours; `--list` says what can be seen.
    """
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,command="],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    kids: dict[int, list[tuple[int, str]]] = {}
    here: list[tuple[int, str]] = []
    want = (tty or "").replace("/dev/", "")
    for line in done.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid, where = int(parts[0]), int(parts[1]), parts[2]
        command = parts[3] if len(parts) > 3 else ""
        kids.setdefault(ppid, []).append((pid, command))
        if want and where == want and pid != top:
            here.append((pid, command))
    found: list[tuple[int, str]] = []
    rank: list[int] = [top]
    while rank:
        for kid, command in kids.get(rank.pop(), []):
            found.append((kid, command))
            rank.append(kid)
    found.reverse()
    seen = {pid for pid, _ in found}
    return found + [(pid, command) for pid, command in here
                    if pid not in seen]


def ours(pid: int) -> set[int]:
    """This process and everything it hangs off, by pid.

    `sticky-chat kill` run from a key binding is a child of the tmux server
    and never of the pane. Run from the pane itself - your own shell, or the
    agent's - it is a descendant of the very tree it is about to signal, and
    without this it kills itself halfway through and reports nothing.
    """
    mine = set()
    for _ in range(64):
        if pid <= 1 or pid in mine:
            break
        mine.add(pid)
        pid = parent_of(pid)
    return mine


def cmd_kill(args) -> int:
    """Stop what the tab is running, without stopping the tab.

    `C-g k`. Everything descended from the pane's own process goes - a test
    run, a build, a dev server, whatever the agent started and cannot get
    back out of - and the pane's own process stays, so an agent keeps its
    conversation and a shell keeps its history. For the tab itself there is
    tmux's own `prefix &`, and for the agent there is its own way out.

    Three signals in turn, because that is what the thing it was written for
    needed. SIGINT is the Ctrl-C that actually arrives: a tab that ignores
    the keystroke does so because the terminal sends it to one process group
    and the runaway is in another, and a signal addressed by pid has no such
    trouble. Then SIGTERM for what traps INT, then SIGKILL for what traps
    both. What survives all three is reported rather than retried.
    """
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane) or args.pane
    if not pane or not tm.pane_exists(pane):
        tell(tm, args.client, "no tab here to stop anything in")
        return 1
    try:
        top = int(tm.fmt(pane, "#{pane_pid}") or 0)
    except (RuntimeError, ValueError):
        top = 0
    if top <= 1:
        tell(tm, args.client, "cannot tell what that tab is running")
        return 1
    skip = ours(os.getpid())
    tree = [(pid, command)
            for pid, command in running_under(top, tm.fmt(pane, "#{pane_tty}"))
            if pid not in skip]
    if not tree:
        held = tm.fmt(pane, "#{pane_current_command}") or "nothing"
        tell(tm, args.client, f"nothing to stop: the tab is running {held} "
                              f"and nothing under it")
        return 0

    def named(pid: int, command: str) -> str:
        """A process the way somebody looking for it would say it."""
        words = shlex.split(command) if command else []
        for word in words:
            if word.startswith("-"):
                continue
            return os.path.basename(word)
        return str(pid)

    names = [named(pid, command) for pid, command in tree]
    if getattr(args, "list", False):
        for pid, command in tree:
            print(f"{pid:>7}  {command[:terminal_width() - 10]}")
        return 0
    if not getattr(args, "yes", False) and sys.stdin.isatty():
        print(f"{BOLD}stop {len(tree)}{RESET}  "
              f"{DIM}{', '.join(names[:6])}{RESET}")
        if not ask("stop them? [y/N] ", "n").lower().startswith("y"):
            return 0

    left = [pid for pid, _ in tree]
    for signal_now in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        for pid in left:
            try:
                os.kill(pid, signal_now)
            except OSError:
                pass                    # already gone, or not ours to signal
        until = time.time() + KILL_GRACE
        while time.time() < until:
            left = [pid for pid in left if alive(pid)]
            if not left:
                break
            time.sleep(0.1)
        if not left:
            break

    went = len(tree) - len(left)
    said = f"stopped {went}: {', '.join(names[:6])}"
    if len(names) > 6:
        said += f" and {len(names) - 6} more"
    if left:
        said += (f" - {len(left)} would not go, not even to KILL: "
                 f"{' '.join(str(pid) for pid in left)}")
    if not getattr(args, "quiet", False):
        tell(tm, args.client, said)
    return 0


def alive(pid: int) -> bool:
    """Whether a pid is still there. Signal zero asks without asking."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                     # there, and not ours to signal
    return True


def cmd_auto(args) -> int:
    """Ask a tab to say `continue` when its agent stops of its own accord.

    The clock in the sidebar waits out a limit nobody chose. This is the
    other half of the same wish, and it has to be asked for: an agent that
    stops because it believes it has finished is usually right, and a tab
    that argued with that every time would be unusable.

    `C-g a` is five more and stacks, so twice is ten - a count rather than a
    switch because the honest version of "keep going" is "keep going for a
    while and then let me look". `C-g A` is the switch, for the times when
    you know it is an hour of work and you are going to bed, and pressing it
    again is how it stops: one key that means "not this" is better than a
    third binding nobody will remember.

    The count lives on the agent's pane, so the sidebar reads it on its next
    pass without being told, a reload keeps it, and `tmux show-options` can
    answer what a tab is about to do.
    """
    tm = Tmux(args.socket)
    pane = claude_pane(tm, args.pane) or args.pane
    if not pane or not tm.pane_exists(pane):
        tell(tm, args.client, "no agent tab here to carry on")
        return 1
    now = (tm.option(pane, "@sticky_autocontinue") or "").strip()
    was = int(now) if now.lstrip("-").isdigit() else 0

    if getattr(args, "off", False):
        left = 0
    elif getattr(args, "forever", False):
        # The toggle. Arming for ever is the one state somebody wants out of
        # in a hurry, and the key that put it there is the key to hand.
        left = 0 if was == AUTO_FOREVER else AUTO_FOREVER
    elif was == AUTO_FOREVER:
        # Already without end: adding five to that is not a thing, and
        # quietly turning it into five would be a reduction nobody asked for.
        tell(tm, args.client, f"{agent_of(tm, pane).short} is already "
                              f"carrying on until you say otherwise")
        return 0
    else:
        left = max(0, was) + max(1, int(getattr(args, "add", AUTO_STEP)
                                        or AUTO_STEP))

    if left:
        tm.ok("set-option", "-p", "-t", pane, "@sticky_autocontinue",
              str(left))
    else:
        tm.ok("set-option", "-p", "-u", "-t", pane, "@sticky_autocontinue")
    # Said out loud, because this is the tab agreeing to act while nobody is
    # watching it. The sidebar carries the same thing standing, on its own
    # row and in the footer.
    short = agent_of(tm, pane).short
    if left == AUTO_FOREVER:
        said = f"carrying {short} on until you say otherwise"
    elif left:
        said = (f"carrying {short} on {left} more "
                f"{'time' if left == 1 else 'times'}")
    else:
        said = f"{short} stops where it stops again"
    if not getattr(args, "quiet", False):
        tell(tm, args.client, said)
    return 0


def cmd_sandbox(args) -> int:
    """Say what a fenced tab would run in, and build what it needs.

    Three things worth knowing before a tab opens in one: whether this
    machine can run a container at all, whether the image has been built,
    and where the agent's home is - because that home is where a login
    lives, and deleting it is how you log a sandboxed agent out.

    "Whether the image has been built" stopped being one question when there
    was more than one engine. docker and podman keep separate image stores, so
    the same tag is two images and a machine can perfectly well have one of
    them and not the other: every fence reports its own, and `--build` takes
    the name of the engine to fill - or, given none, fills the one a new tab
    would open in, which on a machine with a single engine is the only answer
    there was.

    `--build` is the image, and an image belongs to a container: the namespace
    fence has nothing to build, which is most of why it is here.
    """
    if getattr(args, "set", None):
        fence = fence_named(args.set)
        if not fence:
            die(f"no such sandbox: {args.set} (one of {fence_choices()})")
        remember(fence.name)
        print(f"sticky: new tabs default to {fence.name}")
        return 0
    if getattr(args, "build", None) is not None:
        fence = engine_named(args.build)
        if not fence:
            die(f"nothing to build for {args.build}: it runs no container "
                f"(one of {', '.join(f.name for f in FENCES if f.cli)})")
        why = unavailable(fence)
        if why:
            die(why)
        image = args.sandbox_image or DEFAULT_IMAGE
        problem = build_image(image, fence.cli)
        if problem:
            die(problem)
        print(f"sticky: built {image} for {fence.cli}")
        return 0

    image = args.sandbox_image or DEFAULT_IMAGE
    # "off because you said off" and "off because nobody has said" are not
    # the same answer, and what to do next is the difference: the suggestion
    # is named so that turning a fence on is a line to copy rather than a
    # thing to look up.
    suggested = "" if remembered() else suggested_fence()
    said = (f"new tabs open on {remembered()}" if remembered() else
            "nothing chosen yet, so new tabs run on the host")
    print(f"{BOLD}sandbox{RESET}  {DIM}{said}{RESET}")
    if suggested and suggested != OFF.name:
        print(f"         {DIM}{suggested} is ready here: sticky-chat "
              f"sandbox --set {suggested}{RESET}")
    print()
    for fence in FENCES:
        why = unavailable(fence)
        state = f"{DIM}{why}{RESET}" if why else "ready"
        if fence.cli and not why:
            # Asked of this engine and not of the machine: the answer is
            # about a store only this fence can see into. A fence that
            # cannot run is not asked at all, because an engine that is
            # down answers an `image ls` with a timeout rather than a no.
            state += (f", {image} built" if image_built(image, fence.cli)
                      else f", {image} not built yet")
        # "ready" of a fence nobody has run would be the wrong half of the
        # truth: this machine can start it, and nobody has watched it work.
        label = fence.label if fence.tested else f"{fence.label}, untested"
        print(f"  {fence.name:<8} {fence.blurb}")
        print(f"  {'':<8} {DIM}{label}{RESET}, {state}")
        # The half the prompt has no room for: what keeping this fence
        # working asks of you.
        for line in wrap(fence.costs, max(40, terminal_width() - 12), 3):
            print(f"  {'':<8} {DIM}{line}{RESET}")
    for note in FENCE_NOTES:
        for n, line in enumerate(wrap(note, max(40, terminal_width() - 6), 3)):
            print(f"  {DIM}{' ' if n else chr(0x2022)} {line}{RESET}")
    print(f"\n  images    built from {dockerfile_path()}")
    # Only the ones that have one. A home is made the first time a tab of
    # that agent is fenced, and an empty list is the honest answer before
    # any has been.
    for name in AGENTS:
        home = agent_home(name)
        if os.path.isdir(home):
            print(f"  {name:<9} home {home}")
    return 0


def start_asks(args, tty: bool) -> bool:
    """Whether `sticky-chat start` has a question left and somebody to ask.

    A tab is three decisions, and from the command line two of them used to
    be made for you in silence: the agent was always claude, whatever else
    was installed, and the fence was whatever the choice file said. Both are
    questions the new-tab prompt asks, so `start` asks them too - the same
    prompt, with whatever the command line already answered printed rather
    than asked.

    What is left out, and why:

    - no terminal, no question. This is the path a key binding takes through
      `run-shell`, a login item takes at boot and a script takes anywhere,
      and `prompt_line` needs a tty to go raw on. Nothing is asked, and the
      answers are the ones `start` always used.
    - `--agent` and `--sandbox` together answer both, so there is nothing to
      ask about. One of them answers one line and the other is still asked.
    - `--agent-cmd` says exactly what to run, which is a more specific answer
      than a profile name; the agent line would be asking about something
      already settled.
    - `--no-ask` for when a terminal is there and the questions are not
      wanted: the defaults, in silence, as before.
    - `--ask-here` means ask, and ask about the project as well. It is what
      the floating prompt runs.
    """
    if getattr(args, "ask_here", False):
        return True
    if not tty or getattr(args, "no_ask", False):
        return False
    if getattr(args, "agent_cmd", None):
        return not getattr(args, "sandbox", None)
    return not (getattr(args, "agent", None)
                and getattr(args, "sandbox", None))


def cmd_start(args) -> int:
    tm = Tmux(args.socket)
    binary = self_path()
    if getattr(args, "ask", False):
        return open_project_prompt(tm, binary, args)
    if start_asks(args, sys.stdin.isatty()):
        source = getattr(args, "source_pane", None)
        here = agent_of(tm, source).name if source else DEFAULT_AGENT
        # The project line only when this is the prompt. From a command line
        # the path was just typed, so it is printed where it would have been
        # asked - and `--agent-cmd` settles the agent line with whatever the
        # profile would have been, because the command is the real answer.
        project, chose, fenced = ask_project(
            tm, source, args.dir,
            ask_where=bool(getattr(args, "ask_here", False)),
            agent=(getattr(args, "agent", None)
                   or (here if getattr(args, "agent_cmd", None) else "")),
            fence=getattr(args, "sandbox", None) or "")
        if not project:
            return 0                    # cancelled: nothing was opened
        # Answered, so it is not a default any more: `fence_tab` takes an
        # explicit answer over anything the tab beside it was running.
        args.sandbox = fenced
        if chose != here:
            # Only when it is a change. Taking the answer the prompt opened
            # with would read as "--agent was given", and that is what stops
            # a tab inheriting the flags of the one it was opened beside.
            args.agent = chose
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
    asked_for = bool(getattr(args, "agent", None))
    if source and not extra and not args.agent_cmd and not asked_for:
        # Opened from a running tab: the same agent with the same arguments,
        # but not the same conversation. The id belongs to the tab still
        # running it, and the agent will not open it twice.
        #
        # Unless a profile was named. `C-g c` means "another one like this",
        # and inheriting the command line is how it keeps the flags; `C-g T`
        # means "a shell, not whatever I am in", and inheriting there gave a
        # tab that said `shell` and ran the agent it was opened beside.
        inherited = without_session(
            launched_as(tm, claude_pane(tm, source) or source, agent), agent)
    if inherited:
        command = inherited
    else:
        program = args.agent_cmd or agent.command
        if not program:
            die(f"the {agent.name} profile has no command of its own; say "
                f"what to run with --agent-cmd")
        # A command line, not a program name: `--agent-cmd` is how you put
        # something in front of the agent - a sandbox, a container, `env`,
        # `nice` - and quoting the whole string as one word made a filename
        # with spaces in it that could never be run. Split as a shell would,
        # then quoted word by word below, so a path with a space in it still
        # works when you quote it yourself.
        try:
            base = shlex.split(program) or [program]
        except ValueError:
            base = [program]            # unbalanced quotes: take it as given
        if not args.agent_cmd and not args.menus:
            # The agent answers in prose instead of opening a question menu,
            # which a sidebar full of notes has no way to answer.
            base += list(agent.no_menu_args)
        command = " ".join(shell_quote(c) for c in base + extra)

    command = fence_tab(command, project, agent, args,
                        inherited=bool(inherited))

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
    make_home(command)                  # before docker makes it, as root
    launched = time.time()              # before the pane, as in `open_tab`
    cols, rows = room_for_new_windows(tm)     # as in `open_tab`
    if not tm.server_running():
        left = tm.run("-f", config, "new-session", "-d", "-s", SESSION_NAME,
                      "-x", str(cols), "-y", str(rows),
                      "-n", name, "-c", project, *env,
                      "-P", "-F", "#{pane_id}", launch).strip()
    else:
        use_client_size(tm)
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
    # Where this tab's agent keeps what it writes down. Empty on the host,
    # where it is your own home and everybody knows; a directory of ours
    # when the agent is in a container, so the sidebar can read the
    # transcript without knowing that there is one.
    tm.set_option(left, "@sticky_home", home_of(command))
    tm.set_option(left, "@sticky_session", session or unknown)
    tm.set_option(left, "@sticky_agent", agent.name)      # before the sidebar
    # On the window, not the pane: the status line resolves a format
    # against whichever pane is active, and half of a sticky window is
    # the sidebar.
    tm.ok("set-option", "-w", "-t", left, "@sticky_mark", agent.mark)
    count_rows(tm)      # the tab is an agent tab as of now
    mark_launch(tm, left, agent, launched)             # and before it, too
    open_sidebar(tm, binary, left, project, args.sidebar_width, store_dir)
    # A bare --continue leaves the id with the agent, so the tab is remembered
    # under a key of our own and marked as what it is. Without a key nothing
    # is written at all, and the notes taken in it are lost at the next
    # restart along with any way of finding the directory they went to.
    record_window(session or unknown, project=project, command=command,
                  store=store_dir, continued=not session, agent=agent.name,
                  width=args.sidebar_width, virtual_rows=args.virtual_rows,
                  name=name, index=tab_index(tm, left))

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
                         "#{window_zoomed_flag}\t#{pane_pid}\t#{pane_width}\t"
                         "#{@sticky_log}\t#{window_id}")
    except RuntimeError:
        return 0
    started = time.monotonic()
    # Read out of the listing that was being taken anyway, so a log nobody
    # turned on costs nothing. `fit` is what a tab switch reaches first, so
    # it is where the timeline of one begins.
    logging = ""
    woken = []
    # Arriving at a tab changes that tab's geometry and nobody else's, so
    # only its sidebar has anything to look at again. Waking all nine put
    # nine processes and their two dozen tmux calls through a single-threaded
    # server at the same instant, which is most of the third of a second
    # between the click and the last sidebar drawing.
    only = getattr(args, "wake", "") or ""
    for line in listing.splitlines():
        parts = [*line.split("\t"), "", "", "", "", "", "", "", ""][:8]
        logging = logging or parts[6].strip()
        if parts[1] != "sidebar":
            continue
        if not only or parts[7].strip() == only:
            woken.append(parts[4])
        if parts[3] == "1":
            continue
        try:
            width = int(parts[2])
        except ValueError:
            continue
        if parts[5] == str(width):
            continue          # already that wide: this runs on every switch
        tm.ok("resize-pane", "-t", parts[0], "-x", str(width))
    if logging:
        log_line(logging, f"fit: began, {len(woken)} sidebar(s) to wake"
                          f"{' (this tab only)' if only else ''}")
    fit_windows(tm)
    use_client_size(tm)     # a tab opened later is opened at this size
    pin_clients(tm)
    # The hooks that call this are the ones that change how much room the
    # sidebar has. Nothing else would tell it, and it no longer looks.
    for pid in woken:
        if pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGUSR1)
            except OSError:
                pass
    if logging:
        log_line(logging,
                 f"fit: done in {1000 * (time.monotonic() - started):.0f}ms")
    return 0


def cmd_attach(args) -> int:
    tm = Tmux(args.socket)
    if not tm.server_running():
        die("no sticky session; run: sticky-chat start")
    return attach(tm, None)
