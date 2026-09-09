"""Where a note is drawn, given what is on screen.

Placement is the part most likely to go quietly wrong: a note put on the wrong
row looks plausible, so these pin the rules rather than the appearance.
"""

from __future__ import annotations

import pytest

VISIBLE = [f"row {i:02d} payload" for i in range(20)]

# A remembered row belongs to the pane it was taken in, so the tests have to
# say which pane they are pretending to be looking at.
PANE = "%0"


def note(**over):
    base = {"id": "a", "status": "pending", "note": "n",
            "quote": "row 05 payload", "rows": ["row 05 payload"],
            "before": ["row 03 payload", "row 04 payload"],
            "after": ["row 06 payload", "row 07 payload"],
            "abs_line": 5, "span": 1, "landmark": None, "pane": PANE}
    base.update(over)
    return base


def test_exact_row(sticky):
    assert sticky.place_note(VISIBLE, 0, note())["row"] == 5


def test_near_miss_is_fuzzy(sticky):
    changed = list(VISIBLE)
    changed[5] = "row 05 payloadx"
    got = sticky.place_note(changed, 0, note())
    assert got["match"] == "fuzzy" and got["row"] == 5


def test_the_threshold_is_where_it_says_it_is(sticky):
    """The fuzzy pass rejects cheaply before comparing properly.

    `real_quick_ratio` and `quick_ratio` are upper bounds on `ratio`, so
    anything they put under the threshold cannot reach it - but only while
    the comparison either side of them agrees on where the line is. These
    two rows sit at 0.829 and 0.780 against the same quote.
    """
    quote = "row 05 payload-05 and some more text here"
    taken = note(quote=quote, rows=[quote])
    above = list(VISIBLE)
    above[5] = "row 05 payload-05 and some more tezzzzzzz"
    assert sticky.place_note(above, 0, taken)["match"] == "fuzzy"

    below = list(VISIBLE)
    below[5] = "row 05 payload-05 and some more zzzzzzzzz"
    placed = sticky.place_note(below, 0, taken)
    assert placed is None or placed["match"] != "fuzzy"


def test_a_note_pinned_far_away_skips_the_approximate_pass(sticky):
    """Placing every note against every row is what a redraw costs, and most
    notes are nowhere near the screen. A note that has been seen exactly is
    pinned to where that was - `resolve` refuses any approximate candidate
    further than FUZZY_DRIFT from it - so for those the approximate pass can
    only produce candidates that get thrown away again."""
    far = note(anchor_abs=10_000, abs_line=10_000, quote="row 05 payloadx",
               rows=["row 05 payloadx"])
    changed = list(VISIBLE)
    changed[5] = "row 05 payloadx"
    # Exactly what it says, so it is found however far the anchor is.
    assert sticky.place_note(changed, 0, far)["match"] == "exact"

    # Only nearly what it says: unreachable, and refused either way.
    nearly = list(VISIBLE)
    nearly[5] = "row 05 payloady"
    assert sticky.resolve(nearly, 0, 20, [dict(far)], PANE)[0]["row"] is None


def test_an_unanchored_note_is_still_matched_approximately(sticky):
    """The pin only exists once a note has been seen exactly. Without one
    there is nothing to say the note is out of reach, so the search runs."""
    fresh = note(anchor_abs=None, abs_line=5)
    changed = list(VISIBLE)
    changed[5] = "row 05 payloadx"
    assert sticky.place_note(changed, 0, fresh)["match"] == "fuzzy"


def test_a_row_remembered_in_another_pane_is_no_position_at_all(sticky):
    """A row is `history_size + offset`, counted from the top of one pane's
    scrollback. Notes outlive panes - conversations get resumed, sidebars
    reloaded, tabs reopened - and the number means nothing in the next one.
    Believed anyway, it puts notes below the bottom of a pane they never
    saw, where they stay for ever."""
    stale = note(pane="%99", abs_line=9000, anchor_abs=9000)
    placed = sticky.resolve(VISIBLE, 0, 20, [stale], PANE)[0]
    assert placed["row"] == 5, "it is on screen; the stale row said otherwise"

    missing = note(pane="%99", abs_line=9000, quote="gone", rows=["gone"])
    where = sticky.resolve(VISIBLE, 0, 20, [missing], PANE)[0]
    assert where["where"] == "above", \
        "a note from another pane is out of reach, not further down this one"


def test_placing_a_note_records_the_pane_it_was_placed_in(sticky):
    fresh = note(pane="%99", abs_line=9000)
    sticky.resolve(VISIBLE, 0, 20, [fresh], PANE)
    assert fresh["pane"] == PANE and fresh["abs_line"] == 5


def test_landmark_recovers_a_changed_row(sticky):
    # Distinct rows, so the fuzzy pass genuinely finds nothing and only the
    # landmark can place it.
    distinct = ["intro banner", "config loaded", "== build step ==",
                "compiling sources", "linking objects",
                "totally different content now", "cleanup", "done"]
    stray = note(id="b", quote="warning: deprecated API",
                 rows=["warning: deprecated API"], before=[], after=[],
                 landmark={"text": "== build step ==", "offset": 3})
    got = sticky.place_note(distinct, 0, stray)
    assert got["row"] == 5 and got.get("via") == "landmark"


def test_unfindable_is_offscreen(sticky):
    lost = note(rows=["nowhere near this text at all"], before=[], after=[])
    assert sticky.place_note(VISIBLE, 0, lost)["match"] == "offscreen"


def test_duplicate_rows_resolved_by_remembered_position(sticky):
    same = note(id="d", quote="same line", rows=["same line"], before=[],
                after=[], abs_line=7)
    assert sticky.place_note(["same line"] * 10, 0, same)["row"] == 7


@pytest.fixture
def near():
    """One row changed, so only an approximate match is possible."""
    return ["row 05 payloadx" if i == 5 else f"row {i:02d} payload"
            for i in range(20)]


def test_approximate_match_far_from_the_anchor_is_refused(sticky, near):
    far = note(anchor_abs=900)
    assert sticky.resolve(near, 0, 20, [far], PANE)[0]["match"] == "offscreen"


def test_approximate_match_near_the_anchor_is_kept(sticky, near):
    close = note(anchor_abs=5)
    assert sticky.resolve(near, 0, 20, [close], PANE)[0]["match"] == "fuzzy"


def test_an_exact_match_owns_its_row(sticky, near):
    owner = note(id="e", quote="row 05 payloadx", rows=["row 05 payloadx"],
                 before=[], after=[])
    rival = note(id="f", anchor_abs=5)
    placed = {p["id"]: p for p in sticky.resolve(near, 0, 20, [owner, rival])}
    assert placed["e"]["match"] == "exact" and placed["e"]["row"] == 5
    assert placed["f"]["row"] != 5


@pytest.mark.parametrize("last, cursor, expected, why", [
    (500, 501, True, "level with the cursor"),
    (499, 501, True, "the box's top border"),
    (400, 500, False, "output above the box"),
    (100, 500, False, "your own earlier turn"),
    (500, None, False, "no cursor to compare with"),
])
def test_prompt_box_detection(sticky, last, cursor, expected, why):
    assert sticky.in_prompt_box(last, cursor) is expected, why
