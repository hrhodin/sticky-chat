"""What the sidebar draws, and what the commit block looks like."""

from __future__ import annotations

import argparse
import re

import pytest

STRIKE = "\x1b[9m"
REVERSE = "\x1b[7m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def written(path: str) -> str:
    """What a log file holds, read the once."""
    with open(path) as fh:
        return fh.read()


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


class TestNotesFromAPaneThatHasGone:
    """A resumed or reopened tab is full of notes whose rows were counted in
    a pane that no longer exists.

    `abs_line` counts from the top of one pane's scrollback, and the panes
    that came before had been running all day - so their numbers are the
    largest in the store. Sorted in with the rest they take the end of the
    list, which is the end the band shows, and the note you just took is
    nowhere to be seen.
    """

    def rows(self):
        gone = [placed(None, f"gone{i}", f"last time {i}", match="offscreen",
                       where="above", here=False,
                       note={"abs_line": 14000 + i}) for i in range(10)]
        here = [placed(None, f"here{i}", f"just now {i}", match="offscreen",
                       where="above", here=True,
                       note={"abs_line": 100 + i}) for i in range(2)]
        return gone + here

    def test_the_note_just_taken_is_the_one_at_the_bottom(self, sticky):
        lines = [plain(line) for line in
                 sticky.build_frame(self.rows(), 34, 30)]
        listed = [line for line in lines
                  if "just now" in line or "last time" in line]
        assert listed, lines
        assert "just now 1" in listed[-1], listed

    def test_they_sort_above_everything_taken_here(self, sticky):
        order = [item["id"] for item in sticky.history_order(self.rows())]
        assert order[-2:] == ["here0", "here1"]
        assert order[0].startswith("gone")

    def test_a_note_with_no_answer_either_way_keeps_its_place(self, sticky):
        """Nothing says `here` in a frame built by hand, or by an older
        sidebar: that has to go on meaning what it meant."""
        rows = [placed(None, "a", "a", match="offscreen", where="above",
                       note={"abs_line": 9}),
                placed(None, "b", "b", match="offscreen", where="above",
                       note={"abs_line": 4})]
        assert [p["id"] for p in sticky.history_order(rows)] == ["b", "a"]


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


class TestReadingATimeToSendAt:
    """Three shapes, because three are what anybody types.

    The one that needed deciding is a bare time of day that has already
    passed: `00:32` typed at midnight means tonight's 00:32 if it is still
    to come and tomorrow's if it is not, and never yesterday's.
    """

    NOW = None      # set in setup, a fixed Tuesday 22:00

    def setup_method(self):
        import time
        self.NOW = time.mktime((2026, 9, 15, 22, 0, 0, 0, 0, -1))

    def said(self, sticky, text):
        import time
        when = sticky.when_to_send(text, self.NOW)
        return time.strftime("%d %H:%M", time.localtime(when)) if when else ""

    def test_a_wait(self, sticky):
        assert self.said(sticky, "4h") == "16 02:00"
        assert self.said(sticky, "90m") == "15 23:30"
        assert self.said(sticky, "30s") == "15 22:00"
        assert self.said(sticky, "+2h") == "16 00:00", "a leading + is allowed"

    def test_a_time_still_to_come_today(self, sticky):
        assert self.said(sticky, "23:15") == "15 23:15"

    def test_a_time_already_past_means_tomorrow(self, sticky):
        assert self.said(sticky, "09:00") == "16 09:00"
        assert self.said(sticky, "00:32") == "16 00:32"

    def test_a_date_and_time_is_taken_exactly(self, sticky):
        """What an agent says when it tells you which day its limit
        resets."""
        assert self.said(sticky, "2026-09-16 00:32") == "16 00:32"
        assert self.said(sticky, "2026-09-16T00:32") == "16 00:32"

    def test_a_clock_may_wear_an_am_or_a_pm(self, sticky):
        """Agents write times the way people do, and what one printed is
        what gets typed back."""
        assert self.said(sticky, "8pm") == "16 20:00"
        assert self.said(sticky, "3:00 PM") == "16 15:00"
        assert self.said(sticky, "12:32 AM") == "16 00:32"
        assert self.said(sticky, "8 p.m.") == "16 20:00"
        assert self.said(sticky, "12pm") == "16 12:00", "noon, not midnight"
        assert self.said(sticky, "12am") == "16 00:00", "and midnight"

    def test_a_date_can_wear_one_too(self, sticky):
        """The whole of what an agent prints when it says which day."""
        assert self.said(sticky, "2026-09-16 12:32 AM") == "16 00:32"

    def test_a_wait_is_not_read_as_a_meridiem(self, sticky):
        assert self.said(sticky, "30m") == "15 22:30", "m is minutes"
        assert self.said(sticky, "4h") == "16 02:00"

    def test_nonsense_is_refused_rather_than_guessed(self, sticky):
        for text in ("half past", "", "   ", "25:00", "12:99", "soon",
                     "13pm", "0am", "pm"):
            assert sticky.when_to_send(text, self.NOW) == 0.0, text


class TestGuessingWhenTheAgentGetsItsTurnsBack:
    """The scan behind the `t` key's pre-filled prompt.

    Three wordings, all of them real. What comes out is what goes back into
    `when_to_send`, and nothing at all when the pane says nothing: the guess
    is a default in a prompt you can edit, so finding none costs a typed
    time and never a wrong send.
    """

    NOW = None      # the same fixed Tuesday 22:00

    def setup_method(self):
        import time
        self.NOW = time.mktime((2026, 9, 15, 22, 0, 0, 0, 0, -1))

    def test_a_date_and_a_time(self, sticky):
        rows = ["> what does this do?", "",
                "You've hit your usage limit. You can try again at "
                "Sep 16th, 2026 12:32 AM."]
        assert sticky.reset_time(rows, self.NOW) == "2026-09-16 00:32"

    def test_a_time_of_day(self, sticky):
        assert sticky.reset_time(["Your limit resets at 3:00 PM."],
                                 self.NOW) == "15:00"

    def test_a_time_with_no_minutes(self, sticky):
        assert sticky.reset_time(["5-hour limit reached ∙ resets 8pm"],
                                 self.NOW) == "20:00"

    def test_a_pane_with_no_time_in_it_offers_nothing(self, sticky):
        rows = ["  2 files changed, 41 insertions(+)",
                "All 276 tests pass.", "", "> "]
        assert sticky.reset_time(rows, self.NOW) == ""

    def test_a_clock_without_the_word_is_not_one(self, sticky):
        """A pane full of timestamps is the ordinary case, not a notice."""
        assert sticky.reset_time(["git reset --hard  # 12:30 yesterday"],
                                 self.NOW) == ""

    def test_the_bare_word_is_not_the_notice(self, sticky):
        """This one really happened, in the tab this was written in.

        `--limit 40` has the word, `[5:16]` has two numbers and a colon
        between them, and the tab armed itself to say "continue" at twenty
        past five. A shell is full of lines like it. What the real notices
        share is the phrase, not the word.
        """
        assert sticky.reset_time(
            ["gh run list --limit 40 --json createdAt "
             "--jq '.[] | \"\\(.createdAt[5:16])\"'"], self.NOW) == ""
        assert sticky.reset_time(["  --limit 20  # ran at 9:30 PM"],
                                 self.NOW) == ""

    def test_it_keeps_the_row_it_read(self, sticky):
        """So that a tab that armed itself for no visible reason can be
        asked what it saw, rather than the answer being three thousand rows
        up a scrollback that has since been overwritten."""
        said, saw = sticky.reset_notice(
            ["working away", "Your limit resets at 3:00 PM.", "> "], self.NOW)
        assert said == "15:00"
        assert saw == "Your limit resets at 3:00 PM."
        assert sticky.reset_notice(["nothing here"], self.NOW) == ("", "")

    def test_the_blank_bottom_of_a_roomy_pane_is_not_output(self, sticky):
        """A pane with room to spare ends in blank rows, and counting the
        window from the bottom of the screen would look straight past what
        is written at the top of it."""
        rows = ["Your limit resets at 3:00 PM."] + [""] * 30
        assert sticky.reset_time(rows, self.NOW) == "15:00"

    def test_a_notice_scrolled_well_up_is_left_alone(self, sticky):
        rows = (["Your limit resets at 3:00 PM."]
                + [f"  work {n}" for n in range(sticky.RESET_ROWS + 1)])
        assert sticky.reset_time(rows, self.NOW) == ""

    def test_the_newest_notice_wins(self, sticky):
        rows = ["Your limit resets at 3:00 PM.",
                "...", "Your limit resets at 11:30 PM."]
        assert sticky.reset_time(rows, self.NOW) == "23:30"

    def test_a_notice_from_yesterday_is_not_a_guess(self, sticky):
        """Only a dated one can land in the past - and a deadline already
        gone would fire the instant it was set."""
        assert sticky.reset_time(
            ["usage limit reached, try again at Sep 14th, 2026 12:32 AM"],
            self.NOW) == ""

    def test_the_wording_claude_code_actually_prints(self, sticky):
        """Two real notices that went unread, and three reasons why.

        `weekly limit` was not a phrase it knew, the separator was a `·`
        rather than a space, and the date arrived with no year and an `at`
        between it and the hour.
        """
        assert sticky.reset_time(
            ["You've hit your weekly limit \u00b7 resets Sep 16 at 4am "
             "(Europe/Berlin)"], self.NOW) == "2026-09-16 04:00"
        assert sticky.reset_time(
            ["You've hit your weekly limit \u00b7 resets 4am "
             "(Europe/Berlin)"], self.NOW) == "04:00"

    def test_a_day_with_no_year_is_the_coming_one(self, sticky):
        """Claude Code prints no year, so one has to be worked out - and
        the wrong one parks a tab on a clock until next autumn."""
        import time
        eve = time.mktime((2026, 12, 31, 23, 30, 0, 0, 0, -1))
        assert sticky.reset_time(
            ["weekly limit \u00b7 resets Jan 1 at 4am"], eve) \
            == "2027-01-01 04:00", "over a new year, next year"
        assert sticky.reset_time(
            ["weekly limit \u00b7 resets Jan 2 at 4am"], self.NOW) == "", \
            "but a day months behind is a stale notice, not next year's"

    def test_a_notice_above_the_box_is_still_reached(self, sticky):
        """The rows an agent keeps below its own output are what the window
        has to clear, and Claude Code keeps more of them than codex: a
        two-line notice, the "done" line, an input box and the hints under
        it put a real notice at about twelve rows up."""
        pane = ["  \u255a  You've hit your weekly limit \u00b7 resets "
                "Sep 16 at 4am (Europe/Berlin)",
                "     /usage-credits to finish what you\u2019re working on.",
                "",
                "\u273b Cogitated for 36s \u00b7 done 12:14 AM",
                "",
                "\u2500" * 60, "\u276f", "\u2500" * 60,
                "\u23f5\u23f5 bypass permissions on \u00b7 \u2190 1 agent",
                "new task? /clear to save 932.4k tokens",
                "/rc"]
        assert sticky.reset_time(pane, self.NOW) == "2026-09-16 04:00"

    def test_what_comes_out_goes_back_in(self, sticky):
        for row in ("Your limit resets at 3:00 PM.",
                    "5-hour limit reached ∙ resets 8pm",
                    "hit your usage limit, back Sep 16th, 2026 12:32 AM",
                    "You've hit your weekly limit · resets Sep 16 at 4am"):
            guess = sticky.reset_time([row], self.NOW)
            assert guess and sticky.when_to_send(guess, self.NOW), row


class TestTheFooterSaysWhenABatchIsDue:
    def test_a_time_is_shown_when_one_is_set(self, sticky):
        import time
        note = {"note": {"status": "pending"}, "row": 1}
        assert "\u21b3" not in sticky.footer_text([note])
        soon = time.time() + 3600
        said = sticky.footer_text([note], due=soon)
        assert time.strftime("%H:%M", time.localtime(soon)) in said
        assert "pending" in said, "and what is waiting is still said first"

    def test_the_hour_says_it_is_an_hour(self, sticky):
        """Four digits and a colon beside an hourglass is a stopwatch.

        Read as how long the tab has been idle, which is a question the
        sidebar never answers, rather than when something is going to
        happen to it - so the hour has to say which of the two it is.
        """
        import time
        note = {"note": {"status": "pending"}, "row": 1}
        said = sticky.footer_text([note], due=time.time() + 600,
                                  saying="continue")
        assert "at " in said, f"a clock, not a duration: {said!r}"


class TestLightingTheAnnotatedLines:
    """tmux will not restyle a pane that is being written to - but a pane
    you have scrolled back in is in copy mode, and copy mode paints the
    matches of a search. So the search is set for you on the way in."""

    def test_it_matches_the_row_and_not_the_selection(self, sticky):
        """The quote is what you dragged over, which may be half a line,
        and half a line lit reads as a mistake."""
        pattern = sticky.mark_pattern(
            [{"rows": ["def greet(name): print(nome)"], "quote": "nome"}])
        assert pattern == r"def greet\(name\): print\(nome\)"

    def test_every_note_goes_in_one_search(self, sticky):
        pattern = sticky.mark_pattern([{"rows": ["first line here"]},
                                       {"rows": ["second line here"]}])
        assert pattern == "first line here|second line here"

    def test_the_same_line_twice_is_one_alternative(self, sticky):
        pattern = sticky.mark_pattern([{"rows": ["marked line"]},
                                       {"rows": ["marked line"]}])
        assert pattern == "marked line"

    def test_a_struck_out_note_lights_nothing(self, sticky):
        assert not sticky.mark_pattern(
            [{"rows": ["gone now"], "deleted": True}])

    def test_a_scrap_of_a_line_is_left_alone(self, sticky):
        """Two letters are matched all over a pane, and lighting up rows
        nobody annotated is worse than lighting up none."""
        assert not sticky.mark_pattern([{"rows": ["ab"]}])

    def test_a_quote_is_cut_to_its_distinctive_head(self, sticky):
        """The search runs inside the tmux server and blocks it while it
        runs, so the expression it is given is kept short: a first line is
        distinctive long before its fortieth character."""
        row = "x" * 200
        pattern = sticky.mark_pattern([{"rows": [row]}])
        assert len(pattern) == sticky.MARK_CHARS

    def test_it_does_not_grow_without_end(self, sticky):
        """A regular expression the width of a hundred notes is one tmux
        runs against every row it draws."""
        many = [{"rows": [f"row number {n} of many"]} for n in range(200)]
        assert (sticky.mark_pattern(many).count("|") + 1
                == sticky.MARK_QUOTES)


class TestTypingInTheNotesPane:
    """A letter the sidebar has no use for is not an error to swallow: it
    is the first letter of a sentence, typed with the eye on the notes. It
    crosses to the chat and arrives there - the same bargain the transcript
    makes when you type while scrolled back."""

    def test_an_ordinary_character_goes_to_the_agent(self, sticky):
        for key in ("e", "A", ".", "0", "\u20ac"):
            assert sticky.typing_through(key), key

    def test_the_sidebar_keeps_the_keys_it_answers_to(self, sticky):
        for key in "qrhgG?><xFuSst123456789 ":
            assert not sticky.typing_through(key), key

    def test_moving_about_is_not_a_sentence(self, sticky):
        """The arrows, the page keys and the wheel are how you get around
        in here, and a control code is nobody's first letter."""
        for key in ("up", "down", "pgup", "pgdn", "home", "end", "esc",
                    "\r", "\n", "\t", "\x03", "\x19", "\x05"):
            assert not sticky.typing_through(key), key


class TestTheLog:
    """What the sidebar decided, and the rows behind it.

    A tab that marks itself unread when nothing happened can only be caught
    in the act: the answer is always "what changed on screen", and the
    screen has moved on by the time anybody thinks to ask.
    """

    def test_it_writes_the_diff_and_not_the_whole_screen(self, sticky, tmp_path):
        path = str(tmp_path / "sticky.log")
        before = [f"row {n}" for n in range(200)]
        after = list(before)
        after[5] = "row five, changed"
        sticky.to_log(path, "%1", "80x24", "the screen changed", before, after)
        said = written(path)
        assert "the screen changed" in said
        assert "+row five, changed" in said
        assert "-row 5" in said, "and what it was before"
        assert "row 100" not in said, "unchanged rows are not the answer"

    def test_a_long_diff_is_cut_off(self, sticky, tmp_path):
        path = str(tmp_path / "sticky.log")
        before = [f"row {n}" for n in range(500)]
        sticky.to_log(path, "%1", "80x24", "everything", before,
                      [f"changed {n}" for n in range(500)])
        assert "... and more" in written(path)
        assert len(written(path).splitlines()) < sticky.LOG_ROWS + 5

    def test_every_line_carries_the_milliseconds(self, sticky, tmp_path):
        """What the log is for is the order things happened in and the gaps
        between them, and the gaps worth chasing are under a second."""
        path = str(tmp_path / "sticky.log")
        sticky.log_line(path, "clicked tab 7")
        said = written(path).strip()
        assert re.match(r"^\d\d:\d\d:\d\d\.\d\d\d clicked tab 7$", said), said

    def test_a_mark_from_a_key_binding_joins_the_same_timeline(
            self, sticky, tmp_path, monkeypatch):
        """The click, the hooks it sets off and every sidebar that woke up
        are one story, so they go in one file."""
        path = str(tmp_path / "sticky.log")

        class FakeTmux:
            def __init__(self, socket):
                self.socket = socket

            def run(self, *args):
                return path if args[-1] == "@sticky_log" else ""

        monkeypatch.setattr(sticky.commands, "Tmux", FakeTmux)
        args = argparse.Namespace(socket=None, label="clicked tab 7")
        assert sticky.cmd_trace(args) == 0
        assert "-- clicked tab 7" in written(path)

    def test_nothing_is_written_while_the_log_is_off(
            self, sticky, tmp_path, monkeypatch):
        class FakeTmux:
            def __init__(self, socket):
                self.socket = socket

            def run(self, *args):
                return ""

        monkeypatch.setattr(sticky.commands, "Tmux", FakeTmux)
        assert sticky.cmd_trace(
            argparse.Namespace(socket=None, label="x")) == 0
        assert not list(tmp_path.iterdir())

    def test_a_log_nobody_can_write_is_not_a_fault(self, sticky, tmp_path):
        """It is a debugging aid. Taking the sidebar down with it would be
        the tail wagging the dog."""
        sticky.to_log(str(tmp_path / "no" / "such" / "dir" / "x.log"),
                      "%1", "80x24", "anything")


class TestTwoNotesOnOneLine:
    """The same line marked twice is one quote with two things to say.

    Drawn one at a time they land on the same row and the second is written
    over the first, which then exists only in the pending count: not on
    screen, not in either band, nothing to click, and still sent when the
    batch goes. So they are drawn as one block instead.
    """

    QUOTE = "def greet(name)"

    @classmethod
    def two(cls, row=8, texts=("who is nome?", "and no argument")):
        return [placed(row, ident, text,
                       note={"quote": cls.QUOTE, "rows": [cls.QUOTE]})
                for ident, text in zip("ab", texts)]

    def test_both_are_on_screen(self, sticky):
        frame = "\n".join(sticky.build_frame(self.two(), 34, 24))
        assert "who is nome?" in frame
        assert "and no argument" in frame

    def test_the_quote_is_said_once(self, sticky):
        frame = "\n".join(sticky.build_frame(self.two(), 34, 24))
        assert frame.count(self.QUOTE) == 1, "it is one line, not two"

    def test_each_gets_its_own_button(self, sticky):
        """Or one of them cannot be struck out, and the row that could is
        the other note's."""
        hits = []
        sticky.build_frame(self.two(), 34, 24, hits=hits)
        buttons = {h["id"] for h in hits if h["x"] < 10 ** 5}
        assert buttons == {"a", "b"}, f"a button each: {hits}"

    def test_one_note_is_drawn_as_it_always_was(self, sticky):
        """The button sits on the quote row when nothing is sharing it."""
        hits = []
        sticky.build_frame(self.two(texts=("who is nome?",))[:1], 34, 24,
                           hits=hits)
        top = [h for h in hits if h["row"] == 8]
        assert top and top[0]["x"] < 10 ** 5, "the quote row carries it"


class TestTwoRowsOfTabs:
    """Agent tabs on one row, everything else on another.

    They are two kinds of thing, and one list holding both is a list you
    read twice - once for the tab you meant and once past the tabs you did
    not. What tells them apart is the mark: sticky gives every agent tab
    one, `$` is the shell's, and a window sticky never opened has none.
    """

    def test_the_second_row_is_spelt_the_way_tmux_spells_it(self, sticky):
        """`status` is a choice, and one row is `on`. `set -g status 1` is
        refused as an unknown value - which a hook cannot tell you, so it
        simply leaves the row count wherever it happened to be."""
        assert '"set -g status on"' in sticky.config.ROWS_HOOK
        assert '"set -g status 2"' in sticky.config.ROWS_HOOK
        assert " 1\"" not in sticky.config.ROWS_HOOK

    def test_each_row_asks_for_the_half_it_draws(self, sticky):
        agents = sticky.config.AGENT_ROW
        others = sticky.config.OTHER_ROW
        assert sticky.config.AGENT_TAB in agents and sticky.config.AGENT_TAB in others
        assert "window-status-current-format" in agents, "the current tab too"
        assert "range=window" in agents, "and still clickable"
        hints = "#{T;=/#{status-right-length}:status-right}"
        assert hints in agents, "the key hints stay on the first row"
        assert hints not in others, "and not on the second"

    def test_both_rows_start_their_list_in_the_same_column(self, sticky):
        """Two lists read as one when their left edges agree, and as two
        ragged ones when they do not - which is most of what having two
        rows was meant to fix."""
        assert len(sticky.config.OTHER_LABEL) == len(sticky.config.STICKY_LABEL)
        assert sticky.config.OTHER_LABEL.strip() == "other"
        assert sticky.config.STICKY_LABEL in sticky.CONFIG_TEMPLATE.replace(
            "@LABEL@", sticky.config.STICKY_LABEL), "and the row uses it"

    def test_the_separator_goes_inside_the_test(self, sticky):
        """Left where tmux puts it, a skipped tab still lays down the space
        between two tabs and the row reads `1:beta  3:gamma`, with a hole
        where the shell used to be."""
        row = sticky.config.AGENT_ROW
        gap = "#{E:window-status-separator}"
        assert gap in row
        assert f"{gap},}}" in row, "inside the conditional, not after it"


class TestTheSidebarSaysWhatIsAboutToHappen:
    """The countdown, which is the half of a clock that is read now.

    The hour is the half that is read at four in the morning. Both are
    wanted, and neither does the other's job: `at 00:03` does not say
    whether that is in a minute or tomorrow, and `in 7h` does not survive
    being looked at an hour later.
    """

    def test_a_wait_is_said_the_way_it_is_waited(self, sticky):
        assert sticky.how_long(0) == "in under a minute"
        assert sticky.how_long(90) == "in 2 min", "rounded up, never short"
        assert sticky.how_long(7200) == "in 2h"
        assert sticky.how_long(3600 * 7 + 40 * 60) == "in 7h 40m"

    def test_it_names_what_is_going(self, sticky):
        import time
        soon = time.time() + 2520
        assert "continue" in sticky.due_line(soon, "continue", 0)
        assert "3 notes" in sticky.due_line(soon, "", 3)
        assert "1 note " in sticky.due_line(soon, "", 1), "not 1 notes"
        assert not sticky.due_line(0.0, "continue", 0), "nothing due, nothing said"

    def test_it_takes_a_row_of_its_own(self, sticky):
        """Prominent means a row, not a corner of the abbreviations.

        A tab that sends something while nobody is looking is the one
        thing in here that should never arrive as a surprise.
        """
        import time
        note = placed(1, "a", "who is nome?")
        plain = sticky.build_frame([note], 34, 20)
        armed = sticky.build_frame([note], 34, 20,
                                   due=time.time() + 2520, saying="continue")
        assert len(plain) == len(armed), "the frame is still the pane's height"
        assert any("continue" in line for line in armed)
        assert not any("continue" in line for line in plain)


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
