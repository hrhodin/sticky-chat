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


def test_a_named_profile_is_not_overruled_by_the_tab_it_came_from(
        tmux, server, run_sticky, project, close_windows):
    """`C-g c` means "another one like this" and inherits the command line,
    which is how it keeps the flags. `C-g T` means "a shell, not whatever I
    am in" - and inheriting there gave a tab that said `shell` in its
    options and ran the agent it was opened beside.
    """
    first = run_sticky("start", project, "--detach", "--agent", "generic",
                       "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(first)
    time.sleep(0.8)

    same = run_sticky("new", project, "--detach", "--from", first).strip()
    close_windows.append(same)
    shell = run_sticky("new", project, "--detach", "--from", first,
                       "--agent", "shell").strip()
    close_windows.append(shell)
    time.sleep(0.8)

    def launched(pane):
        return tmux("display-message", "-p", "-t", pane,
                    "#{@sticky_agent_cmd}").strip()

    assert launched(same) == launched(first), "C-g c still copies the line"
    assert launched(shell) != launched(first), "a named profile starts itself"
    assert launched(shell).endswith("sh"), f"a shell, not {launched(shell)!r}"


def test_a_batch_can_be_sent_on_a_timer(
        tmux, server, run_sticky, project, close_windows):
    """An agent that has run out of turns until midnight is the case: write
    the notes now, and let them go when it can answer.

    The sidebar watches the clock, being the only part of sticky awake
    between one keypress and the next, so the deadline is a pane option
    rather than a sleeping process - it survives the sidebar restarting and
    dies with the tab.
    """
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", "cat").strip()
    close_windows.append(pane)
    time.sleep(0.8)
    run_sticky("add", "--pane", pane, "--note", "later, please",
               "--text", "some output")
    time.sleep(0.5)

    run_sticky("commit", "--at", "4s", "--pane", pane)
    assert tmux("show-options", "-pqv", "-t", pane,
                "@sticky_send_at").strip(), "the deadline is written down"

    deadline = time.time() + 12
    while time.time() < deadline:
        if not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip():
            break
        time.sleep(0.1)
    else:
        pytest.fail("the timer never fired")

    time.sleep(1.0)
    seen = tmux("capture-pane", "-p", "-t", pane)
    assert "later, please" in seen, "and the batch arrived in the pane"


def test_an_agent_out_of_turns_arms_itself(
        tmux, server, run_sticky, project, close_windows):
    """The message that says "try again at" is the moment to set a clock.

    The agent prints the notice and stops, which is the same silence that
    means "finished" - so the difference is told by what it printed, and
    only then. The tab shows a clock instead of its mark while it waits.
    """
    agent = Path(project) / "out-of-turns"
    agent.write_text("#!/bin/sh\nwhile read -r line; do\n"
                     "  printf \"You've hit your usage limit. "
                     "Try again at 11:59 PM.\\n\"\ndone\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    tmux("send-keys", "-t", pane, "a question", "Enter")

    # Generous, because the whole suite shares one server and the sidebar
    # walks every pane on it: what is being waited for is the quiet, not
    # the speed of the machine underneath it.
    deadline = time.time() + 25
    while time.time() < deadline:
        armed = tmux("show-options", "-pqv", "-t", pane,
                     "@sticky_send_at").strip()
        if armed:
            break
        time.sleep(0.1)
    else:
        pytest.fail("a notice about a limit should have set a clock")

    at, _, rest = armed.partition(":")
    _, _, saying = rest.partition(":")
    assert saying == "continue", f"and it should say so: {armed!r}"
    assert float(at) > time.time(), "at a time still to come"
    assert tmux("show-options", "-pqv", "-t", pane,
                "@sticky_continue_tries").strip() == "1", "counted"
    assert tmux("show-options", "-wqv", "-t", pane,
                "@sticky_waiting").strip() == "1", "and the tab says so"
    assert "usage limit" in tmux("show-options", "-pqv", "-t", pane,
                                 "@sticky_continue_saw"), "and what it read"


def test_a_new_sidebar_arms_from_what_is_already_on_screen(
        tmux, server, run_sticky, project, close_windows):
    """The notice does not have to arrive while anyone is watching.

    A sidebar that has just started has no idea whether the tab in front of
    it has been quiet for a second or since last night, so it treats the
    beginning as though the agent had just printed: the first quiet is
    looked at like any other, and a limit already on screen is found there.
    Which is what makes `reload` - and a sidebar closed and opened again -
    pick up an agent that ran out while nothing was running to notice.
    """
    agent = Path(project) / "out-of-turns-once"
    agent.write_text("#!/bin/sh\nwhile read -r line; do\n"
                     "  printf \"You've hit your usage limit. "
                     "Try again at 11:59 PM.\\n\"\ndone\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    tmux("send-keys", "-t", pane, "a question", "Enter")
    time.sleep(6)

    # Everything the first sidebar worked out, thrown away: the notice is
    # still the last thing on the pane, and nothing anywhere says so.
    tmux("set-option", "-p", "-u", "-t", pane, "@sticky_send_at")
    tmux("set-option", "-p", "-u", "-t", pane, "@sticky_continue_tries")
    tmux("set-option", "-w", "-u", "-t", pane, "@sticky_waiting")
    run_sticky("reload", "--quiet")

    deadline = time.time() + 25
    while time.time() < deadline:
        armed = tmux("show-options", "-pqv", "-t", pane,
                     "@sticky_send_at").strip()
        if armed:
            break
        time.sleep(0.1)
    else:
        pytest.fail("a sidebar should read the pane it is handed")
    assert armed.endswith(":continue"), f"and act on it: {armed!r}"
    assert tmux("show-options", "-wqv", "-t", pane,
                "@sticky_waiting").strip() == "1", "and the tab says so"


def test_a_turn_of_your_own_gives_the_count_back(
        tmux, server, run_sticky, project, close_windows):
    """Three attempts at a wall, not three attempts ever.

    The count is there to stop an agent poking all night at something
    waiting will not fix. Once somebody has asked the tab for something
    themselves and got an answer, it is not that any more, and a tab that
    ran out once last night should not spend the rest of its life one
    strike from the end.

    The same test pins the other half of it: the old notice is still on
    screen above the answer, and acting on that would set a clock for a
    limit that lifted hours ago.
    """
    agent = Path(project) / "out-of-turns-sometimes"
    agent.write_text(
        "#!/bin/sh\nwhile read -r line; do\n"
        "  case \"$line\" in\n"
        "    *limit*) printf \"You've hit your usage limit. "
        "Try again at 11:59 PM.\\n\" ;;\n"
        "    *) printf 'here you go\\n' ;;\n"
        "  esac\ndone\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    tmux("send-keys", "-t", pane, "mind the limit", "Enter")

    deadline = time.time() + 25
    while time.time() < deadline:
        if tmux("show-options", "-pqv", "-t", pane,
                "@sticky_send_at").strip():
            break
        time.sleep(0.1)
    else:
        pytest.fail("the notice should have set a clock")
    assert tmux("show-options", "-pqv", "-t", pane,
                "@sticky_continue_tries").strip() == "1"

    # Called off, the way `t` then `cancel` does, and then asked something
    # in person. The answer arrives under a notice that is still on screen.
    run_sticky("commit", "--at", "cancel", "--pane", pane)
    tmux("send-keys", "-t", pane, "hello", "Enter")
    time.sleep(8)

    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_continue_tries").strip(), "the three come back"
    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip(), "on a notice already dealt with"


def test_the_clock_can_be_called_off_by_clicking_it(
        tmux, server, run_sticky, sticky_home, project, close_windows):
    """The row is the button. Clicking it stands the clock down, clicking
    it again puts it back.

    The time is parked rather than thrown away: a clock you have turned off
    is one you may well want back, and the hour it was set for is not a
    thing anybody should have to remember. And off has to mean off - the
    sidebar arms a tab by reading it when it goes quiet, so without a guard
    it would find the same notice a minute later and start the whole thing
    again behind your back.
    """
    import glob
    import json

    agent = Path(project) / "out-of-turns-click"
    agent.write_text("#!/bin/sh\nwhile read -r line; do\n"
                     "  printf \"You've hit your usage limit. "
                     "Try again at 11:59 PM.\\n\"\ndone\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    tmux("send-keys", "-t", pane, "a question", "Enter")

    deadline = time.time() + 25
    while time.time() < deadline:
        armed = tmux("show-options", "-pqv", "-t", pane,
                     "@sticky_send_at").strip()
        if armed:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the notice should have set a clock")

    # By partner, not by being the first sidebar on the server: the whole
    # suite shares one, and every other test has left one standing.
    side = next(line.split()[0] for line in
                tmux("list-panes", "-a", "-F",
                     "#{pane_id} #{@sticky_role} #{@sticky_partner}"
                     ).splitlines()
                if line.split()[1:] == ["sidebar", pane])

    def clock_row():
        for path in glob.glob(f"{sticky_home}/**/sidebar-rows.json",
                              recursive=True):
            with open(path) as fh:
                rows = json.load(fh)
            if rows.get("pane") != side:
                continue
            for hit in rows.get("hits", []):
                if hit["id"] == "@clock":
                    return str(hit["row"])
        return None

    stop = time.time() + 10
    while time.time() < stop and clock_row() is None:
        time.sleep(0.2)
    row = clock_row()
    assert row is not None, "the clock row should be something to click"
    run_sticky("click", "--pane", side, "--y", row, "--x", "0")
    time.sleep(2)
    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip(), "clicked once, it stands down"
    assert tmux("show-options", "-pqv", "-t", pane,
                "@sticky_send_at_off").strip() == armed, "the time is kept"
    assert not tmux("show-options", "-wqv", "-t", pane,
                    "@sticky_waiting").strip(), "and the tab stops waiting"

    # Long enough for the tab to go quiet again under the same notice.
    time.sleep(6)
    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip(), "off stays off"

    run_sticky("click", "--pane", side, "--y", row, "--x", "0")
    time.sleep(2)
    assert tmux("show-options", "-pqv", "-t", pane,
                "@sticky_send_at").strip() == armed, "clicked again, it is back"
    assert tmux("show-options", "-wqv", "-t", pane,
                "@sticky_waiting").strip() == "1", "and the tab says so again"


def test_it_gives_up_after_three_attempts(
        tmux, server, run_sticky, project, close_windows):
    """Three and no more. If three have not got past it, something is wrong
    that waiting will not fix, and an agent left poking a wall all night is
    worse than one that stopped."""
    agent = Path(project) / "out-of-turns-always"
    agent.write_text("#!/bin/sh\nwhile read -r line; do\n"
                     "  printf \"You've hit your usage limit. "
                     "Try again at 11:59 PM.\\n\"\ndone\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    time.sleep(0.8)

    # Three already spent, and a sidebar that has read that off the pane:
    # the count lives there so a reload does not hand out three more.
    tmux("set-option", "-p", "-t", pane, "@sticky_continue_tries", "3")
    run_sticky("reload", "--quiet")
    time.sleep(1.5)

    tmux("send-keys", "-t", pane, "a question", "Enter")
    time.sleep(6)
    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip(), "it should have stopped"


def test_a_timer_can_be_called_off(
        tmux, server, run_sticky, project, close_windows):
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", "cat").strip()
    close_windows.append(pane)
    time.sleep(0.8)
    run_sticky("add", "--pane", pane, "--note", "not yet", "--text", "output")
    run_sticky("commit", "--at", "2h", "--pane", pane)
    assert tmux("show-options", "-pqv", "-t", pane, "@sticky_send_at").strip()
    run_sticky("commit", "--at", "cancel", "--pane", pane)
    assert not tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_send_at").strip()


def test_the_timer_key_offers_the_time_the_agent_printed(
        tmux, server, run_sticky, project, close_windows):
    """`t` in the sidebar, and where the time in its prompt comes from.

    The whole of the guess is that it is a default in a line you can edit:
    the pane said half past midnight, so that is what the prompt opens
    with, and the deadline is set by the Enter that follows it. A vendor
    that changes its wording leaves the prompt empty and you type the time,
    which is what you did before there was a key for this.

    The longest of the real wordings on purpose: it is wider than a pane
    with a sidebar beside it, so the word "limit" and the time it is about
    end up on different rows - and the first of those rows has been pushed
    into the history by the reflow that opening the sidebar caused.
    """
    when = time.localtime(time.time() + 86400)
    notice = ("You have hit your usage limit. You can try again at "
              + time.strftime("%b %d, %Y", when) + " 12:32 AM.")
    expect = time.strftime("%Y-%m-%d", when) + " 00:32"
    agent = fake_claude(project, body=f"printf '{notice}\\n'")
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", agent).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    run_sticky("add", "--pane", pane, "--note", "when you can",
               "--text", "some output")

    def panes(role):
        return [row.split("\t")[0] for row in tmux(
            "list-panes", "-t", pane, "-F",
            "#{pane_id}\t#{@sticky_role}").splitlines()
            if row.endswith(f"\t{role}")]

    def until(what, why):
        deadline = time.time() + 10
        while time.time() < deadline:
            answer = what()
            if answer:
                return answer
            time.sleep(0.1)
        pytest.fail(why)

    sidebar = until(lambda: panes("sidebar"), "no sidebar was opened")[0]
    # The help panel is showing until the first note exists, and any key
    # takes it away rather than acting - so wait until the note is drawn.
    until(lambda: "when you" in tmux("capture-pane", "-p", "-t", sidebar),
          "the sidebar never showed the note")

    tmux("send-keys", "-t", sidebar, "t")
    prompt = until(lambda: panes("note"), "t opened no prompt")[0]
    shown = until(lambda: ("send at>" in tmux("capture-pane", "-p", "-t",
                                              prompt)) and
                  tmux("capture-pane", "-p", "-t", prompt),
                  "the prompt never drew its line")
    assert f"send at> {expect}" in shown, \
        f"the guess was not offered: {shown!r}"

    tmux("send-keys", "-t", prompt, "Enter")
    due = until(lambda: tmux("show-options", "-pqv", "-t", pane,
                             "@sticky_send_at").strip(),
                "Enter set no deadline")
    at = float(due.partition(":")[0])
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) == expect


def test_a_tab_that_went_quiet_says_so(
        tmux, server, run_sticky, project, close_windows):
    """Printed, then silent: the nearest thing to "finished" that holds for
    every agent, since a bell is a thing an agent may or may not ring.

    The mark is a window option the status line reads. It is not set on the
    tab being looked at - there is nothing to tell somebody already
    watching - and looking at the tab clears it.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    time.sleep(0.8)
    window = tmux("display-message", "-p", "-t", pane, "#{window_id}").strip()

    def done():
        return tmux("show-options", "-wqv", "-t", pane, "@sticky_done").strip()

    # Printing is what `cat` does with anything typed at it, and unlike
    # respawning the pane it does not take the sidebar with it: a pane that
    # exits is a tab that gets swept.
    tmux("respawn-pane", "-k", "-t", pane, "cat")
    time.sleep(0.8)

    # Looked at, so nothing to say however much it prints.
    tmux("select-window", "-t", window)
    tmux("send-keys", "-t", pane, "working", "Enter")
    time.sleep(4)
    assert done() != "1", "the tab you are watching does not need telling"

    # Backgrounded, printing, then quiet.
    tmux("select-window", "-t", "sticky:0")   # the fixture's own window
    time.sleep(0.3)
    assert tmux("display-message", "-p", "-t", pane,
                "#{window_active}").strip() == "0", "the tab is in the background"
    tmux("send-keys", "-t", pane, "an answer", "Enter")
    deadline = time.time() + 8
    while time.time() < deadline and done() != "1":
        time.sleep(0.1)
    assert done() == "1", "a quiet tab in the background should say so"

    # And looking at it is what answers the question.
    tmux("select-window", "-t", window)
    time.sleep(0.6)
    assert done() != "1", "arriving at the tab clears the mark"


def test_a_repaint_is_not_an_answer(
        tmux, server, run_sticky, project, close_windows):
    """Bytes are not news.

    tmux fires `pane-activity` for everything an agent writes, and an agent
    that redraws its own input box writes a great deal that leaves the
    screen exactly as it was. Counted as output, each of those starts the
    clock that marks a tab finished, so tabs light up having done nothing -
    and the mark stops meaning anything. What the pane says is the only
    thing that can tell the two apart.
    """
    agent = Path(project) / "repainting"
    # Draws one line, then rewrites that same line every few seconds: the
    # gaps are wider than DONE_QUIET, so each redraw looks exactly like an
    # agent that printed something and stopped. Which is the shape of the
    # thing being guarded against - a tab lighting up between one glance
    # and the next with nothing new on it.
    agent.write_text("#!/bin/sh\nprintf 'starting up\\n'\nsleep 3\n"
                     "printf 'here you go\\n'\n"
                     "while :; do sleep 4; "
                     "printf '\\033[A\\rhere you go\\n'; done\n")
    agent.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "generic",
                      "--agent-cmd", str(agent)).strip()
    close_windows.append(pane)
    window = tmux("display-message", "-p", "-t", pane, "#{window_id}").strip()

    def done():
        return tmux("show-options", "-wqv", "-t", pane, "@sticky_done").strip()

    # The line it really did draw is real output, and marks the tab. (The
    # banner above it is not: whatever is on screen when a sidebar starts is
    # the baseline it compares against, never news in its own right.)
    deadline = time.time() + 15
    while time.time() < deadline and done() != "1":
        time.sleep(0.1)
    assert done() == "1", "the line it drew is an answer"

    # Read it, and go away again. From here on nothing changes on screen,
    # though the bytes never stop.
    tmux("select-window", "-t", window)
    time.sleep(1.0)
    assert done() != "1", "arriving at the tab clears the mark"
    tmux("select-window", "-t", "sticky:0")
    # Watched the whole way rather than looked at once at the end. Taken as
    # output, each redraw sets the mark and the next redraw clears it
    # again, so a single glance can land in either half of that and prove
    # nothing. What is being asserted is that it never lights up at all.
    seen = []
    until = time.time() + 12
    while time.time() < until:
        seen.append(done())
        time.sleep(0.25)
    assert "1" not in seen, (
        f"a redraw of the same screen is not an answer: "
        f"marked on {seen.count('1')} of {len(seen)} looks")


def test_a_reload_is_not_news(
        tmux, server, run_sticky, project, close_windows):
    """Restarting the sidebars is not the tabs saying something.

    A sidebar that has just started cannot tell whether the tab in front of
    it has been quiet for a second or since last night, so it treats the
    first look as though the agent had just printed - which is what lets a
    limit notice already on screen be found. The mark must not follow it
    there: `reload` restarts every sidebar at once, so one keystroke would
    otherwise light up every tab in the list at the same moment, and a mark
    that means "I was restarted" means nothing at all.
    """
    pane = run_sticky("start", project, "--detach",
                      "--agent-cmd", fake_claude(project)).strip()
    close_windows.append(pane)
    window = tmux("display-message", "-p", "-t", pane, "#{window_id}").strip()

    def done():
        return tmux("show-options", "-wqv", "-t", pane, "@sticky_done").strip()

    # Read, then left alone. Nothing is outstanding and nothing is printed
    # in it from here on.
    tmux("select-window", "-t", window)
    time.sleep(1.5)
    tmux("select-window", "-t", "sticky:0")
    time.sleep(4)
    assert done() != "1", "nothing has happened yet"

    run_sticky("reload", "--quiet")
    time.sleep(8)
    assert done() != "1", "and a reload is still nothing happening"

    # But the tab has not gone deaf: what it says after the reload counts.
    tmux("respawn-pane", "-k", "-t", pane, "cat")
    time.sleep(1)
    tmux("send-keys", "-t", pane, "an answer", "Enter")
    deadline = time.time() + 10
    while time.time() < deadline and done() != "1":
        time.sleep(0.1)
    assert done() == "1", "a restarted sidebar still hears the tab"


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
