"""End to end against a real tmux server, with no terminal attached.

These drive the same commands the key bindings do, because most of the bugs
this project has had lived in paths only a binding could reach.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import LAUNCHER, OUTER, SOCKET

pytestmark = pytest.mark.integration


def test_the_pane_produced_the_output_the_tests_expect(pane_view):
    assert any("payload-30" in row for row in pane_view["rows"])


class TestCapturingASelection:
    def test_a_whole_line_keeps_its_row_and_context(
            self, run_sticky, project, pane_view, target, notes_of):
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", "whole line",
                   "--sy", str(target["abs"]), "--sx", "0",
                   "--ey", str(target["abs"]),
                   "--ex", str(len(target["text"]) - 1), stdin=target["text"])
        rec = notes_of(project)[-1]
        rows = pane_view["rows"]
        assert rec["rows"] == [target["text"]]
        assert rec["before"] == [rows[target["index"] - 2].rstrip(),
                                 rows[target["index"] - 1].rstrip()]
        assert rec["span"] == 1
        assert rec["landmark"] is not None

    def test_a_sub_line_selection_records_its_columns(
            self, run_sticky, project, pane_view, target, notes_of):
        part = "payload-20"
        col = target["text"].index(part)
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", "just the payload",
                   "--sy", str(target["abs"]), "--sx", str(col),
                   "--ey", str(target["abs"]),
                   "--ex", str(col + len(part) - 1), stdin=part)
        rec = notes_of(project)[-1]
        assert rec["quote"] == part
        assert rec["rows"] == [target["text"]], "the anchor is the whole row"
        assert rec["partial"] is True
        assert (rec["sx"], rec["ex"]) == (col, col + len(part) - 1)

    def test_a_multi_line_selection_spans_its_rows(
            self, run_sticky, project, pane_view, notes_of):
        rows = pane_view["rows"]
        index = next(i for i, r in enumerate(rows) if "payload-24" in r)
        wanted = [rows[index + k].rstrip() for k in range(3)]
        start = pane_view["history"] + index
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", "three rows", "--sy", str(start), "--sx", "0",
                   "--ey", str(start + 2), "--ex", str(len(wanted[-1]) - 1),
                   stdin="\n".join(wanted))
        rec = notes_of(project)[-1]
        assert rec["span"] == 3
        assert rec["rows"] == wanted


class TestTheMouseUsesItsJudgement:
    """A drag into the prompt box is a plain copy, not a note."""

    def test_a_drag_by_the_prompt_makes_no_note(
            self, run_sticky, project, pane_view, notes_of):
        bottom = pane_view["history"] + pane_view["height"] - 1
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--auto", "--note", "should not exist",
                   "--sy", str(bottom), "--ey", str(bottom),
                   "--sx", "0", "--ex", "10", stdin="whatever is down there")
        assert notes_of(project) == []

    def test_a_drag_over_output_still_makes_one(
            self, run_sticky, project, pane_view, target, notes_of):
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--auto", "--note", "real note",
                   "--sy", str(target["abs"]), "--ey", str(target["abs"]),
                   "--sx", "0", "--ex", "20", stdin=target["text"])
        assert len(notes_of(project)) == 1


class TestCommitting:
    @pytest.fixture
    def two_notes(self, run_sticky, project):
        run_sticky("add", "--project", project, "--note", "keep me",
                   "--text", "first quote")
        run_sticky("add", "--project", project, "--note", "drop me",
                   "--text", "second quote")
        return project

    def test_rendering_and_status(self, run_sticky, two_notes, notes_of):
        out = run_sticky("commit", "--no-paste", "--project", two_notes)
        assert "first quote - keep me" in out
        assert "\n\n" in out, "notes are separated by a blank line"
        assert all(n["status"] == "committed" and n["committed_at"]
                   for n in notes_of(two_notes))

    def test_a_multi_line_quote_becomes_a_block(self, run_sticky, project):
        run_sticky("add", "--project", project, "--note", "three rows",
                   "--text", "one\ntwo\nthree")
        out = run_sticky("commit", "--no-paste", "--project", project)
        assert "> one" in out and "- three rows" in out

    def test_uncommit_puts_the_last_send_back(
            self, run_sticky, two_notes, notes_of):
        run_sticky("commit", "--no-paste", "--project", two_notes)
        run_sticky("uncommit", "--project", two_notes)
        back = notes_of(two_notes)
        assert all(n["status"] == "pending" and n["committed_at"] is None
                   for n in back)
        run_sticky("commit", "--no-paste", "--project", two_notes)
        assert all(n["status"] == "committed" for n in notes_of(two_notes))

    def test_a_struck_out_note_is_left_out(
            self, run_sticky, two_notes, notes_of, sticky, sticky_home):
        path = (Path(sticky_home) /
                sticky.project_slug(os.path.abspath(two_notes)) / "notes.json")
        data = json.loads(path.read_text())
        data["notes"][1]["deleted"] = True
        path.write_text(json.dumps(data))

        out = run_sticky("commit", "--no-paste", "--project", two_notes)
        assert "keep me" in out and "drop me" not in out
        statuses = [n["status"] for n in notes_of(two_notes)]
        assert statuses.count("pending") == 1, "the struck-out one stays open"


def test_a_local_store_is_used_when_the_project_has_one(
        run_sticky, project, pane_view):
    os.makedirs(os.path.join(project, ".sticky"))
    run_sticky("add", "--pane", pane_view["pane"], "--project", project,
               "--note", "local one", "--text", "HEADER alpha",
               "--sy", "0", "--ey", "0")
    assert os.path.exists(os.path.join(project, ".sticky", "notes.json"))


def test_the_commit_reaches_the_pane_as_a_bracketed_paste(
        tmux, run_sticky, project, pane_view):
    # tmux only emits the bracketed-paste markers when the receiving
    # application has enabled that mode, as Claude Code does.
    sink = tmux("split-window", "-h", "-d", "-t", pane_view["pane"],
                "-P", "-F", "#{pane_id}",
                "sh", "-c", r"printf '\033[?2004h'; cat -v").strip()
    try:
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", "paste me", "--text", "HEADER alpha",
                   "--sy", "0", "--ey", "0")
        run_sticky("commit", "--pane", sink, "--project", project)
        time.sleep(0.6)
        pasted = tmux("capture-pane", "-p", "-t", sink).strip()
        assert "HEADER alpha - paste me" in pasted
        assert "200~" in pasted, "not sent as a bracketed paste"
    finally:
        tmux("kill-pane", "-t", sink, check=False)


class TestPlacementAgainstALivePane:
    def test_a_note_follows_a_scroll(
            self, tmux, run_sticky, project, pane_view):
        rows = pane_view["rows"]
        index = next(i for i, r in enumerate(rows)
                     if "payload" in r and i >= len(rows) // 2)
        text = rows[index].rstrip()
        start = pane_view["history"] + index
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", "scroll me", "--sy", str(start), "--sx", "0",
                   "--ey", str(start), "--ex", str(len(text) - 1), stdin=text)

        placed = json.loads(run_sticky("place", "--pane", pane_view["pane"],
                                       "--project", project))
        assert placed[0]["match"] == "exact" and placed[0]["row"] == index

        tmux("copy-mode", "-t", pane_view["pane"])
        tmux("send-keys", "-t", pane_view["pane"], "-X", "-N", "4",
             "scroll-up")
        try:
            placed = json.loads(run_sticky("place", "--pane",
                                           pane_view["pane"],
                                           "--project", project))
            assert placed[0]["row"] == index + 4
        finally:
            tmux("send-keys", "-t", pane_view["pane"], "-X", "cancel",
                 check=False)


def test_the_real_copy_pipe_binding_delivers_a_selection(
        tmux, sticky_home, project, tmp_path):
    """The binding needs an attached client, so drive one from a second
    server. This is the only test that exercises the actual key binding."""
    subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                   capture_output=True)
    subprocess.run(["tmux", "-L", OUTER, "new-session", "-d",
                    "-x", "120", "-y", "40",
                    f"tmux -L {SOCKET} attach -t sticky"],
                   capture_output=True, check=True)
    try:
        time.sleep(0.8)
        pane = tmux("display-message", "-p", "-t", "sticky:",
                    "#{pane_id}").strip()
        binding = (f"STICKY_HOME={sticky_home} {sys.executable} {LAUNCHER} "
                   f"add --socket {SOCKET} --project {project} "
                   f"--note 'from copy-pipe' "
                   '--pane "#{pane_id}" --sy "#{selection_start_y}" '
                   '--sx "#{selection_start_x}" --ey "#{selection_end_y}" '
                   '--ex "#{selection_end_x}" --hsize "#{history_size}"')
        for table in ("copy-mode", "copy-mode-vi"):
            tmux("bind-key", "-T", table, "M", "send-keys", "-X",
                 "copy-pipe-and-cancel", binding)
        tmux("copy-mode", "-t", pane)
        tmux("send-keys", "-t", pane, "-X", "history-top")
        tmux("send-keys", "-t", pane, "-X", "-N", "6", "cursor-down")
        tmux("send-keys", "-t", pane, "-X", "begin-selection")
        tmux("send-keys", "-t", pane, "-X", "-N", "2", "cursor-down")
        tmux("send-keys", "-t", pane, "-X", "end-of-line")
        tmux("send-keys", "-t", pane, "M")
        time.sleep(1.2)
        tmux("send-keys", "-t", pane, "-X", "cancel", check=False)

        found = None
        for entry in Path(sticky_home).iterdir():
            candidate = entry / "notes.json"
            if candidate.exists():
                data = json.loads(candidate.read_text())
                if data.get("project") == project:
                    found = data["notes"]
        assert found, "the binding produced no note"
        rec = found[-1]
        assert rec["span"] == 3
        assert rec["quote"].split("\n")[0].strip() in rec["rows"][0]
    finally:
        subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                       capture_output=True)


def test_several_notes_are_all_placed_exactly(
        run_sticky, project, pane_view, target):
    """Notes are resolved against each other, so more than one at a time is
    the case that matters."""
    rows = pane_view["rows"]
    for payload in ("payload-20", "payload-24", "payload-28"):
        index = next(i for i, r in enumerate(rows) if payload in r)
        text = rows[index].rstrip()
        start = pane_view["history"] + index
        run_sticky("add", "--pane", pane_view["pane"], "--project", project,
                   "--note", f"note on {payload}", "--sy", str(start),
                   "--sx", "0", "--ey", str(start),
                   "--ex", str(len(text) - 1), stdin=text)
    placed = json.loads(run_sticky("place", "--pane", pane_view["pane"],
                                   "--project", project))
    assert len(placed) == 3
    assert all(p["match"] == "exact" for p in placed)
    assert len({p["row"] for p in placed}) == 3, "each on its own row"


def test_notes_go_off_screen_when_the_text_is_gone(
        tmux, server, run_sticky, project, tmp_path):
    """A pane of its own: replacing the shared one would strand every other
    test that points at a payload row."""
    own = tmux("new-window", "-d", "-t", "sticky:", "-P", "-F", "#{pane_id}",
               "sh", "-c", "printf 'ORIGINAL %d\\n' 1 2 3 4 5; sleep 300"
               ).strip()
    try:
        time.sleep(0.6)
        raw = tmux("display-message", "-p", "-t", own,
                   "#{history_size}").strip()
        run_sticky("add", "--pane", own, "--project", project,
                   "--note", "will vanish", "--sy", raw, "--ey", raw,
                   "--sx", "0", "--ex", "9", stdin="ORIGINAL 1")
        placed = json.loads(run_sticky("place", "--pane", own,
                                       "--project", project))
        assert placed[0]["match"] == "exact"

        tmux("respawn-pane", "-k", "-t", own, "sh", "-c",
             "printf 'REPLACED %d\\n' 1 2 3; sleep 300")
        time.sleep(0.6)
        placed = json.loads(run_sticky("place", "--pane", own,
                                       "--project", project))
        assert all(p["match"] == "offscreen" for p in placed)
    finally:
        index = tmux("display-message", "-p", "-t", own, "#{window_index}",
                     check=False).strip()
        if index:
            tmux("kill-window", "-t", f"sticky:{index}", check=False)
