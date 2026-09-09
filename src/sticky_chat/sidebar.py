"""The pane on the right: what it draws, and its key handling."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time

from .agents import CLAUDE, Agent, discover_session
from .placement import VIEW_FORMAT, parse_view, placements
from .store import agent_of, open_store, record_window, resolve_project
from .tmux import Tmux
from .util import (
    BOLD,
    COL_COMMITTED,
    COL_FOOTER,
    COL_PENDING,
    DIM,
    RESET,
    REVERSE,
    STATUS_COLOR,
    STRIKE,
    die,
    read_key,
    self_path,
    terminal_size,
    truncate,
    visible,
    wrap,
)

                            # Claude repaints the bottom of its frame in place,
                            # and a frame taller than the screen eats the
                            # scrollback above it; 200 is more than any one
                            # frame and still a trivial grid for tmux to carry
SIDEBAR_IDLE = 30.0         # not a heartbeat: the seatbelt on a missed wake


SIDEBAR_TICK = 0.5          # the fallback where tmux cannot say "look again"


SIDEBAR_BUSY = 0.5          # while output keeps coming, work this often at most



SIDEBAR_FROZEN_TICK = 2.0   # while you are scrolled back through the history


SIDEBAR_SETTLE = 0.3        # one more pass after output or a scroll settles


# Looking for the id an agent chose for itself: how often, and for how much
# of the tab's life. Both are generous rather than tight - one glob and one
# short read, a handful of times - and the window is bounded because an agent
# that never writes a transcript is a normal outcome, not something to keep
# waiting for. Once the id is found or given up on, `hunting` is False and
# this costs one comparison a pass.
DISCOVER_EVERY = 5.0

DISCOVER_WINDOW = 60.0


# ------------------------------------------------------------------ drawing


BAND_SHARE = 30          # percent of the sidebar given to the list of notes above


CLOSE = "[x]"            # strikes the note out; brackets so it reads as a button


def close_column(width: int) -> int:
    """The button's first cell - and the first cell a click on it lands in.

    Pinned to the right edge rather than trailing the text, so every note
    offers the same target and the drawn button is the one the mouse finds.
    """
    return max(0, width - len(CLOSE))


def with_close(line: str, width: int, lit: bool = False) -> str:
    """Pad a rendered line out and hang the button off its right edge.

    Dim at rest: a column of twenty buttons should not shout. `lit` is the
    cursor sitting on the note, which is also the note the `x` key would
    strike out - so the key and the button light up as the same thing.
    """
    pad = max(1, close_column(width) - visible(line))
    return f"{line}{' ' * pad}{BOLD if lit else DIM}{CLOSE}{RESET}"


# ------------------------------------------------------------------- help


def help_sections(agent: Agent = CLAUDE) -> list[tuple[str, list[str]]]:
    """The help panel's text, with the agent named where it is named.

    Only three lines out of the page know which agent is in the pane, and
    they read wrongly rather than harmlessly when it is another one: nothing
    is sent "to Claude" from a Gemini tab, and /exit is not everyone's word
    for leaving. The rest is sticky's own keys and says nothing about it.
    """
    return [
        ("Add a note", [
            "Mouse: select output, type, Enter",
            "Keys: C-g [ scroll \u00b7 v select \u00b7 N",
        ]),
        ("Shortcuts (this pane)", [
            "s send \u00b7 S send now \u00b7 u unmark",
            "r reload \u00b7 q close \u00b7 ? this page",
            "Selecting copies \u00b7 y copies again",
        ]),
        (f"Send to {agent.short}", [
            "C-g s pastes the pending notes",
            "C-g S pastes and sends",
            "C-s while typing sends too,",
            "  alt-enter sends and enters",
            "C-g u unmarks the last batch",
        ]),
        ("Keyboard", [
            "Arrows/kj pick \u00b7 PgUp/Dn faster",
            "Enter edits \u00b7 space finds it",
            "x strikes out \u00b7 esc lets go",
            "Tab or > < change tab",
        ]),
        ("Tabs", [
            "C-g c new \u00b7 n next \u00b7 w list",
            "C-g C new, tall window",
            "C-g o reopens a closed tab",
            "C-g F forks chat and notes",
        ]),
        ("Leaving", [
            f"C-g d detach \u2014 {agent.short} runs on",
            "C-g Q closes all",
            "sticky-chat resume --last",
            agent.exit_hint,
        ]),
        ("Development", [
            "C-g r reloads config + pane",
        ]),
    ]


HELP_SECTIONS = help_sections()        # the default profile's, for callers


# ------------------------------------------------------------------- help


def footer_text(placed: list[dict], listed: int = 0,
                below: int | None = None) -> str:
    """The bottom line: what is pending, and what is off screen either way.

    How much Claude has printed while you read is *not* here - it is on the
    status line, at the bottom of the window you are looking at rather than
    at the foot of the pane beside it, and it costs no room there.
    """
    pending = sum(1 for p in placed if p["note"]["status"] == "pending"
                  and not p["note"].get("deleted"))
    if below is None:
        below = sum(1 for p in placed if p["row"] is None
                    and p.get("where") == "below")
    parts = [f"{pending} pending"]
    if listed:
        parts.append(f"\u2191{listed} above")
    if below:
        parts.append(f"\u2193{below} below")
    if any(p["note"]["status"] == "committed" for p in placed):
        parts.append("u unmark")
    parts.append("? help")
    return "  ".join(parts)


def build_help(width: int, height: int,
               agent: Agent = CLAUDE) -> list[str]:
    """The sidebar's welcome panel: shown until the first note exists."""
    body_h = max(1, height - 1)
    text_w = max(8, width - 1)         # one column of indent, one column spent
    hint = f"{BOLD} press any key to go back{RESET}"
    room = max(1, body_h - 2)

    def render(spaced: bool) -> list[str]:
        lines = [f"{BOLD} sticky-chat{RESET}",
                 f"{DIM} notes for {agent.label}{RESET}"]
        for title, paragraphs in help_sections(agent):
            if spaced:
                lines.append("")
            lines.append(f"{COL_PENDING} {truncate(title, text_w)}{RESET}")
            for para in paragraphs:
                for line in wrap(para, text_w, 6):
                    lines.append(f"{DIM} {line}{RESET}")
        return lines

    # Losing the gaps between sections beats losing a section off the bottom.
    out = render(True)
    if len(out) > room:
        out = render(False)
    if len(out) > room:
        out = out[:room]
    return out + [""] * (room - len(out)) + ["", hint][:body_h - room]


def band_window(placed: list[dict], band_h: int, scroll: int,
                top: int = 0) -> tuple[list[dict], list[dict], int, int,
                                       list[dict]]:
    """Split the notes into the band's list and the row-aligned ones.

    The band lists what is *above* what you can see: notes that scrolled off
    the top, notes above the viewport when the window is taller than the
    terminal, and notes sitting under the band itself. Scrolling the sidebar
    walks that window back through every note ever taken, so the whole
    history is reachable without touching the Claude pane.

    What is *below* comes back separately, unwindowed: there are only ever as
    many of those as the pane has scrolled past, and the foot of the sidebar
    lists them in its own band.
    """
    listed, aligned, beneath = [], [], []
    for item in placed:
        if item["row"] is None:
            (beneath if item.get("where") == "below" else listed).append(item)
        elif item["row"] < top + band_h:
            listed.append(item)
        else:
            aligned.append(item)

    history = sorted(placed, key=lambda p: p["note"].get("abs_line", 0))
    # With nothing above the view there is nothing to preview: the band only
    # earns its rows once you scroll it back through the whole history.
    anchor = len(listed) or (len(history) if scroll else 0)
    # The rule always costs a row; the "N more" line costs another, but only
    # when there is something for it to say. Reserving it either way would
    # put the oldest note permanently out of the cursor's reach.
    capacity = max(1, band_h - 1)
    if anchor > capacity or scroll:
        capacity = max(1, band_h - 2)
    # Scrolling only makes sense while there are older notes left to reveal.
    scroll = max(0, min(scroll, max(0, anchor - capacity)))
    # No floor of one here: with nothing above the view, forcing an entry
    # into the window would list a note the aligned map is already drawing on
    # its own row - the same note twice, each with its own button.
    stop = max(0, anchor - scroll)
    window = history[max(0, stop - capacity):stop]
    return window, aligned, scroll, len(listed), beneath


def note_bullet(note: dict) -> str:
    if note.get("deleted"):
        return "x"
    return "\u2713" if note["status"] == "committed" else "-"


def note_style(note: dict, match: str = "exact") -> str:
    if note.get("deleted"):
        # Dim as well as struck out. A terminal that cannot draw SGR 9
        # drops it without a word, and a note that only went grey reads
        # as committed; faint is the one attribute they all have.
        return COL_COMMITTED + DIM + STRIKE
    return STATUS_COLOR.get((note["status"], match), COL_PENDING)


def band_lines(window: list[dict], hidden: int, width: int, band_h: int,
               scroll: int, hits: list | None = None,
               top: int = 0, cursor: str | None = None) -> list[str]:
    """The dense list, newest at the bottom, over a rule.

    The only thing at the very top is how many older notes did not fit; the
    running count of what is above the screen lives in the footer, at the
    bottom of the pane.
    """
    text_w = max(4, width - 2)
    out: list[str] = []
    if hidden or scroll:
        head = f"\u2191 {hidden} more"
        if scroll:
            head += "   esc"
        out.append(f"{COL_FOOTER} {truncate(head, text_w)}{RESET}")
    room = max(4, close_column(width) - 4)      # " - ", the text, then a gap
    for item in window:
        note = item["note"]
        text = note["note"].strip() or "(no text)"
        mark = REVERSE if item["id"] == cursor else ""
        out.append(with_close(f"{note_style(note)}{mark} {note_bullet(note)} "
                              f"{truncate(text, room)}{RESET}", width,
                              item["id"] == cursor))
        if hits is not None:
            hits.append({"row": top + len(out) - 1, "id": item["id"],
                         "x": close_column(width), "onscreen": False})
    while len(out) < band_h - 1:
        out.append("")
    out = out[:band_h - 1]
    rule = "\u2500" * width
    out.append(f"{DIM}{rule}{RESET}")
    return out


def foot_lines(beneath: list[dict], width: int, foot_h: int,
               hits: list | None = None, base: int = 0,
               cursor: str | None = None) -> list[str]:
    """The mirror of the band, for the notes whose text is below the screen.

    Same entries and the same button, under a rule of their own, ordered
    nearest first so the note just past the bottom edge is the one you reach
    first. There is no window to scroll here: what is below is only ever what
    the pane has not caught up with yet.
    """
    text_w = max(4, width - 2)
    rule = "\u2500" * width
    out = [f"{DIM}{rule}{RESET}"]
    room = max(4, close_column(width) - 4)      # " - ", the text, then a gap
    order = sorted(beneath, key=lambda p: p["note"].get("abs_line", 0))
    # The rule always costs a row, and the "N more" line costs another when
    # there is something for it to say. Work that out before drawing rather
    # than trimming afterwards: a trim takes the count off the end, which is
    # the one line that says what you are not being shown.
    capacity = max(0, foot_h - 1)
    if len(order) > capacity:
        capacity = max(0, foot_h - 2)
    for item in order[:capacity]:
        note = item["note"]
        text = note["note"].strip() or "(no text)"
        mark = REVERSE if item["id"] == cursor else ""
        out.append(with_close(f"{note_style(note)}{mark} {note_bullet(note)} "
                              f"{truncate(text, room)}{RESET}", width,
                              item["id"] == cursor))
        if hits is not None:
            hits.append({"row": base + len(out) - 1, "id": item["id"],
                         "x": close_column(width), "onscreen": False})
    hidden = max(0, len(order) - capacity)
    if hidden:
        tail = f"\u2193 {hidden} more"
        out.append(f"{COL_FOOTER} {truncate(tail, text_w)}{RESET}")
    return out[:foot_h]


def build_frame(placed: list[dict], width: int, height: int,
                scroll: int = 0, hits: list | None = None,
                top: int = 0, hint: str | None = None,
                cursor: str | None = None) -> list[str]:
    """One rendered line per sidebar row, aligned with the Claude pane.

    A note keeps the row its text is on, so the aligned part is drawn in
    pane coordinates whatever the terminal shows. `top` is the first row the
    terminal does show - the pan offset, zero unless the window is taller
    than the client - and the band and its sizing follow it, so the list of
    what is above stays at the top of the *screen*.
    """
    body_h = max(1, height - (2 if hint else 1))
    text_w = width - 2
    top = max(0, min(top, body_h - 1))
    view_h = body_h - top

    band_h = max(3, view_h * BAND_SHARE // 100) if view_h >= 10 else 0
    window, aligned, scroll, listed, beneath = band_window(
        placed, band_h, scroll, top)
    if band_h:
        # Shrink to what the list actually needs; every row saved is a row
        # the aligned map gets back. Never below what holds every entry.
        wanted = listed + 1 + (1 if scroll else 0)
        if wanted < band_h:
            band_h = max(2, wanted)
            window, aligned, scroll, listed, beneath = band_window(
                placed, band_h, scroll, top)
    if band_h and not window and not scroll:
        band_h = 0                      # nothing above: give the rows back
        window, aligned, scroll, listed, beneath = band_window(
            placed, 0, 0, top)

    # The notes below the screen get a band of their own at the foot, so they
    # are reachable without scrolling the Claude pane to find them. It takes
    # its rows from the map, never from the band above, and only as many as
    # it has entries for - two rows of map are the least worth leaving.
    # The foot band takes its rows off the bottom of the map, so a note
    # aligned to one of them has nowhere left to sit. Clamping it to the last
    # row of the map stacks notes on one line; leaving it past the end draws
    # it nowhere at all, with no way to click it and no way to reach it -
    # while the footer goes on counting it. Below the map is what the band is
    # for, so that is where such a note goes.
    #
    # Which is circular: the band is sized by how many notes are below, and
    # which notes are below depends on how many rows the band took. Settling
    # it takes two passes at most - a band that grows can only push more
    # notes into itself, and it stops growing when it has room for them.
    def band_for(below: list[dict]) -> int:
        if not below or view_h < 10:
            return 0
        spare = body_h - (top + band_h) - 2
        if spare <= 1:
            return 0
        return min(len(below) + 1, spare, max(2, view_h * BAND_SHARE // 100))

    foot_h = band_for(beneath)
    for _ in range(3):
        displaced = [item for item in aligned if item["row"] >= body_h - foot_h]
        settled = band_for(beneath + displaced)
        if settled == foot_h:
            break
        foot_h = settled
    map_h = body_h - foot_h
    displaced = [item for item in aligned if item["row"] >= map_h]
    if displaced:
        aligned = [item for item in aligned if item["row"] < map_h]
        beneath = beneath + displaced

    grid: list[str] = [""] * body_h
    claimed = [False] * body_h
    if band_h:
        hidden = max(0, listed - len(window))
        for index, line in enumerate(
                band_lines(window, hidden, width, band_h, scroll, hits,
                           top, cursor)):
            grid[top + index] = line
            claimed[top + index] = True

    onscreen = sorted(aligned, key=lambda p: p["row"])
    for index, item in enumerate(onscreen):
        note = item["note"]
        row = item["row"]
        span = min(len(note["rows"]), map_h - row)
        colour = note_style(note, item["match"])

        for offset in range(span):
            line = row + offset
            if line >= map_h:
                break
            if span == 1:
                glyph = "\u258c"
            elif offset == 0:
                glyph = "\u250c"
            elif offset == span - 1:
                glyph = "\u2514"
            else:
                glyph = "\u2502"
            grid[line] = f"{colour}{glyph}{RESET}"
            claimed[line] = True

        limit = map_h - row
        if index + 1 < len(onscreen):
            limit = min(limit, max(1, onscreen[index + 1]["row"] - row))

        lines: list[str] = []
        # Only the note's first row carries the button, and only that row has
        # to leave it the cells; the rows under it get the full width back.
        head_room = max(4, close_column(width) - 3)
        if limit > 1:
            snippet = note["quote"].split("\n")[0].strip()
            if snippet:
                lines.append(f"{DIM}{truncate(snippet, head_room)}{RESET}")
        remaining = limit - len(lines)
        bullet = note_bullet(note) + " "
        # With no quote above it the button shares the first row of text, so
        # the whole note wraps that much narrower to leave it the cells.
        room = text_w - 4 if lines else head_room - len(bullet)
        # "- " marks the note text, so it reads the same here as it does in
        # the block that gets pasted back to Claude.
        chunks = wrap(note["note"] or "(no text)", room, remaining)
        mark = REVERSE if item["id"] == cursor else ""
        for position, chunk in enumerate(chunks):
            prefix = bullet if position == 0 else "  "
            lines.append(f"{colour}{mark}{prefix}{chunk}{RESET}")

        for offset, rendered in enumerate(lines):
            line = row + offset
            if line >= map_h:
                break
            prefix = grid[line] if claimed[line] else " "
            rendered = f"{prefix} {rendered}"
            if offset == 0:                          # click here to strike
                rendered = with_close(rendered, width, item["id"] == cursor)
            grid[line] = rendered
            claimed[line] = True
            if hits is not None:
                # Every row the note is written on is a way into it, the way
                # an entry in either band is: the quote and the answer to it
                # are one note, and it reads as one block. Only the first row
                # carries the button, so only that row can strike it out - an
                # x column past the width is one the mouse never reaches.
                hits.append({"row": line, "id": item["id"], "onscreen": True,
                             "x": close_column(width) if offset == 0
                             else 10 ** 6})

    if foot_h:
        for index, line in enumerate(
                foot_lines(beneath, width, foot_h, hits, map_h, cursor)):
            grid[map_h + index] = line
            claimed[map_h + index] = True

    frame = [line if line else "" for line in grid]
    if hint:
        frame.append(f"{COL_FOOTER}{truncate(hint, width)}{RESET}")
    # What the band below actually holds, which is not what `placed` says:
    # a note whose row the band took is listed there too.
    frame.append(f"{COL_FOOTER}"
                 f"{truncate(footer_text(placed, listed, len(beneath)), width)}"
                 f"{RESET}")
    return frame


def draw(frame: list[str], width: int):
    out = ["\x1b[?2026h", "\x1b[H"]
    for line in frame:
        out.append("\x1b[K")
        out.append(line)
        out.append("\r\n")
    if out[-1] == "\r\n":
        out.pop()
    out.append("\x1b[J")
    out.append("\x1b[?2026l")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def cmd_sidebar(args) -> int:
    tm = Tmux(args.socket)
    pane = args.pane
    if not pane:
        die("need --pane")
    project = resolve_project(tm, pane, args.project)
    # Not Store(project): a forked tab has a copy of its own, and showing the
    # project's notes here while every other command used the fork's would put
    # notes on screen that nothing could send.
    store = open_store(tm, pane, project, args.store)

    # tmux hooks nudge us through this pid instead of us polling hard; the
    # sidebar's own pane carries it too, so leaving copy mode here wakes us.
    self_pane = os.environ.get("TMUX_PANE")
    session_id = tm.option(pane, "@sticky_session")
    agent = agent_of(tm, pane)         # whose output this pane is drawing
    touched = 0.0

    # Which conversation the agent chose for itself, where choosing it was
    # never ours to do. The sidebar does this because it is the only thing
    # still alive after the tab is opened, and it already knows the pane;
    # `start` cannot, because at the moment it returns the agent has not
    # written anything yet. Asked here once and then answered by the loop
    # below - the two options are the only tmux calls this adds, and only on
    # a profile that has something to learn.
    launched = 0.0
    if agent.can_discover and session_id \
            and not tm.option(pane, "@sticky_agent_session"):
        stamp = tm.option(pane, "@sticky_launched")
        try:
            launched = float(stamp)
        except ValueError:
            launched = 0.0             # a tab from before this was recorded
    # Give up on a tab that is already older than the window: a sidebar
    # restarted by `reload` hours later would otherwise adopt whichever
    # conversation happens to be the newest on the machine.
    hunting = bool(launched) and time.time() - launched < DISCOVER_WINDOW
    look_again = 0.0                   # monotonic; the first pass looks
    tm.ok("set-option", "-p", "-t", pane, "@sticky_sidebar_pid", str(os.getpid()))
    if self_pane:
        tm.ok("set-option", "-p", "-t", self_pane, "@sticky_sidebar_pid",
              str(os.getpid()))

    # tmux can only say "Claude printed" after 3.7c; where it cannot, the
    # sidebar has to keep looking, and this is the one thing that decides it.
    idle = (SIDEBAR_IDLE
            if tm.run("show-options", "-gqv", "@sticky_wake").strip() == "1"
            else SIDEBAR_TICK)

    raw_mode = sys.stdin.isatty()
    old_attrs = None
    if raw_mode:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        tty.setcbreak(fd)

    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.signal(signal.SIGUSR1, lambda *_: None)

    def hangup(*_):
        # Killed with the pane rather than closed: the finally below will not
        # run, so the pid has to be taken off the panes here or it outlives
        # the process that owns it.
        for owner in (pane, self_pane):
            if owner:
                tm.ok("set-option", "-p", "-u", "-t", owner,
                      "@sticky_sidebar_pid")
        os._exit(0)

    signal.signal(signal.SIGHUP, hangup)
    # cbreak leaves ISIG on, so C-c would raise SIGINT and end the pane. Close
    # this with q instead; C-c is far too easy to hit by accident in here.
    if raw_mode:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.set_wakeup_fd(wake_w)

    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()
    last_sig = None
    last_mtime = -1.0
    notes: list[dict] = []
    placed: list[dict] = []
    help_mode: bool | None = None      # None = automatic (help until first note)
    scroll = 0                         # rows back through the note history
    cursor: str | None = None          # the note the keyboard acts on
    shown: list[str] = []              # ids the last frame actually drew
    settle_at = 0.0                    # one more pass once output has settled
    scrolled_at: int | None = None     # history size when you scrolled back
    fresh = 0                          # what Claude has printed since then
    said_fresh = -1                    # ... and what the status line was told
    last_placed = 0.0                  # when the notes were last put on rows
    last_pass = 0.0                    # when a full pass last cost tmux calls
    replace = True                     # ... and whether that is due again

    try:
        while True:
            # Is Claude still there, and is this pane in a mode or panned:
            # two questions, one invocation, because what a tmux call costs
            # is the process it starts and not the answer it gives.
            #
            # The pan offset is the first row the terminal shows, and is
            # empty unless the window is taller than the client.
            top, mode, view = 0, "0", None
            try:
                if self_pane:
                    # Three questions, one invocation. The third is what
                    # `placements` would otherwise send a whole round trip of
                    # its own to ask, and a round trip is nearly all of what
                    # a tmux call costs.
                    alive, raw, view = tm.formats(
                        (pane, "#{pane_id}\t#{pane_in_mode}"),
                        (self_pane, "#{pane_in_mode}\t#{window_offset_y}"),
                        (pane, VIEW_FORMAT))
                    mode, _, offset = raw.partition("\t")
                    top = int(offset) if offset.isdigit() else 0
                else:
                    alive, view = tm.formats((pane, "#{pane_id}\t#{pane_in_mode}"),
                                             (pane, VIEW_FORMAT))
            except RuntimeError:
                break              # before 3.8, asking about a gone pane errors
            alive, _, reading = alive.partition("\t")
            if alive.strip() != pane:
                break

            # How much Claude has printed since you scrolled back. The view
            # holds still while you read, and tmux's scroll position does not
            # move as lines arrive, so this is the only thing that knows.
            history = parse_view(view)[0] if view else 0
            if reading.strip() == "1":
                if scrolled_at is None:
                    scrolled_at = history
            else:
                scrolled_at = None
            fresh = (max(0, history - scrolled_at)
                     if scrolled_at is not None else 0)
            if fresh != said_fresh:
                # A window option, read back by `status-right`. The status
                # line is redrawn on its own slow clock, so it is asked to
                # look now - both are one tmux call, and only when the
                # number actually changes.
                if fresh:
                    tm.ok("set-option", "-w", "-t", pane,
                          "@sticky_new", str(fresh))
                else:
                    tm.ok("set-option", "-w", "-u", "-t", pane, "@sticky_new")
                tm.ok("refresh-client", "-S")
                said_fresh = fresh

            mtime = store.mtime()
            if mtime != last_mtime:
                # A note with no text is one that is being typed right now,
                # in a prompt of its own - or one whose prompt died before it
                # got any. Neither is a note yet: drawing it puts a row that
                # says nothing under a count that says one is pending, and
                # nothing will ever send it.
                notes = [n for n in store.load() if n["note"].strip()]
                last_mtime = mtime
                scroll = 0
                replace = True         # different notes, so place them again


            # While you are selecting text in here, hold completely still: a
            # redraw under a selection makes it jump or disappear.
            if mode == "1":
                if raw_mode:
                    ready, _, _ = select.select([sys.stdin, wake_r], [], [],
                                                idle)
                    # Both ends have to be drained. Claude printing fills
                    # stdin with wake bytes, and leaving them there makes
                    # every select return at once - a spin at full speed,
                    # three tmux processes a pass, for as long as the
                    # selection is held.
                    for fd in (wake_r, sys.stdin.fileno() if
                               sys.stdin in ready else None):
                        if fd is None:
                            continue
                        try:
                            os.read(fd, 4096)
                        except (BlockingIOError, OSError):
                            pass
                else:
                    time.sleep(SIDEBAR_FROZEN_TICK)
                last_sig = None                 # redraw once the mode is left
                continue

            width, height = terminal_size()
            top = max(0, min(top, height - 2))
            # Scrolled back through the history, the pane is a note list, not a
            # map of the screen: leave the placements alone until you come back.
            # Reacting to the first line Claude prints should be immediate;
            # working this hard for every line of a long reply should not.
            # Placing is what costs - a capture of the pane and every note
            # matched against it - so while output keeps arriving it happens
            # at most every SIDEBAR_BUSY, and the settle pass afterwards does
            # it once more against the screen that has stopped moving.
            now = time.monotonic()
            due = replace or not placed or now - last_placed >= SIDEBAR_BUSY
            if (scroll == 0 or not placed) and due:
                try:
                    placed = placements(tm, pane, notes, view)
                except RuntimeError:
                    break
                last_placed, replace = now, False

            show_help = (not notes) if help_mode is None else help_mode
            hits: list[dict] = []
            if show_help:
                frame = [""] * top + build_help(width, height - top, agent)
                frame.append(f"{COL_FOOTER}"
                             f"{truncate(footer_text(placed), width)}"
                             f"{RESET}")
            else:
                # Beginners need telling how to send; once nothing is
                # pending there is nothing to send and the row goes back.
                pending = any(p["note"]["status"] == "pending"
                              and not p["note"].get("deleted") for p in placed)
                hint = (f"s submits \u00b7 C-g s from {agent.short}"
                        if pending else None)
                # Page the band until whatever the cursor is on is on screen:
                # what you would act on and what you can see must not differ.
                order = [q["id"] for q in
                         sorted(placed,
                                key=lambda q: q["note"].get("abs_line", 0))]
                for _ in range(len(order) + 2):
                    hits.clear()
                    frame = build_frame(placed, width, height, scroll, hits,
                                        top, hint, cursor)
                    shown = [h["id"] for h in hits]
                    if cursor is None or cursor in shown or not shown:
                        break
                    if cursor not in order:
                        cursor = None
                        continue
                    older = order.index(cursor) < order.index(shown[0])
                    moved = scroll + 1 if older else max(0, scroll - 1)
                    if moved == scroll:
                        break
                    scroll = moved
            shown = [h["id"] for h in hits]
            signature = (width, height, tuple(frame))
            if signature != last_sig:
                draw(frame, width)
                last_sig = signature
                if self_pane:
                    store.save_hits(self_pane, hits)

            now = last_pass = time.monotonic()
            if session_id and now - touched > 60:
                record_window(session_id)     # still open, as of now
                touched = now
            if hunting and now >= look_again:
                # One glob and one first line, a few seconds apart. Off the
                # hot path on purpose: it runs after the frame is on screen,
                # never before one is drawn.
                found = discover_session(agent, launched, project=project)
                if found:
                    # Only the window record, and only in a field of its
                    # own. The notes stay in the `tab-` directory they were
                    # written to - moving a store would strand the notes
                    # already on screen, and this id is for reopening the
                    # agent's conversation and nothing else.
                    record_window(session_id, agent_session=found)
                    tm.ok("set-option", "-p", "-t", pane,
                          "@sticky_agent_session", found)
                hunting = (not found
                           and time.time() - launched < DISCOVER_WINDOW)
                look_again = now + DISCOVER_EVERY
            # Nothing is on a clock, bar the minute above. tmux says when
            # Claude has printed (`pane-activity` sends a byte here), when a
            # mode is entered or left, and when the room changed; every
            # command that writes a note signals as it writes. The long
            # timeout is not a poll but the seatbelt: a wake that never
            # arrives would otherwise leave this pane showing yesterday.
            timeout = idle
            if settle_at:
                timeout = max(0.0, min(timeout, settle_at - now))
            if hunting:
                # The one thing here that is on a clock, and only while a
                # tab is young and its id still unknown: nothing in tmux can
                # say "the agent has written its transcript now", so the next
                # look has to be waited for rather than waited on. It goes
                # back to being woken as soon as the id is found or given up.
                timeout = max(0.0, min(timeout, look_again - now))
            if not raw_mode:
                time.sleep(timeout)
                continue

            ready, _, _ = select.select([sys.stdin, wake_r], [], [], timeout)
            if settle_at and time.monotonic() >= settle_at:
                settle_at = 0.0
                last_sig = None                 # force one exact pass
                replace = True                  # against a still screen
            if wake_r in ready:
                try:
                    os.read(wake_r, 4096)
                except BlockingIOError:
                    pass
                settle_at = time.monotonic() + SIDEBAR_SETTLE
            if sys.stdin in ready:
                key = read_key(sys.stdin.fileno())
                if key == "\x00":
                    # Not a key: the `pane-activity` hook saying Claude has
                    # printed. tmux fires it once per server loop for as long
                    # as output keeps arriving, so a burst reaches us as a run
                    # of these, and one pass answers all of them - taking them
                    # one at a time would cost a pass each. Anything that is
                    # not a wake stops the draining and is read as the key it
                    # is, so nothing you typed is swallowed with them.
                    stash = None
                    while select.select([sys.stdin], [], [], 0)[0]:
                        byte = os.read(sys.stdin.fileno(), 1)
                        if byte != b"\x00":
                            stash = byte or None
                            break
                    settle_at = time.monotonic() + SIDEBAR_SETTLE
                    if stash is None:
                        # Output and nothing else. Going round again would
                        # cost a tmux round trip and a whole frame, and with
                        # placing already throttled neither could say
                        # anything the last one did not - so a long reply is
                        # answered at the rate notes are placed rather than
                        # once per line. Waiting here rather than at the foot
                        # of the loop is what keeps the tmux call out of it.
                        while True:
                            left = last_pass + SIDEBAR_BUSY - time.monotonic()
                            if left <= 0:
                                break
                            ready, _, _ = select.select(
                                [sys.stdin, wake_r], [], [], left)
                            if not ready or wake_r in ready:
                                # A note was written, or the wait is up.
                                # Either way the pass is owed now; the wake
                                # byte is drained at the foot as always.
                                break
                            byte = os.read(sys.stdin.fileno(), 1)
                            if byte != b"\x00":
                                stash = byte or None
                                break
                            settle_at = time.monotonic() + SIDEBAR_SETTLE
                    if stash is None:
                        continue
                    key = read_key(sys.stdin.fileno(), first=stash)
                if show_help:             # any key returns to the notes
                    help_mode = False
                    last_sig = None
                    continue

                def run(*command):
                    subprocess.run([self_path(), *command,
                                    "--pane", pane, "--project", project,
                                    "--socket", tm.socket],
                                   capture_output=True, text=True)

                order = [p["id"] for p in
                         sorted(placed,
                                key=lambda p: p["note"].get("abs_line", 0))]
                if key in ("up", "k", "down", "j", "pgup", "pgdn",
                           "home", "end"):
                    # The wheel pans freely; the first key after that puts the
                    # cursor back on something you can actually see.
                    if cursor not in shown:
                        cursor = shown[0] if shown else (
                            order[-1] if order else None)
                    elif order:
                        step = {"up": -1, "k": -1, "down": 1, "j": 1,
                                "pgup": -max(1, len(shown)),
                                "pgdn": max(1, len(shown))}.get(key, 0)
                        at = order.index(cursor) if cursor in order else 0
                        if key == "home":
                            at = 0
                        elif key == "end":
                            at = len(order) - 1
                        else:
                            at = max(0, min(len(order) - 1, at + step))
                        cursor = order[at]
                    last_sig = None
                    continue

                if key == "\x19":                          # wheel up: pan only
                    scroll += 1
                    last_sig = None
                if key == "\x05":                        # wheel down: pan only
                    scroll = max(0, scroll - 1)
                    last_sig = None
                if key == "q":            # not C-c: too easy to hit by mistake
                    break
                if key == "r":
                    last_mtime = -1.0
                    last_sig = None
                if key in ("?", "h"):
                    help_mode = not ((not notes) if help_mode is None
                                     else help_mode)
                    last_sig = None
                if key in ("g", "esc", "G"):             # back to the live map
                    scroll = 0
                    cursor = None
                    last_sig = None
                if key in ("\t", ">"):
                    tm.ok("next-window")
                if key == "<":
                    tm.ok("previous-window")
                if cursor and key in ("\r", "\n"):                     # edit
                    run("click", "--edit", "--id", cursor)
                    last_mtime = -1.0
                    last_sig = None
                if cursor and key == " ":            # show me where it came from
                    run("click", "--id", cursor)
                if cursor and key == "x":
                    notes = store.load()
                    for note in notes:
                        if note["id"] == cursor:
                            note["deleted"] = not note.get("deleted")
                    store.save(notes)
                    last_mtime = -1.0
                    last_sig = None
                if key == "F":                            # same key as C-g F
                    run("fork", "--quiet")
                    last_sig = None
                if key == "u":                            # same letter as C-g u
                    run("uncommit")
                    last_mtime = -1.0
                    last_sig = None
                if key == "S":            # send it and press Enter as well
                    run("commit", "--send")
                    last_mtime = -1.0
                    last_sig = None
                if key == "s":                            # same letter as C-g s
                    run("commit")
                    last_mtime = -1.0
                    last_sig = None
    except KeyboardInterrupt:
        pass
    finally:
        # Nothing must be left pointing at this process: the pid outlives it
        # in the option, the system reuses pids, and what the hooks send is
        # SIGUSR1 - fatal to anything that did not ask for it.
        for owner in (pane, self_pane):
            if owner:
                tm.ok("set-option", "-p", "-u", "-t", owner,
                      "@sticky_sidebar_pid")
        signal.set_wakeup_fd(-1)
        sys.stdout.write("\x1b[?25h\x1b[0m")
        sys.stdout.flush()
        if old_attrs is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
    return 0
