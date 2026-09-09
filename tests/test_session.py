"""Starting, forking, and the window taller than the terminal.

These open real windows, so each one cleans up after itself: a stray window
here would confuse every test that lists panes.
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


def fake_claude(directory, body="printf 'hello from claude\\n'"):
    """A stand-in that prints and then waits, so the pane stays alive."""
    path = Path(directory) / "fake-claude"
    path.write_text(f"#!/bin/sh\n{body}\nsleep 300\n")
    path.chmod(0o755)
    return str(path)


def dying_claude(directory, seconds=3):
    """A stand-in that ends on its own, the way /exit does."""
    path = Path(directory) / "dying-claude"
    path.write_text(f"#!/bin/sh\nprintf 'hello\\n'\nsleep {seconds}\n")
    path.chmod(0o755)
    return str(path)


def shut(tmux, panes):
    for pane in panes:
        index = tmux("display-message", "-p", "-t", pane, "#{window_index}",
                     check=False).strip()
        if not index:
            continue
        # The session is shared by the whole run and its fixtures are
        # session-scoped, so taking its last window - and with it the server -
        # would strand every test that comes after.
        left = tmux("list-windows", "-t", "sticky", "-F", "#{window_index}",
                    check=False).split()
        if len(left) <= 1:
            continue
        tmux("kill-window", "-t", f"sticky:{index}", check=False)


@pytest.fixture
def close_windows(tmux):
    """Kill whatever windows a test opened, however it ends."""
    panes = []
    yield panes
    shut(tmux, panes)


@pytest.fixture(scope="class")
def class_windows(tmux):
    """The same, for a tab a whole class of tests shares."""
    panes = []
    yield panes
    shut(tmux, panes)


@pytest.fixture(scope="class")
def class_project(tmp_path_factory):
    path = tmp_path_factory.mktemp("shared-project")
    return str(path)


@pytest.fixture(scope="class")
def attached_client(tmux, server):
    """A real client of a known size, so window_bigger means something."""
    subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                   capture_output=True)
    subprocess.run(["tmux", "-L", OUTER, "new-session", "-d",
                    "-x", "100", "-y", "30",
                    f"tmux -L {SOCKET} attach -t sticky"],
                   capture_output=True, check=True)
    time.sleep(0.8)
    yield
    subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                   capture_output=True)


def test_start_opens_a_claude_pane_and_a_sidebar(
        tmux, server, run_sticky, project, close_windows):
    """Catches anything cmd_start touches that has gone missing."""
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(1.0)
    roles = tmux("list-panes", "-a", "-F", "#{pane_id} #{@sticky_role}")
    assert f"{pane} claude" in roles
    assert "sidebar" in roles


def test_a_second_tab_on_one_project_starts_empty(
        tmux, server, run_sticky, project, close_windows, notes_of):
    """Notes belong to the conversation, not to the directory it runs in."""
    def store_of(pane):
        return tmux("display-message", "-p", "-t", pane,
                    "#{@sticky_store}").strip()

    claude = fake_claude(project)
    first = run_sticky("start", project, "--detach",
                       "--agent-cmd", claude).strip()
    close_windows.append(first)
    time.sleep(0.8)
    run_sticky("add", "--pane", first, "--note", "mine alone",
               "--text", "a quoted line")

    second = run_sticky("start", project, "--detach",
                        "--agent-cmd", claude).strip()
    close_windows.append(second)
    time.sleep(0.8)

    assert store_of(first) and store_of(first) != store_of(second)
    assert [n["note"] for n in notes_of(project, store_of(first))] == \
        ["mine alone"]
    assert notes_of(project, store_of(second)) == []


def test_a_note_from_a_plain_shell_joins_the_tab_that_is_open(
        tmux, server, run_sticky, project, close_windows, notes_of):
    """`echo ... | sticky-chat add` has no pane to say which conversation.

    Notes belong to a conversation now, so the project's own store is a place
    no sidebar is looking: the note would exist and be unsendable. It goes to
    whichever tab is open on the project instead.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    store = tmux("display-message", "-p", "-t", pane,
                 "#{@sticky_store}").strip()

    run_sticky("add", "--project", project, "--note", "from a plain shell",
               stdin="a line of output\n")

    assert [n["note"] for n in notes_of(project, store)] == \
        ["from a plain shell"]
    assert notes_of(project) == [], "it did not go to the project's own store"


def test_resuming_a_conversation_finds_its_notes_again(
        tmux, server, run_sticky, project, close_windows, notes_of):
    """The id names the store, so the same conversation reopens on it."""
    def store_of(pane):
        return tmux("display-message", "-p", "-t", pane,
                    "#{@sticky_store}").strip()

    claude = fake_claude(project)
    first = run_sticky("start", project, "--detach",
                       "--agent-cmd", claude).strip()
    close_windows.append(first)
    time.sleep(0.8)
    run_sticky("add", "--pane", first, "--note", "still open",
               "--text", "a quoted line")
    session = tmux("display-message", "-p", "-t", first,
                   "#{@sticky_session}").strip()

    again = run_sticky("start", project, "--detach", "--agent-cmd", claude,
                       "--", "--resume", session).strip()
    close_windows.append(again)
    time.sleep(0.8)

    assert store_of(again) == store_of(first)
    assert [n["note"] for n in notes_of(project, store_of(again))] == \
        ["still open"]


def test_the_new_tab_key_asks_for_a_project_and_completes_it(
        tmux, server, run_sticky, sticky_home, tmp_path, close_windows):
    """Drive the real C-g c, because only the real binding proves the wiring.

    The binding reaches sticky through run-shell, which is what expands the
    `#{}` in it; a popup named in the config instead would arrive with the
    socket and the pane still written as format strings, and the tab would
    never open.
    """
    home = tmp_path / "elsewhere"
    (home / "completed-project").mkdir(parents=True)
    claude = fake_claude(str(home))
    first = run_sticky("start", str(home), "--detach",
                       "--agent-cmd", claude).strip()
    close_windows.append(first)
    time.sleep(0.8)
    tmux("source-file", str(Path(sticky_home) / "tmux.conf"))
    index = tmux("display-message", "-p", "-t", first,
                 "#{window_index}").strip()

    subprocess.run(["tmux", "-L", OUTER, "kill-server"], capture_output=True)
    subprocess.run(["tmux", "-L", OUTER, "new-session", "-d",
                    "-x", "120", "-y", "40",
                    f"tmux -L {SOCKET} attach -t sticky"],
                   capture_output=True, check=True)
    try:
        time.sleep(1.0)
        tmux("switch-client", "-t", f"sticky:{index}")
        outer = subprocess.run(
            ["tmux", "-L", OUTER, "display-message", "-p", "-t", "0",
             "#{pane_id}"], capture_output=True, text=True).stdout.strip()

        def typed(*keys, wait=0.3):
            subprocess.run(["tmux", "-L", OUTER, "send-keys", "-t", outer,
                            *keys], capture_output=True)
            time.sleep(wait)

        typed("C-g", wait=0.2)
        typed("c", wait=1.5)
        typed("C-u")
        typed(str(home / "completed-pro"))
        typed("Tab", wait=0.5)          # one match, so Tab finishes the name
        typed("Enter", wait=3.0)

        opened = [line.split("\t") for line in tmux(
            "list-windows", "-t", "sticky",
            "-F", "#{window_index}\t#{window_name}").splitlines()]
        match = [i for i, name in opened if name == "completed-project"]
        assert match, f"the prompt opened no tab: {opened}"
        close_windows.extend(
            tmux("list-panes", "-t", f"sticky:{match[0]}",
                 "-F", "#{pane_id}").split())
        roles = tmux("list-panes", "-t", f"sticky:{match[0]}",
                     "-F", "#{@sticky_role}").split()
        assert sorted(roles) == ["claude", "sidebar"]
    finally:
        subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                       capture_output=True)


def test_leaving_claude_takes_the_tab_with_it(
        tmux, server, run_sticky, project, close_windows):
    """Once Claude has gone the sidebar has nothing left to annotate.

    It notices by asking tmux whether its pane is still there - and from
    tmux 3.8 that question is answered without an error for a pane that has
    gone, so a check reading only the exit status leaves the sidebar sitting
    there for ever.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", dying_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    sidebar = tmux("display-message", "-p", "-t", pane,
                   "#{@sticky_partner}").strip()
    assert sidebar, "no sidebar was opened"

    deadline = time.time() + 10
    while time.time() < deadline:
        if sidebar not in tmux("list-panes", "-a", "-F", "#{pane_id}").split():
            return
        time.sleep(0.3)
    pytest.fail("the sidebar outlived Claude")


def test_sweep_closes_a_sidebar_that_lost_its_claude(
        tmux, server, run_sticky, close_windows):
    """What the pane-exited hook calls.

    Built by hand rather than by killing a real tab, so that what closes the
    pane is unambiguously the sweep and not the sidebar noticing for itself.
    """
    orphan = tmux("new-window", "-d", "-P", "-F", "#{pane_id}",
                  "-t", "sticky:", "sleep 300").strip()
    close_windows.append(orphan)
    partner = tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                   "-t", orphan, "sleep 300").strip()
    keep = tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                "-t", orphan, "sleep 300").strip()
    tmux("set-option", "-p", "-t", orphan, "@sticky_role", "sidebar")
    tmux("set-option", "-p", "-t", orphan, "@sticky_partner", "%999")
    # Partnered to a pane that stays, so that closing the orphan cannot take
    # this one with it: a sidebar is only ever partnered to a Claude pane.
    tmux("set-option", "-p", "-t", keep, "@sticky_role", "sidebar")
    tmux("set-option", "-p", "-t", keep, "@sticky_partner", partner)

    run_sticky("sweep", "--quiet")
    time.sleep(0.4)
    live = tmux("list-panes", "-a", "-F", "#{pane_id}").split()
    assert orphan not in live, "the sidebar with no partner was left open"
    assert keep in live, "a sidebar whose partner is alive was closed"


def test_a_claude_that_will_not_start_is_said_so(
        tmux, server, run_sticky, project):
    """Rather than a sidebar alone in a window, with nothing to explain it."""
    before = set(tmux("list-panes", "-a", "-F", "#{pane_id}").split())
    with pytest.raises(RuntimeError, match="exited immediately"):
        run_sticky("start", project, "--detach", "--agent-cmd", "/bin/false")
    time.sleep(0.5)
    after = set(tmux("list-panes", "-a", "-F", "#{pane_id}").split())
    assert not after - before, "a sidebar was left behind"


def test_output_wakes_the_sidebar(
        tmux, server, run_sticky, sticky_home, project, close_windows):
    """There is no heartbeat left, so this is the only thing that redraws.

    The agent prints, tmux's `pane-activity` hook sends one byte to the pane
    beside it, and the sidebar - sitting in `select` with nothing on a clock
    - goes round the loop.

    Which needs a tmux that has `pane-activity`, and that is master, after
    3.7c. Where it is missing the sidebar falls back to looking every
    `SIDEBAR_TICK` and only redraws when what it would draw has changed -
    so the timestamp below never moves, and there is nothing here to test.
    That is not a failure, it is the fallback, and CI runs on released tmux.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    if tmux("show-options", "-gqv", "@sticky_wake").strip() != "1":
        pytest.skip("this tmux has no pane-activity; the sidebar polls "
                    "instead, and a poll only redraws a changed frame")
    tmux("source-file", str(Path(sticky_home) / "tmux.conf"))
    run_sticky("add", "--pane", pane, "--note", "a note to place",
               "--text", "hello from claude")
    time.sleep(1.2)

    # Every draw rewrites this, and a wake forces one whether the frame
    # changed or not, so its timestamp is the loop going round.
    rows = Path(tmux("display-message", "-p", "-t", pane,
                     "#{@sticky_store}").strip()) / "sidebar-rows.json"
    was = rows.stat().st_mtime
    time.sleep(1.0)
    assert rows.stat().st_mtime == was, "it redrew with nothing happening"

    tmux("respawn-pane", "-k", "-t", pane,
         "sh -c 'printf \"quite different output\\n\"; sleep 300'")
    deadline = time.time() + 5
    while time.time() < deadline:
        if rows.stat().st_mtime != was:
            return
        time.sleep(0.02)
    pytest.fail("the sidebar never noticed that Claude had printed")


def test_the_note_prompt_has_a_cursor_you_can_move(
        tmux, server, run_sticky, project, close_windows):
    """Left, Home and End inside the note popup, driven through a real client.

    The popup reads raw bytes, so arrow keys arrive as escape sequences that
    used to be read and thrown away: everything you typed landed at the end.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    store = tmux("display-message", "-p", "-t", pane,
                 "#{@sticky_store}").strip()
    index = tmux("display-message", "-p", "-t", pane,
                 "#{window_index}").strip()

    subprocess.run(["tmux", "-L", OUTER, "kill-server"], capture_output=True)
    subprocess.run(["tmux", "-L", OUTER, "new-session", "-d",
                    "-x", "120", "-y", "40",
                    f"tmux -L {SOCKET} attach -t sticky"],
                   capture_output=True, check=True)
    try:
        time.sleep(1.0)
        tmux("switch-client", "-t", f"sticky:{index}")
        client = tmux("list-clients", "-F", "#{client_name}").split()[0]
        outer = subprocess.run(
            ["tmux", "-L", OUTER, "display-message", "-p", "-t", "0",
             "#{pane_id}"], capture_output=True, text=True).stdout.strip()

        popup = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "--socket", SOCKET, "add",
             "--pane", pane, "--client", client, "--text", "hello from claude",
             "--sy", "0", "--ey", "0", "--hsize", "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "STICKY_HOME": str(Path(store).parents[1])})
        time.sleep(1.5)

        def typed(*keys, wait=0.12):
            subprocess.run(["tmux", "-L", OUTER, "send-keys", "-t", outer,
                            *keys], capture_output=True)
            time.sleep(wait)

        typed("hello world")
        for _ in range(5):                    # back over "world"
            typed("Left", wait=0.05)
        typed("BIG ")
        typed("Home")
        typed(">> ")
        typed("End")
        typed("!")
        typed("Enter", wait=1.5)
        popup.wait(timeout=10)

        notes = json.loads((Path(store) / "notes.json").read_text())["notes"]
        assert [n["note"] for n in notes] == [">> hello BIG world!"]
    finally:
        subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                       capture_output=True)


def test_clicking_away_from_the_prompt_keeps_what_was_typed(
        tmux, server, run_sticky, project, close_windows):
    """The reason the prompt is a floating pane and not a popup.

    tmux hands a popup every mouse event and drops the ones landing outside
    it, so nothing downstream can tell you clicked away. A floating pane is
    a pane: clicking off it changes the active pane, `window-pane-changed`
    says so, and the sweep closes it - which is a SIGHUP to the prompt, and
    that is what saves the note.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    store = Path(tmux("display-message", "-p", "-t", pane,
                      "#{@sticky_store}").strip())

    subprocess.run(["tmux", "-L", OUTER, "kill-server"], capture_output=True)
    subprocess.run(["tmux", "-L", OUTER, "new-session", "-d",
                    "-x", "120", "-y", "40",
                    f"tmux -L {SOCKET} attach -t sticky"],
                   capture_output=True, check=True)
    try:
        time.sleep(1.0)
        tmux("switch-client", "-t", tmux("display-message", "-p", "-t", pane,
                                         "#{session_name}:#{window_index}"
                                         ).strip())
        outer = subprocess.run(
            ["tmux", "-L", OUTER, "display-message", "-p", "-t", "0",
             "#{pane_id}"], capture_output=True, text=True).stdout.strip()

        run_sticky("add", "--pane", pane, "--text", "hello from claude",
                   "--sy", "0", "--ey", "0", "--hsize", "0")
        time.sleep(1.2)
        prompt = [p for p in tmux("list-panes", "-a", "-F",
                                  "#{pane_id}\t#{@sticky_role}").splitlines()
                  if p.endswith("\tnote")]
        assert prompt, "no prompt pane was opened"
        prompt = prompt[0].split("\t")[0]
        assert tmux("display-message", "-p", "-t", prompt,
                    "#{pane_floating_flag}").strip() == "1"

        subprocess.run(["tmux", "-L", OUTER, "send-keys", "-t", outer,
                        "half-written thought"], capture_output=True)
        time.sleep(0.5)

        tmux("select-pane", "-t", pane)      # as clicking on Claude would
        run_sticky("sweep", "--quiet")
        time.sleep(0.8)

        assert prompt not in tmux("list-panes", "-a", "-F",
                                  "#{pane_id}").split()
        notes = json.loads((store / "notes.json").read_text())["notes"]
        assert [n["note"] for n in notes] == ["half-written thought"]
    finally:
        subprocess.run(["tmux", "-L", OUTER, "kill-server"],
                       capture_output=True)


def test_the_click_that_closes_a_prompt_does_nothing_else(
        tmux, server, run_sticky, project, close_windows):
    """One click, one thing. Clicking the [x] of another note to get rid of
    a prompt used to finish your note and strike that one out at once."""
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    store = Path(tmux("display-message", "-p", "-t", pane,
                      "#{@sticky_store}").strip())
    sidebar = tmux("display-message", "-p", "-t", pane,
                   "#{@sticky_partner}").strip()
    run_sticky("add", "--pane", pane, "--note", "a note with a button",
               "--text", "hello from claude")
    time.sleep(1.0)

    button = next(hit for hit in
                  json.loads((store / "sidebar-rows.json").read_text())["hits"]
                  if hit["x"] < 34)

    def struck():
        notes = json.loads((store / "notes.json").read_text())["notes"]
        return [bool(n.get("deleted")) for n in notes]

    def press():
        run_sticky("click", "--pane", sidebar, "--y", str(button["row"]),
                   "--x", str(button["x"]))
        time.sleep(0.4)

    press()
    assert struck() == [True], "the button does not work at all"
    press()
    assert struck() == [False], "and does not toggle back"

    # Now with a prompt open, which is the case that used to do both.
    run_sticky("add", "--pane", pane, "--text", "hello from claude")
    time.sleep(1.2)
    assert "note" in tmux("list-panes", "-t", pane,
                          "-F", "#{@sticky_role}").split()
    press()
    assert struck()[0] is False, "the click that dismissed also struck a note"


def test_a_note_still_being_typed_is_not_counted_as_pending(
        tmux, server, run_sticky, project, close_windows):
    """Opening a prompt writes the note before you have said anything, so
    for as long as you are typing there is a note with no text in the store.
    Nothing can send it and it says nothing on screen, so it should not be
    drawn or counted - least of all as one of the notes a send will take."""
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    store = Path(tmux("display-message", "-p", "-t", pane,
                      "#{@sticky_store}").strip())
    sidebar = tmux("display-message", "-p", "-t", pane,
                   "#{@sticky_partner}").strip()

    run_sticky("add", "--pane", pane, "--text", "hello from claude")
    time.sleep(1.5)
    notes = json.loads((store / "notes.json").read_text())["notes"]
    assert [n["note"] for n in notes] == [""], "the placeholder should exist"

    drawn = tmux("capture-pane", "-p", "-t", sidebar)
    assert "(no text)" not in drawn
    assert "1 pending" not in drawn, "counted a note that cannot be sent"


class TestForking:
    @pytest.fixture(scope="class")
    def forked(self, tmux, server, run_sticky, class_project, class_windows,
               notes_of):
        project, close_windows = class_project, class_windows
        origin = run_sticky(
            "start", project, "--detach", "--agent-cmd",
            fake_claude(project, "printf 'args: %s\\n' \"$*\"")).strip()
        close_windows.append(origin)
        time.sleep(0.8)
        run_sticky("add", "--pane", origin, "--note", "carry me",
                   "--text", "a quoted line")
        run_sticky("add", "--pane", origin, "--note", "old news",
                   "--text", "another line")
        origin_dir = tmux("display-message", "-p", "-t", origin,
                          "#{@sticky_store}").strip()
        pane, directory = run_sticky("fork", "--pane", origin).strip().split()
        close_windows.append(pane)
        time.sleep(1.0)
        return {"origin": origin, "origin_dir": origin_dir, "pane": pane,
                "dir": directory, "project": project}

    def test_it_opens_its_own_tab(self, tmux, forked):
        name = tmux("display-message", "-p", "-t", forked["pane"],
                    "#{window_name}").strip()
        assert name.endswith("-fork")

    def test_it_repeats_the_original_command_line(self, tmux, forked):
        started = tmux("display-message", "-p", "-t", forked["pane"],
                       "#{pane_start_command}")
        assert "--continue --fork-session" in started

    def test_it_is_as_tall_as_the_tab_it_came_from(self, tmux, forked):
        def height(pane):
            return tmux("display-message", "-p", "-t", pane,
                        "#{window_height}").strip()

        assert height(forked["pane"]) == height(forked["origin"])

    def test_it_primes_its_rows_the_same_way(self, tmux, forked):
        def prologue(pane):
            return tmux("display-message", "-p", "-t", pane,
                        "#{pane_start_command}").count("awk")

        assert prologue(forked["pane"]) == prologue(forked["origin"])

    def test_it_carries_the_pending_notes(self, forked, notes_of):
        carried = notes_of(forked["project"], forked["dir"])
        assert sorted(n["note"] for n in carried) == ["carry me", "old news"]

    def test_a_note_taken_there_lands_in_its_own_store(
            self, run_sticky, forked, notes_of):
        # Reading one store while writing another is invisible until a note
        # you can see refuses to send.
        run_sticky("add", "--pane", forked["pane"], "--note",
                   "taken in the fork", "--text", "a line in the fork")
        here = [n["note"] for n in notes_of(forked["project"], forked["dir"])]
        there = [n["note"]
                 for n in notes_of(forked["project"], forked["origin_dir"])]
        assert "taken in the fork" in here
        assert "taken in the fork" not in there

    def test_its_sidebar_reads_that_store_too(self, forked):
        assert (Path(forked["dir"]) / "sidebar-rows.json").exists()

    def test_committing_there_leaves_the_original_alone(
            self, run_sticky, forked, notes_of):
        run_sticky("commit", "--no-paste", "--pane", forked["pane"])
        states = {n["status"]
                  for n in notes_of(forked["project"], forked["origin_dir"])}
        assert states == {"pending"}


class TestVirtualRows:
    """Claude repaints the bottom of its frame in place, so the window is made
    taller than the terminal and tmux pans a viewport over it."""

    @pytest.fixture(scope="class")
    def tall(self, tmux, run_sticky, class_project, class_windows,
             attached_client):
        project, close_windows = class_project, class_windows
        body = ("i=1; while [ $i -le 60 ]; do printf '  body-%02d\\n' $i; "
                "i=$((i+1)); done")
        pane = run_sticky("start", project, "--detach",
                          "--virtual-rows", "200",
                          "--agent-cmd", fake_claude(project, body)).strip()
        close_windows.append(pane)
        time.sleep(1.2)
        tmux("select-window", "-t", tmux(
            "display-message", "-p", "-t", pane,
            "#{session_name}:#{window_index}").strip())
        time.sleep(1.2)
        fields = tmux("display-message", "-p", "-t", pane,
                      "#{window_height}\t#{window_bigger}\t#{window_offset_y}"
                      "\t#{cursor_y}\t#{client_height}").strip().split("\t")
        return {"pane": pane, "project": project,
                "height": int(fields[0]), "bigger": fields[1],
                "offset": int(fields[2]), "cursor": int(fields[3]),
                "area": int(fields[4]) - 1}       # less the status line

    def test_the_window_is_taller_than_the_client(self, tall):
        assert tall["height"] == 200
        assert tall["bigger"] == "1"

    def test_the_viewport_is_pinned_to_the_bottom(self, tall):
        assert tall["offset"] == tall["height"] - tall["area"]

    def test_the_pane_starts_at_the_bottom_not_the_top(self, tall):
        assert tall["cursor"] == tall["height"] - 1

    def test_a_note_lands_on_its_own_row_and_follows_a_scroll(
            self, tmux, run_sticky, tall):
        raw = tmux("display-message", "-p", "-t", tall["pane"],
                   "#{history_size}\t#{pane_height}").strip().split("\t")
        history, height = int(raw[0]), int(raw[1])
        rows = tmux("capture-pane", "-p", "-t", tall["pane"], "-S", "0",
                    "-E", str(height - 1)).rstrip("\n").split("\n")
        index = next(i for i, r in enumerate(rows) if "body-30" in r)
        text = rows[index].rstrip()
        run_sticky("add", "--pane", tall["pane"], "--project", tall["project"],
                   "--note", "tall window", "--sy", str(history + index),
                   "--sx", "0", "--ey", str(history + index),
                   "--ex", str(len(text) - 1), stdin=text)

        placed = json.loads(run_sticky("place", "--pane", tall["pane"],
                                       "--project", tall["project"]))
        assert placed[0]["match"] == "exact" and placed[0]["row"] == index

        tmux("copy-mode", "-t", tall["pane"])
        tmux("send-keys", "-t", tall["pane"], "-X", "-N", "5", "scroll-up")
        time.sleep(0.4)
        try:
            placed = json.loads(run_sticky("place", "--pane", tall["pane"],
                                           "--project", tall["project"]))
            assert placed[0]["row"] == index + 5
        finally:
            tmux("send-keys", "-t", tall["pane"], "-X", "cancel", check=False)

    def test_virtual_rows_0_keeps_the_terminal_s_size(
            self, tmux, run_sticky, tall, tmp_path, close_windows):
        # Its own tab, so the shared one is left as the other tests found it.
        plain_project = tmp_path / "plain"
        plain_project.mkdir()
        pane = run_sticky("start", str(plain_project), "--detach",
                          "--virtual-rows", "0", "--agent-cmd",
                          fake_claude(plain_project)).strip()
        close_windows.append(pane)
        time.sleep(1.0)
        got = tmux("display-message", "-p", "-t", pane,
                   "#{window_height}\t#{@sticky_virtual_rows}").strip()
        assert got == str(tall["area"])


@pytest.mark.integration
class TestAnotherAgent:
    """A tab on a profile that cannot name a conversation still works.

    Everything that does not depend on which agent is in the pane - the
    notes, the sidebar, the sending - is unchanged. The two features that do
    need a conversation id say so instead of writing a flag the agent has
    never heard of.
    """

    @pytest.fixture(scope="class")
    def plain(self, tmux, server, run_sticky, class_project, class_windows):
        project = class_project
        pane = run_sticky("start", project, "--detach", "--agent", "generic",
                          "--agent-cmd", fake_claude(project)).strip()
        class_windows.append(pane)
        time.sleep(1.0)
        store = tmux("display-message", "-p", "-t", pane,
                     "#{@sticky_store}").strip()
        return {"pane": pane, "project": project, "store": store}

    def test_the_profile_is_recorded_on_the_pane(self, tmux, plain):
        assert tmux("display-message", "-p", "-t", plain["pane"],
                    "#{@sticky_agent}").strip() == "generic"

    def test_no_flag_the_agent_has_never_heard_of_is_added(self, tmux, plain):
        line = tmux("display-message", "-p", "-t", plain["pane"],
                    "#{@sticky_agent_cmd}").strip()
        assert line.endswith("fake-claude"), line
        assert "--session-id" not in line and "--disallowedTools" not in line

    def test_it_takes_notes_like_any_other_tab(
            self, run_sticky, plain, notes_of):
        run_sticky("add", "--pane", plain["pane"], "--note", "still works",
                   "--text", "hello from claude")
        assert "still works" in [n["note"] for n
                                 in notes_of(plain["project"], plain["store"])]

    def test_forking_declines_rather_than_opening_an_empty_tab(
            self, run_sticky, plain):
        """A fork of a conversation that cannot be branched is a new tab
        wearing the notes of one that is still talking."""
        with pytest.raises(RuntimeError) as raised:
            run_sticky("fork", "--pane", plain["pane"])
        assert "cannot branch a conversation" in str(raised.value)

    def test_resume_puts_the_tab_back_without_inventing_a_flag(
            self, tmux, run_sticky, plain, tmp_path_factory, close_windows):
        """Its own STICKY_HOME, so `resume --all` takes this one record.

        The tab comes back - project, notes, sidebar - and the command line
        it comes back on is the one it went away on, with no --resume that
        the agent would have refused.
        """
        home = tmp_path_factory.mktemp("resume-home")
        (home / "windows").mkdir()
        command = fake_claude(plain["project"])
        (home / "windows" / "tab-generic.json").write_text(json.dumps({
            "session": "tab-generic", "project": plain["project"],
            "command": command, "store": plain["store"], "agent": "generic",
            "continued": True, "name": "resumed-generic",
            "last_seen": time.time(), "created": time.time()}))

        said = run_sticky("resume", "--all", "--detach", home=home)
        assert "the conversation starts fresh" in said
        rows = [line.split("\t") for line in tmux(
            "list-panes", "-a", "-F", "#{pane_id}\t#{window_name}\t"
            "#{@sticky_role}\t#{@sticky_agent}\t"
            "#{@sticky_agent_cmd}").splitlines()]
        back = [r for r in rows
                if r[1] == "resumed-generic" and r[2] == "claude"]
        assert len(back) == 1, rows
        close_windows.append(back[0][0])
        assert back[0][3] == "generic"
        assert back[0][4] == command

    def test_claude_is_still_what_a_tab_gets_by_default(
            self, tmux, run_sticky, project, close_windows, sticky_home):
        pane = run_sticky("start", project, "--detach",
                          "--agent-cmd", fake_claude(project)).strip()
        close_windows.append(pane)
        time.sleep(0.8)
        session, agent = tmux(
            "display-message", "-p", "-t", pane,
            "#{@sticky_session}\t#{@sticky_agent}").strip().split("\t")
        assert agent == "claude"
        record = json.loads(
            (Path(sticky_home) / "windows" / f"{session}.json").read_text())
        assert record["agent"] == "claude"


@pytest.mark.integration
class TestRememberingTabs:
    def test_starting_a_tab_records_how_to_reopen_it(
            self, tmux, run_sticky, project, close_windows, sticky_home):
        pane = run_sticky(
            "start", project, "--detach", "--agent-cmd",
            fake_claude(project)).strip()
        close_windows.append(pane)
        time.sleep(0.8)
        session = tmux("display-message", "-p", "-t", pane,
                       "#{@sticky_session}").strip()
        assert session
        path = Path(sticky_home) / "windows" / f"{session}.json"
        record = json.loads(path.read_text())
        assert record["project"] == project
        assert f"--session-id {session}" in record["command"]
        assert record["last_seen"] >= record["created"]



@pytest.mark.integration
class TestReopeningOneRememberedTab:
    """`C-g o` and `sticky-chat reopen`: one closed tab, chosen and put back.

    Driven through the CLI rather than through the key, because what the key
    adds - a floating pane with a terminal in it - is the same pane the
    new-tab prompt opens and is covered where that is. What is proved here is
    the half a fake server cannot: that the picker's own answer about which
    tabs are on screen is one the real tmux gives.
    """

    def remember(self, home, project, command, **fields):
        """One record in a home of this test's own, so the list is known."""
        windows = Path(home) / "windows"
        windows.mkdir(parents=True, exist_ok=True)
        record = {"session": "tab-picked", "project": project,
                  "command": command, "agent": "generic", "continued": True,
                  "name": "picked-again", "last_seen": time.time(),
                  "created": time.time(), **fields}
        (windows / f"{record['session']}.json").write_text(json.dumps(record))
        return record

    def test_the_number_you_type_opens_that_tab(
            self, tmux, run_sticky, project, close_windows, tmp_path_factory):
        home = tmp_path_factory.mktemp("reopen-home")
        command = fake_claude(project)
        self.remember(home, project, command)

        listed = run_sticky("reopen", "--detach", stdin="1\n", home=home)
        assert "picked-again" in listed, listed
        assert "generic" in listed and "a new conversation" in listed

        time.sleep(1.0)
        rows = [line.split("\t") for line in tmux(
            "list-panes", "-a", "-F", "#{pane_id}\t#{window_name}\t"
            "#{@sticky_role}\t#{@sticky_agent_cmd}").splitlines()]
        back = [r for r in rows if r[1] == "picked-again" and r[2] == "claude"]
        assert len(back) == 1, rows
        close_windows.append(back[0][0])
        assert back[0][3] == command, "reopened on the line it went away on"
        assert [r for r in rows if r[1] == "picked-again"
                and r[2] == "sidebar"], "the sidebar came back with it"

    def test_a_tab_that_is_already_open_is_not_offered(
            self, tmux, run_sticky, project, close_windows, tmp_path_factory):
        """Otherwise the picker would put a second tab on a live
        conversation. Which tabs are open is asked of the panes, and this is
        the test that the question tmux is really asked works."""
        pane = run_sticky("start", project, "--detach",
                          "--agent-cmd", fake_claude(project)).strip()
        close_windows.append(pane)
        time.sleep(0.8)
        session, store = tmux(
            "display-message", "-p", "-t", pane,
            "#{@sticky_session}\t#{@sticky_store}").strip().split("\t")

        home = tmp_path_factory.mktemp("open-already-home")
        self.remember(home, project, fake_claude(project), session=session,
                      store=store, name="already-open")
        before = len(tmux("list-windows", "-t", "sticky", "-F",
                          "#{window_id}").split())

        said = run_sticky("reopen", "--detach", stdin="1\n", home=home)
        time.sleep(0.5)
        assert said.strip() == "", "nothing was listed and nothing was opened"
        after = tmux("list-windows", "-t", "sticky", "-F",
                     "#{window_id}\t#{window_name}").splitlines()
        assert len(after) == before, after
        assert not [w for w in after if "already-open" in w]
