"""What the sidebar draws, and what the commit block looks like."""

from __future__ import annotations

import re

import pytest

STRIKE = "\x1b[9m"
REVERSE = "\x1b[7m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def plain(line: str) -> str:
    """A rendered line as the eye sees it, with the colour codes taken out."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def placed(row, ident, text, **over):
    inner = {"status": "pending", "note": text, "quote": "q", "rows": ["q"],
             "abs_line": row if row is not None else 0}
    inner.update(over.pop("note", {}))
    return {"row": row, "match": over.pop("match", "exact"), "id": ident,
            "note": inner, **over}


def test_single_line_commit_form(sticky):
    assert sticky.render_commit(
        {"quote": "hello world", "note": "why"}) == "hello world - why"


def test_multi_line_commit_form(sticky):
    assert sticky.render_commit(
        {"quote": "one\ntwo", "note": "why"}) == "> one\n> two\n- why"


def test_a_struck_out_note_is_dashed_out_and_marked(sticky):
    struck = placed(2, "d", "dropped", note={"deleted": True})
    drawn = "\n".join(sticky.build_frame([struck], 34, 12))
    assert STRIKE in drawn and "x dropped" in drawn


def test_the_frame_reports_where_each_note_was_drawn(sticky):
    hits = []
    sticky.build_frame([placed(2, "d", "dropped")], 34, 12, 0, hits)
    assert hits and hits[0]["id"] == "d"
    assert hits[0]["x"] == sticky.close_column(34)


class TestTheCloseButton:
    """The button that strikes a note out. A click counts when its column is
    at or past the recorded one, so the cells it is drawn in and the cells
    that answer to it have to be the same ones."""

    def rows(self, sticky, placed, width=34, height=30, scroll=0):
        hits = []
        frame = sticky.build_frame(placed, width, height, scroll, hits)
        return [plain(line) for line in frame], hits

    @staticmethod
    def buttons(hits, width=34):
        """The hits a click can actually strike out: every row of a note is
        a way into it, but only the row the button is drawn on answers."""
        return [hit for hit in hits if hit["x"] < width]

    def test_every_note_offers_exactly_one(self, sticky):
        lines, hits = self.rows(sticky, [
            placed(None, "a", "above the view"),
            placed(12, "b", "on its own row"),
        ])
        assert sum(line.count(sticky.CLOSE) for line in lines) == 2
        assert len(self.buttons(hits)) == 2
        assert {hit["id"] for hit in hits} == {"a", "b"}

    def test_the_band_does_not_repeat_an_aligned_note(self, sticky):
        """The band lists what is *above* the view. With nothing above it,
        listing anything would draw a note twice - two buttons for one note."""
        for scroll in (0, 1):
            lines, hits = self.rows(sticky, [
                placed(12, "b", "on its own row"),
                placed(20, "c", "further down"),
            ], scroll=scroll)
            assert sum(line.count(sticky.CLOSE) for line in lines) == 2
            assert [hit["id"] for hit in self.buttons(hits)] == ["b", "c"]
            assert not any("\u2500" * 5 in line for line in lines)

    @pytest.mark.parametrize("width", [24, 34, 60])
    @pytest.mark.parametrize("note", [
        {"note": "short", "quote": "q"},
        {"note": "a note whose text runs on well past the sidebar width",
         "quote": "a quoted line that is itself far too long to fit here"},
        {"note": "no quote at all, so the button shares the text row",
         "quote": ""},
    ])
    def test_it_is_drawn_in_the_cells_it_answers_to(self, sticky, width, note):
        for row in (None, 12):
            lines, hits = self.rows(
                sticky, [placed(row, "n", note["note"], note=note)], width)
            drawn = next(line for line in lines if sticky.CLOSE in line)
            assert len(drawn) <= width         # never past the pane's edge
            assert drawn.index(sticky.CLOSE) == hits[0]["x"]
            assert drawn.endswith(sticky.CLOSE)

    def test_the_cursor_lights_the_button_it_would_press(self, sticky):
        """`x` strikes out the note under the cursor, so that note's button
        is the one drawn bold - the key and the button as the same thing."""
        rows = [placed(None, "a", "above"), placed(12, "b", "on its own row")]
        for lit in ("a", "b"):
            frame = sticky.build_frame(rows, 34, 30, 0, None, 0, None, lit)
            styled = {}
            for line in frame:
                for style in (BOLD, DIM):
                    if style + sticky.CLOSE in line:
                        styled[plain(line).strip()[:5]] = style
            assert sorted(styled.values()) == sorted([BOLD, DIM])

    def test_no_cursor_leaves_every_button_at_rest(self, sticky):
        frame = sticky.build_frame(
            [placed(None, "a", "above"), placed(12, "b", "row")], 34, 30)
        assert not any(BOLD + sticky.CLOSE in line for line in frame)
        assert sum(DIM + sticky.CLOSE in line for line in frame) == 2

    def test_the_whole_note_still_reaches_the_row_under_it(self, sticky):
        """The button takes its cells from the row it sits on, not from the
        text: wrapping narrower is fine, dropping a word is not."""
        text = "no quote at all, so the button shares the first text row"
        lines, _ = self.rows(
            sticky, [placed(12, "n", text, note={"quote": ""})])
        shown = " ".join(lines[12:15]).replace(sticky.CLOSE, "")
        assert shown.split() == ["\u258c", "-", *text.split()]


def test_the_selected_note_is_marked(sticky):
    marked = "\n".join(sticky.build_frame(
        [placed(4, "pick", "chosen")], 34, 12, 0, None, 0, None, "pick"))
    assert REVERSE in marked


class TestPannedWindow:
    """A window taller than the terminal: the map speaks in pane rows, while
    the band and the footer stay where the eye can see them."""

    @pytest.fixture
    def frame_and_hits(self, sticky):
        rows = [placed(40, "p", "on the row"), placed(5, "q", "above the view")]
        hits = []
        return sticky.build_frame(rows, 34, 60, 0, hits, 20), hits

    def test_one_line_per_pane_row(self, frame_and_hits):
        frame, _ = frame_and_hits
        assert len(frame) == 60

    def test_nothing_is_drawn_above_the_viewport(self, frame_and_hits):
        frame, _ = frame_and_hits
        assert not any(line.strip() for line in frame[:20])

    def test_the_band_starts_at_the_top_of_the_viewport(self, frame_and_hits):
        frame, _ = frame_and_hits
        assert "above the view" in frame[20]

    def test_an_aligned_note_keeps_its_row(self, frame_and_hits):
        frame, _ = frame_and_hits
        assert "on the row" in frame[41]

    def test_the_footer_stays_last(self, frame_and_hits):
        frame, _ = frame_and_hits
        assert "pending" in frame[-1]

    def test_click_rows_follow_the_band(self, frame_and_hits):
        _, hits = frame_and_hits
        assert hits and all(hit["row"] >= 20 for hit in hits)


@pytest.mark.parametrize("above", [2, 4, 6, 20])
def test_the_cursor_can_reach_every_note_and_stays_on_screen(sticky, above):
    """Walking the cursor upward must page the band, never leave it behind:
    what you would act on and what you can see must not disagree."""
    rows = [placed(None, f"old{i}", f"note {i}", match="offscreen",
                   where="above", note={"abs_line": i})
            for i in range(above)]
    rows += [placed(row, f"on{j}", f"live {j}", note={"abs_line": 100 + j})
             for j, row in enumerate((14, 18))]
    order = [q["id"] for q in rows]

    def draw(cursor, scroll):
        for _ in range(len(order) + 2):
            hits = []
            sticky.build_frame(rows, 34, 22, scroll, hits, 0, None, cursor)
            shown = [h["id"] for h in hits]
            if cursor is None or cursor in shown or not shown:
                return scroll, shown
            older = order.index(cursor) < order.index(shown[0])
            moved = scroll + 1 if older else max(0, scroll - 1)
            if moved == scroll:
                return scroll, shown
            scroll = moved
        return scroll, shown

    scroll, shown = draw(None, 0)
    cursor = shown[0]
    for _ in range(above + 2):
        cursor = order[max(0, order.index(cursor) - 1)]
        scroll, shown = draw(cursor, scroll)
        assert cursor in shown, f"cursor {cursor} scrolled out of sight"
    assert cursor == "old0", "the cursor never reached the oldest note"


class TestTheDrawnSidebar:
    """Colours and glyphs, on a frame small enough to read whole."""

    @pytest.fixture
    def frame(self, sticky):
        rows = [
            placed(1, "a", "first note here",
                   note={"quote": "alpha row", "rows": ["alpha row"]}),
            placed(4, "b", "second note",
                   note={"status": "committed", "quote": "beta row",
                         "rows": ["beta", "row", "three"]}),
            placed(None, "c", "hidden", match="offscreen",
                   note={"quote": "gone", "rows": ["gone"]}),
        ]
        return sticky.build_frame(rows, 34, 10)

    def test_one_line_per_row_plus_a_footer(self, frame):
        assert len(frame) == 10

    def test_a_pending_note_is_yellow(self, frame):
        assert "\x1b[33m" in "".join(frame)

    def test_a_committed_note_is_grey(self, frame):
        assert "\x1b[90m" in "".join(frame)

    def test_a_multi_row_note_draws_a_bracket(self, frame):
        assert "┌" in "".join(frame) and "└" in "".join(frame)

    def test_the_footer_counts_what_is_above(self, frame):
        assert "↑1" in frame[-1]


class TestTheBand:
    """The dense list of notes above the view, and paging back through it."""

    def band(self, sticky, scroll=0):
        rows = [placed(None, f"old{i}", f"older note {i}", match="offscreen",
                       where="above", note={"abs_line": i})
                for i in range(12)]
        rows.append(placed(20, "live", "on screen",
                           note={"quote": "alpha", "rows": ["alpha"],
                                 "abs_line": 100}))
        return sticky.build_frame(rows, 34, 30, scroll)

    def test_it_lists_the_notes_above_the_view(self, sticky):
        joined = "\n".join(self.band(sticky))
        assert "- older note 11" in joined, "the nearest one sits at the bottom"
        assert "↑ 6 more" in joined, "and says how many did not fit"

    def test_it_is_separated_from_the_aligned_part(self, sticky):
        assert any(line.count("─") > 10 for line in self.band(sticky))

    def test_an_aligned_note_still_sits_on_its_own_row(self, sticky):
        frame = self.band(sticky)
        assert "on screen" in frame[20] or "on screen" in frame[21]

    def test_scrolling_pages_back_through_the_history(self, sticky):
        scrolled = "\n".join(self.band(sticky, scroll=3))
        assert "esc" in scrolled
        assert "- older note 3" in scrolled
        assert "- older note 11" not in scrolled

    def test_the_running_count_lives_in_the_footer(self, sticky):
        assert "↑12 above" in self.band(sticky)[-1]


class TestTheBandBelow:
    """Notes whose text is past the bottom edge get a band of their own."""

    def frame(self, sticky, below=2, height=20, hits=None):
        notes = [placed(2, "here", "on screen")] + [
            placed(None, f"b{i}", f"later note {i}", where="below",
                   note={"abs_line": 50 + i}) for i in range(below)]
        return [plain(line) for line in
                sticky.build_frame(notes, 34, height, hits=hits)]

    def test_it_lists_them_at_the_foot(self, sticky):
        frame = self.frame(sticky)
        assert "- later note 0" in frame[-3]
        assert "- later note 1" in frame[-2], "nearest first, then downwards"

    def test_it_is_separated_from_the_aligned_part(self, sticky):
        assert self.frame(sticky)[-4].count("─") > 10

    def test_the_aligned_note_keeps_its_row(self, sticky):
        assert "on screen" in "\n".join(self.frame(sticky)[:4])

    def test_the_entries_can_be_clicked(self, sticky):
        hits = []
        frame = self.frame(sticky, hits=hits)
        rows = {hit["id"]: hit["row"] for hit in hits}
        assert "later note 0" in frame[rows["b0"]]
        assert not any(hit["onscreen"] for hit in hits if hit["id"] == "b0")

    def test_it_says_how_many_did_not_fit(self, sticky):
        assert "↓ 6 more" in "\n".join(self.frame(sticky, below=9))

    def test_a_short_pane_gets_no_band(self, sticky):
        frame = self.frame(sticky, height=8)
        assert not any(line.count("─") > 10 for line in frame)
        assert "later note 0" not in "\n".join(frame)


class TestLayingOutTheLine:
    """The note prompt breaks its own rows rather than letting the terminal.

    A terminal wraps at its own last column, and whether it has wrapped yet
    when the text ends exactly there is not something you can ask it - so the
    cursor arithmetic would be guessing. One spare column removes the guess.
    """

    def test_a_short_line_is_one_row(self, sticky):
        rows, where = sticky.lay_out("note> hi", 40)
        assert rows == ["note> hi"]
        assert where[-1] == (0, 8), "the cursor sits after the last character"

    def test_it_breaks_at_the_width(self, sticky):
        rows, _ = sticky.lay_out("abcdefghij", 4)
        assert rows == ["abcd", "efgh", "ij"]

    def test_every_character_knows_its_row_and_column(self, sticky):
        _, where = sticky.lay_out("abcdefghij", 4)
        assert where[0] == (0, 0)
        assert where[4] == (1, 0), "the fifth character starts the second row"
        assert where[9] == (2, 1)

    def test_a_wide_character_is_never_split(self, sticky):
        rows, _ = sticky.lay_out("ab\u4e16\u754c", 3)
        assert rows == ["ab", "\u4e16", "\u754c"], \
            "a two-column glyph moves to the next row rather than straddling"

    def test_the_empty_line_still_has_somewhere_to_type(self, sticky):
        rows, where = sticky.lay_out("", 10)
        assert rows == [""] and where == [(0, 0)]


class TestUpAndDownInTheNotePrompt:
    """A note long enough to wrap gets rows, so up and down should cross them.

    The rows are the drawn ones, not anything in the text: `row_step` is
    handed the same `lay_out` the cursor arithmetic uses, so the cursor goes
    where the eye says it will.
    """

    def step(self, sticky, text, index, direction, goal=None):
        rows, where = sticky.lay_out(text, 4)
        return sticky.row_step(rows, where, index, direction, goal)

    def test_up_keeps_the_column(self, sticky):
        # "abcdefghij" at width 4 is "abcd" / "efgh" / "ij"; index 6 is "g".
        assert self.step(sticky, "abcdefghij", 6, -1)[0] == 2, "above g is c"

    def test_down_keeps_the_column(self, sticky):
        assert self.step(sticky, "abcdefghij", 2, 1)[0] == 6

    def test_the_first_row_has_nothing_above_it(self, sticky):
        assert self.step(sticky, "abcdefghij", 2, -1)[0] == 2

    def test_the_last_row_has_nothing_below_it(self, sticky):
        assert self.step(sticky, "abcdefghij", 9, 1)[0] == 9

    def test_a_short_row_takes_the_cursor_to_its_end(self, sticky):
        # The third row is "ij", so column 3 does not exist there.
        index, _ = self.step(sticky, "abcdefghij", 7, 1)
        assert index == 10, "one past the j, which is where you would type"

    def test_the_column_survives_a_short_row(self, sticky):
        # Down onto the short row and back up should return to where it
        # started, rather than staying where the short row left it.
        index, goal = self.step(sticky, "abcdefghij", 7, 1)
        assert goal == 3
        assert self.step(sticky, "abcdefghij", index, -1, goal)[0] == 7

    def test_a_line_that_never_wrapped_does_not_move(self, sticky):
        rows, where = sticky.lay_out("hi", 40)
        assert sticky.row_step(rows, where, 1, -1, None)[0] == 1
        assert sticky.row_step(rows, where, 1, 1, None)[0] == 1


class TestTheFootBand:
    """Notes whose text is below the screen get a band at the foot.

    It takes its rows off the bottom of the aligned map, which is where both
    of these went wrong: a note aligned to a row the band has taken, and a
    band too short to say how much it is not showing.
    """

    @staticmethod
    def note(nid, abs_line, text):
        return {"id": nid, "note": text, "quote": text, "rows": [text],
                "span": 1, "abs_line": abs_line, "status": "pending",
                "created": "", "deleted": False, "committed_at": None,
                "landmark": None}

    def aligned(self, nid, row):
        return {"id": nid, "row": row, "match": "exact",
                "note": self.note(nid, row, f"note {nid}")}

    def below(self, nid, abs_line):
        return {"id": nid, "row": None, "where": "below", "match": "exact",
                "note": self.note(nid, abs_line, f"below {nid}")}

    def test_no_two_notes_share_a_row(self, sticky):
        placed = [self.aligned("a", 18), self.aligned("b", 20),
                  self.aligned("c", 21),
                  self.below("d", 40), self.below("e", 41),
                  self.below("f", 42)]
        hits = []
        sticky.build_frame(placed, 34, 24, 0, hits, 0, None, None)
        rows = {}
        for hit in hits:
            rows.setdefault(hit["row"], []).append(hit["id"])
        clashes = {row: ids for row, ids in rows.items() if len(ids) > 1}
        assert not clashes, f"two notes drawn on one row: {clashes}"

    def test_a_displaced_note_is_listed_rather_than_stacked(self, sticky):
        placed = [self.aligned("a", 18), self.aligned("b", 20),
                  self.below("d", 40)]
        hits = []
        sticky.build_frame(placed, 34, 24, 0, hits, 0, None, None)
        drawn = {hit["id"] for hit in hits}
        assert "b" in drawn, "the note the foot band displaced went missing"

    @pytest.mark.parametrize("foot_h, count", [(2, 2), (2, 5), (3, 5)])
    def test_it_always_says_how_many_it_is_hiding(self, sticky, foot_h, count):
        beneath = [self.below(chr(97 + i), 50 + i) for i in range(count)]
        out = sticky.foot_lines(beneath, 34, foot_h)
        assert len(out) <= foot_h
        listed = sum(1 for line in out if "below" in line)
        if listed < count:
            assert any("more" in line for line in out), \
                "the count is the one line that must survive a trim"


class TestClickingANote:
    """Where a note answers to a click.

    A note drawn on the row its text sits on is a quote line with the answer
    written under it. Only the quote line used to be a target, so the half
    you wrote - the half you are looking for when you go back to it - was
    the half you could not click. Both bands treat the whole entry as one.
    """

    @staticmethod
    def wrapped():
        note = {"id": "n1", "status": "pending", "deleted": False,
                "note": "a longer answer that will certainly wrap onto a "
                        "second and a third row of the sidebar",
                "quote": "line 03 of claude output",
                "rows": ["line 03 of claude output"], "span": 1,
                "abs_line": 3, "committed_at": None, "landmark": None}
        return [{"id": "n1", "row": 4, "match": "exact", "note": note}]

    def test_every_row_it_is_written_on_answers(self, sticky):
        hits = []
        frame = sticky.build_frame(self.wrapped(), 34, 24, 0, hits)
        drawn = [row for row, line in enumerate(frame)
                 if plain(line).strip() and row >= 4][:len(hits)]
        assert [hit["row"] for hit in hits] == drawn
        assert len(hits) > 1, "the answer wrapped, so there is a row below"
        assert {hit["id"] for hit in hits} == {"n1"}

    def test_only_the_first_row_can_strike_it_out(self, sticky):
        hits = []
        sticky.build_frame(self.wrapped(), 34, 24, 0, hits)
        with_button = [hit for hit in hits if hit["x"] < 34]
        assert len(with_button) == 1
        assert with_button[0]["row"] == min(hit["row"] for hit in hits)


class TestTruncatingColouredText:
    """Colour is written into a line as escape sequences that take no room.

    Counted as characters they make a line look far wider than it is, and it
    gets cut long before its end - which is how a footer with one highlighted
    word lost the rest of itself.
    """

    def test_colour_costs_no_width(self, sticky):
        plain = "0 pending  down 19 new  ? help"
        fancy = (f"0 pending  {sticky.REVERSE}down 19 new{sticky.RESET}"
                 f"  ? help")
        assert sticky.visible(fancy) == len(plain)
        assert sticky.truncate(fancy, 34) == fancy, "cut though it fits"

    def test_an_escape_is_never_cut_in_half(self, sticky):
        fancy = f"abcdef{sticky.REVERSE}ghijkl{sticky.RESET}mnop"
        for width in range(2, 20):
            cut = sticky.truncate(fancy, width)
            assert cut.count("\x1b[") == len(sticky.ANSI.findall(cut))
            for code in sticky.ANSI.findall(cut):
                assert code.endswith("m"), "half an escape is printed, not obeyed"

    def test_it_still_cuts_what_is_actually_drawn(self, sticky):
        fancy = f"{sticky.REVERSE}{'x' * 40}{sticky.RESET}"
        assert sticky.visible(sticky.truncate(fancy, 10)) <= 10


class TestNewLinesWhileReading:
    """Scrolled back, the view holds still and tmux's scroll position does
    not move as output arrives - so nothing on screen says a reply has been
    printing while you read.

    It is said on the status line, which is at the bottom of the window you
    are looking at rather than at the foot of the pane beside it, and which
    is there already - so saying it costs no room.
    """

    @staticmethod
    def placed():
        note = {"id": "a", "note": "n", "quote": "q", "rows": ["q"], "span": 1,
                "abs_line": 1, "status": "pending", "deleted": False,
                "committed_at": None, "landmark": None}
        return [{"id": "a", "row": 1, "match": "exact", "note": note}]

    def test_the_status_line_asks_for_the_count(self, sticky):
        line = next(ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                    if ln.startswith("set -g status-right "))
        assert "#{?@sticky_new," in line, "shown only when there is a count"
        assert "#[reverse]" in line, "a count nobody notices is no count"

    def test_it_says_more_when_there_is_room_for_more(self, sticky):
        """Long enough to read at a glance where the window is wide, short
        where the tabs need the columns more."""
        line = next(ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                    if ln.startswith("set -g status-right "))
        assert " new output rows below," in line, "the long form"
        assert ", below}" in line, "and the short one it falls back to"

    def test_the_room_is_measured_as_a_number(self, sticky):
        """`#{>:100,90}` is false - those comparisons are on text, so the
        arithmetic form is the one that answers about widths."""
        line = next(ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                    if ln.startswith("set -g status-right "))
        assert "#{e|>:" in line
        assert "#{client_width}" in line and "#{session_windows}" in line

    def test_the_sidebar_footer_leaves_it_alone(self, sticky):
        """It used to be a word among three other counts at the foot of a
        narrow pane, which is where it went unnoticed."""
        assert "new" not in sticky.footer_text(self.placed(), 0, 37)
