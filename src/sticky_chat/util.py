"""Small things with no dependencies of their own."""

from __future__ import annotations

import codecs
import glob
import os
import re
import select
import sys
import unicodedata

RESET = "\x1b[0m"


DIM = "\x1b[2m"


BOLD = "\x1b[1m"


COL_PENDING = "\x1b[33m"


COL_COMMITTED = "\x1b[90m"


COL_FUZZY = "\x1b[38;5;208m"


COL_FOOTER = "\x1b[36m"


STRIKE = "\x1b[9m"


REVERSE = "\x1b[7m"



STATUS_COLOR = {
    ("pending", "exact"): COL_PENDING,
    ("pending", "fuzzy"): COL_FUZZY,
    ("committed", "exact"): COL_COMMITTED,
    ("committed", "fuzzy"): COL_COMMITTED,
}


# ------------------------------------------------------------------- helpers


def self_path() -> str:
    """Absolute path to this program, however it was invoked.

    It is written into the generated tmux config and into key bindings, so
    it has to be something a shell can run. That rules out how `python -m
    sticky_chat` arrives: there `sys.argv[0]` is the module file, which is
    installed without a shebang and without the execute bit, and every
    binding made from it would silently do nothing. The console script is
    the same program under a name that works, so it is preferred there.
    """
    argv0 = sys.argv[0]
    import shutil as _shutil
    if os.path.basename(argv0) == "__main__.py":
        for name in ("sticky-chat", "sticky"):
            found = _shutil.which(name)
            if found:
                return os.path.abspath(found)
        # A checkout with no install: bin/ holds the same launcher.
        here = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "bin", "sticky-chat")
        if os.access(here, os.X_OK):
            return here
    if os.sep in argv0:
        return os.path.abspath(argv0)
    found = _shutil.which(argv0)
    return os.path.abspath(found or __file__)


def die(msg: str, code: int = 1):
    print(f"sticky: {msg}", file=sys.stderr)
    sys.exit(code)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def cell_width(ch: str) -> int:
    """Columns one character takes on screen.

    The one place this is decided. Everything that measures text goes
    through it, so a correction here - ambiguous-width characters, emoji
    presentation - cannot leave the row arithmetic and the truncation
    disagreeing about where a line ends.
    """
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(text: str) -> int:
    """Approximate display width of a string."""
    return sum(cell_width(ch) for ch in text)


def visible(text: str) -> int:
    """Display width of a rendered line, blind to the colour codes in it."""
    return dwidth(ANSI.sub("", text))


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """Cut to `width` columns, counting what is drawn rather than what is sent.

    Colour is written into the text as escape sequences, which take no room
    on screen: counted as characters they make a line look far wider than it
    is and cut it long before its end. They are passed through whole, never
    cut in half, since half an escape sequence is printed rather than obeyed.
    """
    if width <= 0:
        return ""
    if visible(text) <= width:
        return text
    out = []
    used = 0
    rest = text
    while rest:
        code = ANSI.match(rest)
        if code:
            out.append(code.group())
            rest = rest[code.end():]
            continue
        cw = cell_width(rest[0])
        if used + cw > width - 1:
            break
        out.append(rest[0])
        used += cw
        rest = rest[1:]
    return "".join(out) + ellipsis


def wrap(text: str, width: int, limit: int) -> list[str]:
    """Word-wrap into at most `limit` lines, last line ellipsised if needed."""
    if width <= 0 or limit <= 0:
        return []
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        cand = word if not cur else cur + " " + word
        if dwidth(cand) <= width:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        if len(lines) >= limit:
            break
        cur = word if dwidth(word) <= width else truncate(word, width)
    if cur and len(lines) < limit:
        lines.append(cur)
    if len(lines) > limit:
        lines = lines[:limit]
    if lines and len(words) > sum(len(line.split()) for line in lines):
        lines[-1] = truncate(lines[-1] + " …", width)
    return lines


def shell_quote(word: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", word):
        return word
    return "'" + word.replace("'", "'\\''") + "'"


# ------------------------------------------------------------------ sidebar


def terminal_width(default: int = 80) -> int:
    try:
        return os.get_terminal_size(sys.stdout.fileno()).columns
    except OSError:
        return default


def terminal_size(default=(80, 24)) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        return size.columns, size.lines
    except OSError:
        return default


ARROWS = {"[A": "up", "OA": "up", "[B": "down", "OB": "down",
          "[5~": "pgup", "[6~": "pgdn", "[H": "home", "[F": "end"}


def read_key(fd: int, first: bytes | None = None) -> str:
    """One keypress: a character, or a name for the keys that send sequences.

    Arrow and page keys arrive as an escape followed by more bytes; a bare
    Escape arrives alone. Telling them apart means looking for what follows
    straight away, which is what every terminal program does here.

    `first` is for a caller that has already taken the first byte off the
    terminal and found it is not what it was looking for: the rest of the
    sequence is still there to be read.
    """
    if first is None:
        first = os.read(fd, 1)
    if not first:
        return ""
    if first != b"\x1b":
        return first.decode("utf-8", "replace")
    if not select.select([fd], [], [], 0.05)[0]:
        return "esc"
    body = b""
    while len(body) < 8:
        body += os.read(fd, 1)
        if body[-1:].isalpha() or body[-1:] == b"~":
            break
        if not select.select([fd], [], [], 0.02)[0]:
            break
    return ARROWS.get(body.decode("latin-1"), "esc")


def complete_path(text: str) -> tuple[str, list[str]]:
    """The longest certain extension of a path, and what it could still be.

    Only directories are offered: the prompt this serves asks for a project
    to open, and a file is never one. `~` survives, because the completion is
    spliced onto what was typed rather than onto what it expands to.
    """
    stub = os.path.expanduser(text)
    found = sorted(p for p in glob.glob(stub + "*") if os.path.isdir(p))
    if not found:
        return text, []
    shared = os.path.commonprefix(found)
    if not shared.startswith(stub):       # a wildcard was typed: leave it be
        return text, [os.path.basename(p) for p in found]
    if len(found) == 1:
        shared = shared.rstrip("/") + "/"
    return text + shared[len(stub):], [os.path.basename(p) for p in found]


def lay_out(text: str, width: int) -> tuple[list[str], list[tuple[int, int]]]:
    """The rows a line will be printed as, and where each character sits.

    The rows are broken here rather than left to the terminal, so that the
    cursor arithmetic below is answering the same question the screen is:
    a terminal wraps at its own last column, and whether it has wrapped yet
    when the text ends exactly there is famously not something you can ask.
    Keeping one column spare means the wrap never happens on its own.
    """
    rows: list[str] = []
    where: list[tuple[int, int]] = []
    row, used, current = 0, 0, ""
    for ch in text:
        size = cell_width(ch)
        if used + size > width:
            rows.append(current)
            row, used, current = row + 1, 0, ""
        where.append((row, used))
        current += ch
        used += size
    rows.append(current)
    where.append((row, used))            # one past the end: where you type
    return rows, where


def row_step(rows: list[str], where: list[tuple[int, int]],
             index: int, step: int, goal: int | None) -> tuple[int, int]:
    """One printed row up or down from `index`, and the column to keep.

    Where a note has wrapped, up and down mean rows on the screen rather
    than anything in the text, so the answer comes from the same `lay_out`
    the cursor arithmetic uses - the rows as they were actually drawn.

    The column is carried between presses because a short row in the middle
    would otherwise drag the cursor left and leave it there: passing the
    goal back in means a run of them tracks the column you started from,
    and lands on the end of any row too short to hold it.
    """
    row, col = where[index]
    if goal is None:
        goal = col
    want = row + step
    if not 0 <= want < len(rows):
        return index, goal
    best = None
    for i, (r, c) in enumerate(where):
        if r == want and c <= goal and (best is None or c > where[best][1]):
            best = i
    return (index if best is None else best), goal


def prompt_line(prompt: str, initial: str = "",
                complete=None, on_hangup=None) -> tuple[str, str]:
    """Read one line, with a cursor you can move. How it ended, and the text.

    Enter "save"s, Esc "cancel"s, C-c is "copy": reaching for the copy key
    means the selection was what you were after, not a note. `input()` cannot
    see either key, so this reads raw bytes; a bare Esc is told apart from an
    arrow key by whether anything follows it straight away.

    C-s "send"s and Alt-Enter "send_now"s: save this note and hand the batch
    to Claude, the second pressing Enter for you. They are `s` and `S` in the
    sidebar, from where you are already typing.

    Those two keys because those two are the ones that arrive. Raw mode makes
    C-s a byte rather than flow control, and Alt-Enter comes through as an
    escape and a return. C-S is `\x13` - the same byte as C-s, since a
    terminal has no way to say that shift was held - and Shift-Enter and
    C-Enter are both plain `\r`, which is to say Enter. None of that changes
    with `extended-keys` on; it is measured, not assumed.

    Left and right move, up and down cross the printed rows once the note
    has wrapped, Home/End and C-a/C-e jump, backspace and Delete
    take a character from either side of the cursor, and typing puts it
    where the cursor is. The line is redrawn in full after each key, which
    for a line this length is cheaper than reasoning about what changed.

    `complete` makes Tab work: it takes what has been typed and returns how
    far that can be extended with certainty, plus everything it could still
    become. Nothing is chosen for you - an ambiguous Tab lists and waits.

    `on_hangup` is given whatever has been typed when the terminal goes away
    underneath - a prompt closed from outside, a window killed - so that a
    note in progress is kept rather than lost with the pane it was typed in.
    """
    if not sys.stdin.isatty():
        sys.stdout.write(prompt + initial)
        sys.stdout.flush()
        try:
            return "save", (input() or initial)
        except (EOFError, KeyboardInterrupt):
            return "cancel", initial

    import signal
    import termios
    import tty
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = initial
    at = len(text)                       # the cursor, as an index into text
    drawn = 0                            # the row the cursor was left on
    goal: int | None = None              # the column up and down are keeping

    if on_hangup is not None:
        def hangup(*_):
            # The terminal has gone; there is nothing to redraw and nobody
            # to tell, so save what was typed and go.
            on_hangup(text)
            os._exit(0)

        for gone in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(gone, hangup)

    def draw(first: bool = False) -> None:
        nonlocal drawn
        width = max(2, terminal_width() - 1)
        rows, where = lay_out(prompt + text, width)
        out = [] if first else (
            [f"\x1b[{drawn}A"] if drawn else []) + ["\r"]
        out.append("\r\n".join(rows))
        out.append("\x1b[J")            # whatever the last line left behind
        want_row, want_col = where[len(prompt) + at]
        last_row = len(rows) - 1
        if last_row > want_row:
            out.append(f"\x1b[{last_row - want_row}A")
        out.append("\r")
        if want_col:
            out.append(f"\x1b[{want_col}C")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        drawn = want_row

    def listing(options: list[str]) -> None:
        nonlocal drawn
        rows, _ = lay_out(prompt + text, max(2, terminal_width() - 1))
        sys.stdout.write(f"\x1b[{len(rows) - 1 - drawn}B" if
                         len(rows) - 1 > drawn else "")
        listed = truncate("  ".join(options), terminal_width() - 1)
        sys.stdout.write(f"\r\n{listed}\r\n")
        drawn = 0
        draw(first=True)

    try:
        tty.setraw(fd)
        draw(first=True)
        while True:
            raw = os.read(fd, 1)
            # Only up and down carry a column; anything else typed means the
            # next one starts from wherever the cursor now is.
            carried, goal = goal, None
            if not raw:
                return "cancel", text
            if raw == b"\x1b":
                # an escape sequence sends its rest immediately; a keypress
                # stands alone
                if not select.select([fd], [], [], 0.05)[0]:
                    return "cancel", text
                body = b""
                while len(body) < 8 and select.select([fd], [], [], 0.02)[0]:
                    body += os.read(fd, 1)
                    if body[-1:].isalpha() or body[-1:] == b"~":
                        break
                if body in (b"\r", b"\n"):        # Alt-Enter
                    return "send_now", text
                move = body.decode("latin-1")
                if move in ("[D", "OD"):
                    at = max(0, at - 1)
                elif move in ("[C", "OC"):
                    at = min(len(text), at + 1)
                elif move in ("[A", "OA", "[B", "OB"):
                    rows, where = lay_out(prompt + text,
                                          max(2, terminal_width() - 1))
                    index, goal = row_step(
                        rows, where, len(prompt) + at,
                        -1 if move in ("[A", "OA") else 1, carried)
                    at = max(0, index - len(prompt))
                elif move in ("[H", "OH", "[1~"):
                    at = 0
                elif move in ("[F", "OF", "[4~"):
                    at = len(text)
                elif move == "[3~":                  # Delete
                    text = text[:at] + text[at + 1:]
                else:
                    continue                         # not a key we act on
                draw()
                continue
            if raw in (b"\r", b"\n"):
                return "save", text
            if raw == b"\x03":                       # the copy key
                return "copy", text
            if raw == b"\x13":                       # C-s: save and send
                return "send", text
            if raw == b"\x01":                       # C-a
                at = 0
                draw()
                continue
            if raw == b"\x05":                       # C-e
                at = len(text)
                draw()
                continue
            if raw in (b"\x7f", b"\x08"):
                if at:
                    text = text[:at - 1] + text[at:]
                    at -= 1
                    draw()
                continue
            if raw == b"\x04":                       # C-d deletes forwards
                if at < len(text):
                    text = text[:at] + text[at + 1:]
                    draw()
                continue
            if raw == b"\x0b":                       # C-k to the end
                text = text[:at]
                draw()
                continue
            if raw == b"\t":
                if not complete:
                    continue
                grown, options = complete(text)
                if grown != text:
                    text, at = grown, len(grown)
                    draw()
                elif len(options) > 1:
                    listing(options)
                continue
            if raw == b"\x15":                       # C-u clears the line
                text, at = "", 0
                draw()
                continue
            chunk = decoder.decode(raw)
            if not chunk or chunk < " ":
                continue
            text = text[:at] + chunk + text[at:]
            at += len(chunk)
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        rows, _ = lay_out(prompt + text, max(2, terminal_width() - 1))
        if len(rows) - 1 > drawn:        # leave the cursor past the whole line
            sys.stdout.write(f"\x1b[{len(rows) - 1 - drawn}B")
        sys.stdout.write("\r\n")
        sys.stdout.flush()
