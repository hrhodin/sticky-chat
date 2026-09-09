"""Deciding which row on screen a note belongs to."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .agents import CLAUDE, Agent
from .tmux import Tmux

CONTEXT_ROWS = 2


LANDMARK_WINDOW = 40


FUZZY_THRESHOLD = 0.8


# The default profile's output heuristics, kept here under the names everything
# already imports. What varies by agent lives on the profile now, and a caller
# with another agent in the pane passes it; these are what it falls back to.
PROMPT_ROWS = CLAUDE.prompt_rows


PROMPT_BOX_ROWS = CLAUDE.prompt_box_rows


PASTE_PLACEHOLDER = CLAUDE.paste_placeholder


FUZZY_DRIFT = 6             # rows a note may move before an approximate match


                            # stops being believable: absolute line numbers do
                            # not change as output is appended, so only a
                            # repaint moves text, and never far
MIN_FUZZY_CHARS = 8


# Lines that make good landmarks: the agent's tool headers and prompts, plus
# any line that starts hard against column zero (output is normally indented).
GLYPH_LANDMARK = CLAUDE.glyph_landmark


def in_prompt_box(last_abs: int, cursor_abs: int | None,
                  agent: Agent = CLAUDE) -> bool:
    """True when a selection reaches into what you are typing right now.

    The prompt box is wherever the cursor is, so anything level with it or
    just above it is the box itself rather than the agent's output. Your older
    turns, already in the transcript, are ordinary text you may well want to
    annotate, so nothing here looks at the ">" marker.
    """
    return (cursor_abs is not None
            and last_abs >= cursor_abs - agent.prompt_rows)


# ----------------------------------------------------------------- add note


def find_landmark(rows: list[str], agent: Agent = CLAUDE) -> dict | None:
    """Nearest structural line above the quote. rows[-1] is directly above it."""
    for offset, text in enumerate(reversed(rows), start=1):
        if not text.strip():
            continue
        if agent.glyph_landmark.match(text):
            return {"text": text.strip(), "offset": offset}
    for offset, text in enumerate(reversed(rows), start=1):
        stripped = text.strip()
        if len(stripped) >= 3 and not text.startswith((" ", "\t")):
            return {"text": stripped, "offset": offset}
    return None


# ------------------------------------------------------------------ placing


def context_score(visible: list[str], row: int, note: dict) -> tuple[int, int]:
    comparable = matched = 0
    for offset, expected in enumerate(reversed(note.get("before") or []), start=1):
        idx = row - offset
        if 0 <= idx < len(visible):
            comparable += 1
            matched += visible[idx] == expected
    end = row + len(note["rows"])
    for offset, expected in enumerate(note.get("after") or []):
        idx = end + offset
        if 0 <= idx < len(visible):
            comparable += 1
            matched += visible[idx] == expected
    return matched, comparable


def note_candidates(visible: list[str], note: dict, hint: int,
                    fuzzy: bool = True) -> list[dict]:
    """Every plausible row for one note, best first.

    Exact matches beat fuzzy ones, fuzzy beats the landmark fallback, and
    within a tier the row nearest to where the note was last seen wins. The
    caller picks from this list, so a note is never dropped onto a position
    that another note already owns exactly.

    With `fuzzy` off only the exact scan runs - a list comparison per row,
    which costs nothing next to the approximate one. See `resolve` for when
    that is safe.
    """
    rows = note["rows"]
    span = len(rows)
    if not rows or span > len(visible):
        return []

    exact = []
    for i in range(len(visible) - span + 1):
        if visible[i:i + span] != rows:
            continue
        matched, comparable = context_score(visible, i, note)
        ratio = 1.0 if comparable == 0 else matched / comparable
        if ratio >= 0.75:
            exact.append({"row": i, "match": "exact", "score": 2 + ratio})
    if exact:
        exact.sort(key=lambda c: (-c["score"], abs(c["row"] - hint)))
        return exact

    if not fuzzy:
        return []

    joined = "\n".join(rows)
    if len(re.sub(r"\s", "", joined)) >= MIN_FUZZY_CHARS:
        fuzzy = []
        # Every row on screen is compared against every note on every pass,
        # and the real comparison is the expensive part of drawing at all.
        # difflib's two cheap ratios are upper bounds on the real one, so a
        # row they put below the threshold cannot reach it: rejecting on them
        # skips work without changing which rows come back, and that filter
        # is where all of the saving is. The matcher is reused only to keep
        # the quote as the first sequence - difflib caches the *second* one,
        # so nothing is saved by holding on to it - because which side is
        # which is not something difflib promises to be symmetric about.
        matcher = SequenceMatcher(None)
        matcher.set_seq1(joined)
        for i in range(len(visible) - span + 1):
            matcher.set_seq2("\n".join(visible[i:i + span]))
            if matcher.real_quick_ratio() < FUZZY_THRESHOLD:
                continue
            if matcher.quick_ratio() < FUZZY_THRESHOLD:
                continue
            ratio = matcher.ratio()
            if ratio >= FUZZY_THRESHOLD:
                fuzzy.append({"row": i, "match": "fuzzy", "score": ratio,
                              "ratio": round(ratio, 3)})
        if fuzzy:
            fuzzy.sort(key=lambda c: (-c["score"], abs(c["row"] - hint)))
            return fuzzy

    landmark = note.get("landmark")
    if landmark:
        marks = []
        for i, text in enumerate(visible):
            if text.strip() == landmark["text"]:
                row = i + landmark["offset"]
                if 0 <= row < len(visible):
                    marks.append({"row": row, "match": "fuzzy",
                                  "via": "landmark", "score": 0.5})
        marks.sort(key=lambda c: abs(c["row"] - hint))
        return marks
    return []


def place_note(visible: list[str], top_abs: int, note: dict) -> dict:
    """Placement for a single note, ignoring the other notes on screen."""
    hint = note.get("abs_line", top_abs) - top_abs
    cands = note_candidates(visible, note, hint)
    if not cands:
        return {"row": None, "match": "offscreen"}
    best = dict(cands[0])
    best.pop("score", None)
    return best


VIEW_FORMAT = "#{history_size}\t#{pane_height}\t#{scroll_position}"


def parse_view(raw: str) -> tuple[int, int, int]:
    """History size, pane height and scroll offset out of one format read.

    Every field is optional, because the answer can be short or empty: from
    tmux 3.8 a question about a pane that has gone succeeds and prints
    nothing, and a batched read pads what is missing. Reaching past the end
    of that would take the sidebar down with a traceback painted into its
    own pane.
    """
    parts = [*raw.split("\t"), "", "", ""]

    def number(text: str) -> int:
        text = text.strip()
        return int(text) if text.lstrip("-").isdigit() else 0

    return number(parts[0]), number(parts[1]), number(parts[2])


def pane_view(tm: Tmux, pane: str,
              raw: str | None = None) -> tuple[list[str], int, int]:
    """Visible rows, absolute line of the first one, pane height.

    A scroll landing between reading the offset and taking the capture would
    shift every note by the number of rows that moved, so the offset is read
    again afterwards - in the same invocation as the capture - and the whole
    thing tried again when it changed.

    `raw` is that reading already taken. The caller is often asking tmux
    something else at the same moment, and one invocation answers as many
    questions as it is given: what a tmux call costs is the process, so an
    answer already in hand is a whole round trip saved. A retry uses the
    reading the failed attempt came back with, for the same reason.
    """
    for _ in range(3):
        if raw is None:
            raw = tm.fmt(pane, VIEW_FORMAT)
        history, height, scroll = parse_view(raw)
        visible, after = tm.capture_with(pane, -scroll, height - 1 - scroll,
                                         VIEW_FORMAT)
        if after == raw:
            break
        raw = after
    return visible, history - scroll, height


def resolve(visible: list[str], top_abs: int, height: int,
            notes: list[dict],                  # height kept for the callers
            pane: str = "") -> list[dict]:
    """Place every note against the visible rows, resolving collisions.

    Exact matches are handed out first and take ownership of the rows they
    sit on, so a note that only matches those rows approximately is reported
    off-screen instead of being drawn on text that demonstrably belongs to a
    different note. An approximate match is also refused when it lands far
    from where the note was last seen exactly: once you scroll away from the
    real text, similar-looking output elsewhere is not the same text.
    """
    cands = {}
    bottom_abs = top_abs + len(visible)

    def positioned(note: dict) -> bool:
        """Whether this note's remembered row means anything here.

        A row is `history_size + offset`, which counts from the top of one
        pane's scrollback and says nothing about any other. Notes outlive
        panes - a conversation is resumed, a sidebar is reloaded, a tab is
        reopened - so a note carries the pane its position was taken in, and
        a position from a different one is no position at all. Left alone it
        would claim the note is off the bottom of a pane it never saw.
        """
        return bool(pane) and note.get("pane") == pane

    for note in notes:
        here = positioned(note)
        hint = (note.get("abs_line", top_abs) - top_abs) if here else 0
        # A note that has been seen exactly is pinned to where that was: the
        # loop below refuses any approximate candidate further than
        # FUZZY_DRIFT from its anchor. So when the whole window is further
        # away than that, every candidate the approximate pass could produce
        # would be thrown out again, and running it is work for nothing.
        # This is not a shortcut with a cost: the exact scan still runs, and
        # an exact match is still taken wherever on screen it turns up.
        anchor = note.get("anchor_abs") if here else None
        near = anchor is None or (anchor + FUZZY_DRIFT >= top_abs
                                  and anchor - FUZZY_DRIFT <= bottom_abs)
        cands[id(note)] = note_candidates(visible, note, hint, fuzzy=near)

    owned: dict[int, int] = {}          # screen row -> id() of the owning note
    chosen: dict[int, dict] = {}

    def rows_for(note, row):
        return range(row, row + max(1, len(note["rows"])))

    ranked = sorted(notes, key=lambda n: -(cands[id(n)][0]["score"]
                                           if cands[id(n)] else 0))
    for note in ranked:                                   # exact matches first
        best = next((c for c in cands[id(note)] if c["match"] == "exact"), None)
        if best is None:
            continue
        chosen[id(note)] = best
        for row in rows_for(note, best["row"]):
            owned.setdefault(row, id(note))               # this text is spoken for

    for note in ranked:                                   # then the rest
        if id(note) in chosen:
            continue
        anchor = note.get("anchor_abs") if positioned(note) else None
        for cand in cands[id(note)]:
            if any(row in owned for row in rows_for(note, cand["row"])):
                continue                                  # exactly another note's
            if (anchor is not None
                    and abs(top_abs + cand["row"] - anchor) > FUZZY_DRIFT):
                continue                                  # too far from the real one
            chosen[id(note)] = cand
            break

    out = []
    for note in notes:
        cand = chosen.get(id(note))
        if cand is None:
            below = (positioned(note)
                     and note.get("abs_line", top_abs) >= bottom_abs)
            result = {"row": None, "match": "offscreen",
                      "where": "below" if below else "above"}
        else:
            result = {k: v for k, v in cand.items() if k != "score"}
            note["abs_line"] = top_abs + cand["row"]
            if pane:
                note["pane"] = pane
            if cand["match"] == "exact":
                note["anchor_abs"] = note["abs_line"]
        out.append({**result, "id": note["id"], "note": note})
    return out


def placements(tm: Tmux, pane: str, notes: list[dict],
               raw: str | None = None) -> list[dict]:
    visible, top_abs, height = pane_view(tm, pane, raw)
    return resolve(visible, top_abs, height, notes, pane)
