"""The command line, and the tmux config it generates.

The generated config is the only thing the mouse ever reaches, so a binding
that disagrees with the CLI is a feature nobody can use.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time

import pytest

from conftest import LAUNCHER


def test_virtual_rows_are_off_unless_asked_for(sticky, monkeypatch):
    # A window bigger than its client is drawn wrong often enough that it is
    # opted into, tab by tab, rather than handed to everybody.
    from sticky_chat import config

    monkeypatch.delenv("STICKY_VIRTUAL_ROWS", raising=False)
    assert config.default_virtual_rows() == 0
    assert sticky.build_parser().parse_args(["start"]).virtual_rows == 0


def test_the_environment_can_turn_virtual_rows_on(sticky, monkeypatch):
    from sticky_chat import config

    for value, expected in (("200", 200), ("0", 0), ("-5", 0), ("what", 0)):
        monkeypatch.setenv("STICKY_VIRTUAL_ROWS", value)
        assert config.default_virtual_rows() == expected, value


def test_asking_for_virtual_rows_turns_them_on(sticky):
    args = sticky.build_parser().parse_args(["start", "--virtual-rows", "150"])
    assert args.virtual_rows == 150


def test_a_tall_tab_has_its_own_binding(sticky):
    """C-g C is the only way in from the keyboard, so it has to carry rows."""
    config = sticky.CONFIG_TEMPLATE
    assert "bind C run-shell" in config
    assert "--virtual-rows @ROWS@" in config


def test_new_output_wakes_the_sidebar_without_starting_anything(sticky):
    """The sidebar has no heartbeat, so tmux has to say when Claude printed.

    `send-keys` is the whole notification - no `run-shell`, so no process
    per burst of output - and its target has to be `+`, the other pane of
    the two: a target is not format-expanded, so `#{@sticky_partner}` would
    arrive as those very characters and address nothing.
    """
    assert "send-keys -t +" in sticky.ACTIVITY_HOOK
    assert "run-shell" not in sticky.ACTIVITY_HOOK, "a fork per burst is the thing"
    assert "#{@sticky_partner}" not in sticky.ACTIVITY_HOOK


def test_the_activity_hook_is_not_written_into_the_config(sticky):
    """`pane-activity` exists only in tmux after 3.7c, and an unknown hook
    name does not fail quietly: `source-file` gives up on the whole file, so
    a released tmux would lose every binding and `reload` with them."""
    commands = [ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [ln for ln in commands if "pane-activity" in ln]
    assert "install_hooks" in dir(sticky), "it is set from Python instead"


def test_v_begins_a_selection(sticky):
    """The docs, the sidebar's help and every vi user say `v` selects.

    tmux does not: in copy-mode-vi `v` is rectangle-toggle and the key that
    begins a selection is Space - which the typing table hands back to
    Claude, because a space bar should type a space. Without this binding
    there is no keyboard way to select anything at all.
    """
    config = sticky.CONFIG_TEMPLATE
    for table in ("copy-mode-vi", "copy-mode"):
        assert re.search(rf"^bind -T {table} +v send -X begin-selection$",
                         config, re.M), f"{table} has no way to select"
    assert "v" in sticky.RESERVED_IN_COPY_MODE, "or the typing table takes it"
    assert " " not in sticky.RESERVED_IN_COPY_MODE, "space is for typing"


class TestTypingOutOfAScroll:
    """Scrolling back puts the pane in copy mode, where every letter is a
    motion. What you type next went nowhere until you knew to press q."""

    def test_an_ordinary_letter_leaves_and_is_delivered(self, sticky):
        lines = sticky.typing_keys().splitlines()
        assert "bind -T copy-mode 'h' { send -X cancel ; send-keys }" in lines
        assert "bind -T copy-mode-vi 'h' { send -X cancel ; send-keys }" in lines

    def test_both_tables_because_mode_keys_follows_your_editor(self, sticky):
        lines = sticky.typing_keys().splitlines()
        assert sum(1 for ln in lines if " copy-mode " in ln) == \
            sum(1 for ln in lines if " copy-mode-vi " in ln)

    def test_stickys_own_keys_keep_their_meaning(self, sticky):
        lines = sticky.typing_keys()
        for key in sticky.RESERVED_IN_COPY_MODE:
            assert f" '{key}' " not in lines, f"{key} should still be itself"

    @pytest.mark.parametrize("key", ["~", "'", '"', "#", ";", "$", "{", " "])
    def test_the_awkward_characters_are_bound_as_themselves(self, sticky, key):
        """tmux expands a leading ~ inside double quotes, so a naive quoting
        binds the home directory rather than the key - and says so only when
        the config is sourced, by which time every later line is lost."""
        quoted = f'"{key}"' if key == "'" else f"'{key}'"
        assert f"bind -T copy-mode {quoted} " in sticky.typing_keys()


class TestFindingItself:
    """`self_path` goes into the tmux config and into every key binding, so
    it has to name something a shell can run."""

    def test_a_module_run_finds_the_console_script(self, sticky, monkeypatch):
        """`python -m sticky_chat` puts a module file in argv[0]. It is
        installed 644 with no shebang, so a binding made from it does
        nothing at all - silently, which is the worst kind."""
        monkeypatch.setattr(sys, "argv", ["/somewhere/sticky_chat/__main__.py"])
        found = sticky.self_path()
        assert not found.endswith("__main__.py"), found
        assert os.access(found, os.X_OK), f"{found} is not runnable"

    def test_an_ordinary_run_keeps_the_path_it_was_given(self, sticky,
                                                         monkeypatch, tmp_path):
        launcher = tmp_path / "sticky-chat"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(launcher)])
        assert sticky.self_path() == str(launcher)


def test_the_focused_sidebar_is_given_a_ground_of_its_own(sticky):
    """Which pane the keyboard is in is otherwise one column of border.

    `window-active-style` on the sidebar's own pane is what paints it, so
    tmux does the work - empty rows included - exactly while that pane is
    the active one, and Claude's side is never touched. A grey rather than
    something brighter or darker, so it reads as a difference on a dark
    ground and on a light one.
    """
    assert sticky.FOCUS_STYLE.startswith("bg=")
    assert "colour" in sticky.FOCUS_STYLE, "an ANSI colour, not an RGB guess"


def test_the_dismiss_hook_does_not_fire_on_every_click(sticky):
    """It runs on every change of active pane, which is every click that
    moves focus, and starting sticky to find nothing costs 37 ms each
    time. A sticky window is two panes, and three while a prompt is up."""
    line = next(ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                if ln.startswith("set-hook -g window-pane-changed"))
    assert "#{!=:#{window_panes},2}" in line
    assert line.index("if -F") < line.index("run-shell")


def test_a_finished_claude_takes_its_sidebar_by_hook(sticky):
    """Waiting for the sidebar to notice is half a second on a tmux that
    cannot say so and thirty on one that can; the hook is immediate.

    It is set at runtime rather than written into the config, because a
    server that was already running when sticky arrived never sources one -
    and a sidebar alone in a window is exactly that case.
    """
    assert "set-hook -g pane-exited" not in sticky.CONFIG_TEMPLATE
    assert "pane-exited" in sticky.CONFIG_TEMPLATE, "but it says where it went"
    assert "sweep" in sticky.EXIT_HOOK and "run-shell -b" in sticky.EXIT_HOOK
    assert "#{socket_path}" in sticky.EXIT_HOOK.format(binary="/bin/sticky")
    assert "sweep" in sticky.build_parser().sticky_commands


def test_the_new_tab_keys_go_through_run_shell(sticky):
    """run-shell expands #{} in what it runs; display-popup does not.

    Naming the popup in the config instead hands sticky the format strings
    themselves, and the tab never opens.
    """
    for line in sticky.CONFIG_TEMPLATE.splitlines():
        if line.startswith(("bind c ", "bind t ", "bind C ")):
            assert line.startswith(
                ("bind c run-shell", "bind t run-shell",
                 "bind C run-shell")), line
            assert "#{socket_path}" in line and "--ask" in line


@pytest.mark.parametrize("flag", [["--no-virtual-rows"],
                                  ["--virtual-rows", "0"]])
def test_virtual_rows_can_be_turned_off(sticky, flag):
    assert sticky.build_parser().parse_args(["start", *flag]).virtual_rows == 0


@pytest.mark.parametrize("argv, ours, theirs", [
    (["--no-virtual-rows", "--continue"], ["--no-virtual-rows"],
     ["--continue"]),
    (["--model", "opus", "--local"], ["--local"], ["--model", "opus"]),
])
def test_our_flags_are_split_from_claude_s(sticky, argv, ours, theirs):
    assert sticky.split_our_flags(argv) == (ours, theirs)


def test_help_is_ours_not_claude_s(tmp_path):
    """Passing --help through would open a window running `claude --help`."""
    done = subprocess.run([sys.executable, str(LAUNCHER), "--help"],
                          capture_output=True, text=True)
    assert done.returncode == 0
    assert "sticky-chat" in done.stdout and "{start,new" in done.stdout


@pytest.mark.parametrize("text, matches", [
    ("[Pasted text #1 +16 lines]", True),
    ("[Pasted text #2]", True),
    ("Claude folds it into a [Pasted text #1 +N lines] placeholder", False),
    ("we discussed Pasted text # earlier", False),
])
def test_the_folded_paste_check_matches_placeholders_not_prose(
        sticky, text, matches):
    assert bool(sticky.PASTE_PLACEHOLDER.search(text)) is matches


class TestGeneratedConfig:
    @pytest.fixture(scope="class")
    def config(self, tmp_path_factory):
        home = tmp_path_factory.mktemp("conf-home")
        subprocess.run(
            [sys.executable, str(LAUNCHER), "reload",
             "--socket", "sticky-no-such-server"],
            env={**os.environ, "STICKY_HOME": str(home)},
            capture_output=True, text=True)
        return (home / "tmux.conf").read_text().split("\n")

    def test_the_mouse_gesture_asks_add_to_use_its_judgement(self, config):
        mouse = [line for line in config
                 if line.startswith("bind -T copy-mode")
                 and "MouseDragEnd1Pane" in line]
        assert len(mouse) == 2
        assert all("--auto" in line for line in mouse)

    def test_dragging_in_the_sidebar_reaches_the_clipboard(self, config):
        # Over Claude a drag may become a note, so it leaves the clipboard
        # alone; in the sidebar copying is the only thing a drag can mean.
        drags = [line for line in config
                 if line.startswith("bind -T copy-mode")
                 and "MouseDragEnd1Pane" in line]
        assert len(drags) == 2
        for line in drags:
            sidebar_branch = line.rsplit("} {", 1)[-1]
            assert "copy-pipe-no-clear \"" in sidebar_branch

    def test_the_keyboard_always_makes_a_note(self, config):
        typed = [line for line in config
                 if line.startswith("bind -T copy-mode") and " N {" in line]
        assert len(typed) == 2
        assert not any("--auto" in line for line in typed)

    def test_the_state_directory_is_private(self, tmp_path, sticky):
        """Notes quote whatever was on screen, so the names are not public."""
        home = tmp_path / "state"
        sticky.private_dir(str(home))
        assert oct(home.stat().st_mode)[-3:] == "700"

    def test_a_drag_over_the_agent_is_always_a_selection(self, config):
        """Claude Code's multi-agent view asks tmux for every mouse event,
        which left a drag over that pane belonging to the agent - and
        dragging over that pane is the one gesture this program is about."""
        line = next(ln for ln in config
                    if ln.startswith("bind -T root MouseDrag1Pane"))
        agent, rest = line.split("' {", 1)[1].split("} {", 1)
        assert "copy-mode -M" in agent, agent
        assert "mouse_any_flag" not in agent, "the agent's wishes do not count"
        assert "mouse_any_flag" in rest, "everywhere else keeps tmux's rule"

    def test_the_wheel_goes_back_to_a_pane_drawing_its_own_screen(self, config):
        """There is no scrollback on an alternate screen, so scrolling in
        copy mode over one moves nothing while the agent's own view - the
        thing being scrolled at - never hears the wheel."""
        wheels = [ln for ln in config
                  if ln.startswith(("bind -T copy-mode Wheel",
                                    "bind -T copy-mode-vi Wheel"))]
        assert len(wheels) == 4, wheels
        for line in wheels:
            assert "alternate_on" in line and "mouse_any_flag" in line
            assert "cancel" in line and "send-keys -M" in line, line
            assert "scroll-up" in line or "scroll-down" in line, line

    def test_no_gesture_pulls_a_third_pane_into_a_sticky_window(self, config):
        """A sticky window is an agent and its sidebar, and there is nowhere
        in that for a third pane to go. tmux's ctrl-drag makes one where you
        drag, and alt-drag moves one about - which left two floating shells,
        one three columns wide, drawn over an agent as black boxes nobody
        could account for."""
        for key, gesture in (("C-MouseDrag1Pane", "new-pane -M"),
                             ("M-MouseDrag1Pane", "move-pane -M")):
            line = next(ln for ln in config
                        if ln.startswith(f"bind -T root {key} "))
            assert gesture in line, line
            guard = line.split(gesture)[0]
            assert "@sticky_role},}" in guard, (
                f"{key} must ask whether the pane is one of ours: {line}")

    def test_alt_drag_goes_through_to_whatever_wants_the_mouse(self, config):
        """The way out, for a program with its own scrollbars or its own
        selection - and only where something has actually asked for the
        mouse, since elsewhere there is nothing to hand it to and alt-drag
        is tmux's own way to move a pane."""
        line = next(ln for ln in config
                    if ln.startswith("bind -T root M-MouseDrag1Pane"))
        assert "mouse_any_flag" in line
        assert "send-keys -M" in line and "move-pane -M" in line

    def test_your_own_overrides_are_sourced_last(self, config):
        assert any(line.startswith("source-file -q") for line in config)


class TestRememberingTabs:
    """A tab records enough to be opened again after the machine restarts."""

    def test_a_command_that_names_a_session_is_left_alone(self, sticky):
        assert sticky.names_a_session("claude --resume abc")
        assert sticky.names_a_session("claude --continue")
        assert sticky.names_a_session("claude --session-id abc")
        assert not sticky.names_a_session("claude --model opus")

    def test_resuming_asks_for_the_conversation_by_id(self, sticky):
        record = {"session": "abc",
                  "command": "claude --model opus --session-id abc"}
        assert (sticky.resume_command(record)
                == "claude --model opus --resume abc")

    def test_a_fork_resumes_rather_than_forking_again(self, sticky):
        record = {"session": "def",
                  "command": "claude --continue --fork-session "
                             "--session-id def"}
        assert sticky.resume_command(record) == "claude --resume def"

    def test_the_conversation_is_named_once(self, sticky):
        """A tab started with --resume already names it; appending again
        gives Claude the flag twice, and stripping the flag word alone
        leaves its id behind as a stray argument."""
        for flag in ("--resume", "-r"):
            record = {"session": "abc", "command": f"claude {flag} abc"}
            assert sticky.resume_command(record) == "claude --resume abc"

    def test_a_continued_tab_is_asked_for_the_same_way(self, sticky):
        """--continue never told us which conversation it opened, so the
        only honest way to reopen it is to ask for the same thing again."""
        record = {"session": "tab-a1b2c3", "continued": True,
                  "command": "claude --continue"}
        assert sticky.resume_command(record) == "claude --continue"

    def test_a_tab_with_no_id_is_reopened_as_it_was(self, sticky):
        record = {"session": "", "command": "claude --resume mine"}
        assert sticky.resume_command(record) == "claude --resume mine"

    def test_the_id_is_read_back_off_a_command_line(self, sticky):
        assert sticky.session_named("claude --session-id abc") == "abc"
        assert sticky.session_named("claude --resume abc -m opus") == "abc"
        assert sticky.session_named("claude -r abc") == "abc"

    def test_continue_names_no_id_because_there_is_none_yet(self, sticky):
        assert sticky.session_named("claude --continue") == ""
        assert sticky.session_named("claude --resume --model opus") == ""

    def test_notes_follow_the_conversation_not_the_directory(
            self, sticky, tmp_path):
        project = str(tmp_path)
        assert (sticky.session_dir(project, "abc")
                != sticky.session_dir(project, "def"))
        assert (sticky.session_dir(project, "abc")
                == sticky.session_dir(project, "abc")), "resume finds it again"


class TestReopeningOneRememberedTab:
    """`C-g o`: the picker that puts one closed tab back.

    Nothing here opens anything. `open_tab` is stood in for and the server is
    a fake, so what is under test is which record the picker chooses and
    which command line that record would come back on.
    """

    ID = "1673c127-5bdd-4771-a91c-2472e26fd0ee"

    class FakeTmux:
        """Enough of a server to say which tabs are on screen."""

        def __init__(self, panes=""):
            self.socket = "test"
            self.panes = panes            # what `list-panes` answers
            self.said = []

        def run(self, *args):
            return self.panes if args[0] == "list-panes" else ""

        def ok(self, *args):
            self.said.append(args)
            return True

        def fmt(self, target, template):
            return "sticky:7"

    def remember(self, home, **fields):
        """One window record written by hand, so its age is ours to set."""
        windows = home / "windows"
        windows.mkdir(parents=True, exist_ok=True)
        (windows / f"{fields['session']}.json").write_text(json.dumps(
            {"created": 0, **fields}))

    @pytest.fixture
    def remembered(self, sticky, tmp_path, monkeypatch):
        """Three tabs, on three agents, newest first. Returns the project."""
        home = tmp_path / "home"
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(home))
        project = tmp_path / "project"
        project.mkdir()
        now = time.time()
        self.remember(home, session="claude-1", project=str(project),
                      name="newest", agent="claude", last_seen=now,
                      store="/store/claude-1",
                      command="claude --model opus --session-id claude-1")
        self.remember(home, session="tab-codex", project=str(project),
                      name="middle", agent="codex", continued=True,
                      store="/store/tab-codex",
                      command="codex -m gpt", last_seen=now - 3600)
        self.remember(home, session="tab-gemini", project=str(project),
                      name="oldest", agent="gemini", continued=True,
                      store="/store/tab-gemini",
                      agent_session=self.ID, command="gemini",
                      last_seen=now - 86400)
        return str(project)

    def answers(self, sticky, monkeypatch, *replies):
        """Type these lines at the picker, one per time it asks."""
        asked = []

        def prompt_line(prompt, initial="", **_):
            asked.append(initial)
            return replies[len(asked) - 1]

        monkeypatch.setattr(sticky.commands, "prompt_line", prompt_line)
        return asked

    def reopen(self, sticky, monkeypatch, tm, *replies):
        """Run the whole subcommand; returns what `open_tab` was handed."""
        opened = []
        monkeypatch.setattr(sticky.commands, "open_tab",
                            lambda tm, record, command, client:
                            opened.append((record, command)) or "%9")
        monkeypatch.setattr(sticky.commands, "Tmux", lambda socket: tm)
        self.answers(sticky, monkeypatch, *replies)
        args = argparse.Namespace(socket=None, client=None, ask=False,
                                  detach=True, source_pane=None)
        assert sticky.cmd_reopen(args) == 0
        return opened

    def test_the_number_you_type_picks_that_row(
            self, sticky, monkeypatch, remembered):
        self.answers(sticky, monkeypatch, ("save", "2"))
        chosen = sticky.pick_tab(sticky.reopenable(self.FakeTmux()))
        assert chosen["name"] == "middle"

    def test_enter_with_nothing_typed_takes_the_newest(
            self, sticky, monkeypatch, remembered):
        """Which is nearly always the tab you just closed."""
        self.answers(sticky, monkeypatch, ("save", ""))
        chosen = sticky.pick_tab(sticky.reopenable(self.FakeTmux()))
        assert chosen["name"] == "newest"

    def test_a_tab_already_on_screen_is_not_offered(
            self, sticky, remembered):
        """Reopening it would be a second tab on a live conversation.

        The panes are what say so - the agent's carries the id, the sidebar
        the note store - because a record's `last_seen` is kept warm by the
        very sidebar that would make it look closed.
        """
        tm = self.FakeTmux("claude-1\t\n\t/store/tab-codex\n")
        offered = [r["name"] for r in sticky.reopenable(tm)]
        assert offered == ["oldest"], "the open two are gone from the list"
        assert [r["name"] for r in sticky.reopenable(self.FakeTmux())] == [
            "newest", "middle", "oldest"], "and are back once they close"

    def test_a_number_that_is_not_a_row_opens_nothing(
            self, sticky, monkeypatch, remembered):
        """It is asked again with what was typed still on the line, the way
        the new-tab prompt asks again for a path that does not exist."""
        tm = self.FakeTmux()
        asked = self.answers(sticky, monkeypatch, ("save", "9"),
                             ("save", "0"), ("save", "no"), ("cancel", ""))
        assert sticky.pick_tab(sticky.reopenable(tm)) is None
        assert asked == ["", "9", "0", "no"], "the bad answer stays to correct"

    def test_esc_takes_nothing_with_it(
            self, sticky, monkeypatch, remembered):
        tm = self.FakeTmux()
        assert self.reopen(sticky, monkeypatch, tm, ("cancel", "2")) == []
        assert not tm.said, "and nothing was said to a client either"

    @pytest.mark.parametrize("typed, line", [
        ("1", "claude --model opus --resume claude-1"),
        ("2", "codex resume --last -m gpt"),
        ("3", f"gemini --resume {ID}"),
    ])
    def test_the_tab_comes_back_on_its_own_resuming_line(
            self, sticky, monkeypatch, remembered, typed, line):
        """One picker, three profiles, and `resume_command` decides for all
        three - so the picker cannot drift from what `resume` would run."""
        opened = self.reopen(sticky, monkeypatch, self.FakeTmux(),
                             ("save", typed))
        record, command = opened[0]
        assert command == line
        assert command == sticky.resume_command(
            record, sticky.agent_named(record["agent"]))

    def test_the_row_says_which_tab_and_what_comes_back(
            self, sticky, remembered):
        """The four things you choose by: the project, the agent, how long
        ago, and whether the conversation itself comes back."""
        rows = [sticky.ANSI.sub("", sticky.tab_row(n, r)) for n, r
                in enumerate(sticky.reopenable(self.FakeTmux()), 1)]
        assert rows[0].split() == ["1", "newest", "claude", "just", "now",
                                   "same", "conversation"]
        assert rows[1].split() == ["2", "middle", "codex", "1h", "ago", "its",
                                   "last", "conversation"]
        assert rows[2].endswith("same conversation"), "the id it learned"

    def test_a_tab_whose_project_has_gone_is_not_offered(
            self, sticky, tmp_path, monkeypatch):
        """`open_tab` refuses it, so a row for it is a row in the way."""
        home = tmp_path / "home"
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(home))
        self.remember(home, session="claude-9", project=str(tmp_path / "gone"),
                      name="gone", agent="claude", last_seen=time.time(),
                      command="claude --session-id claude-9")
        assert sticky.reopenable(self.FakeTmux()) == []

    def test_the_key_binding_goes_through_run_shell_like_the_others(
            self, sticky):
        line = next(ln for ln in sticky.CONFIG_TEMPLATE.splitlines()
                    if ln.startswith("bind o "))
        assert line.startswith("bind o run-shell -b '@BIN@ reopen --ask")
        assert "#{socket_path}" in line and "#{client_name}" in line

    def test_reopen_is_a_command_of_ours(self, sticky):
        assert "reopen" in sticky.build_parser().sticky_commands


class TestTheDependencyPackagingCannotDeclare:
    """tmux is not a Python package, so nothing installs it for you.

    Which makes the first run the moment it matters: `pip install` says
    nothing about it, and the first thing sticky does is ask tmux a
    question. A traceback out of subprocess is a poor way to be told.
    """

    def test_a_missing_tmux_is_said_plainly(self, sticky, monkeypatch):
        def gone(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        monkeypatch.setattr("sticky_chat.tmux.subprocess.run", gone)
        with pytest.raises(SystemExit) as leaving:
            sticky.Tmux("nowhere").run("list-panes")
        assert leaving.value.code == 1

    def test_it_says_how_to_get_one(self, sticky, monkeypatch, capsys):
        def gone(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        monkeypatch.setattr("sticky_chat.tmux.subprocess.run", gone)
        with pytest.raises(SystemExit):
            sticky.Tmux("nowhere").run("list-panes")
        said = capsys.readouterr().err
        assert "not on your PATH" in said
        assert "brew install tmux" in said and "apt install tmux" in said


class TestTheTabListSaysWhichAgent:
    """One character after the tab's name, so a glance answers it.

    Which agent, not merely whether: with several profiles the useful
    question is usually which one is in tab 3.
    """

    def test_every_profile_has_one(self, sticky):
        for name, agent in sticky.AGENTS.items():
            assert agent.mark, f"{name} has no mark"
            assert sticky.cell_width(agent.mark) == 1, \
                f"{name}'s mark is {sticky.cell_width(agent.mark)} columns"

    def test_they_are_distinct(self, sticky):
        marks = [a.mark for a in sticky.AGENTS.values()]
        assert len(set(marks)) == len(marks), f"two profiles share one: {marks}"

    def test_an_unnamed_agent_still_gets_something(self, sticky):
        """The default is on the field, so a profile added without thinking
        about the tab list still shows up in it."""
        import dataclasses
        quiet = dataclasses.replace(sticky.GENERIC, name="whatever")
        assert quiet.mark == sticky.GENERIC.mark
        assert dataclasses.fields(sticky.Agent)
        made = sticky.Agent(name="x", label="x", short="x", command="x")
        assert made.mark, "the field's default is what saves a new profile"

    def test_the_status_line_reads_it_off_the_window(self, sticky):
        """A pane option would come and go: the status line resolves a
        format against whichever pane is active, and half of a sticky
        window is the sidebar."""
        config = sticky.CONFIG_TEMPLATE
        assert "window-status-format" in config
        assert "@sticky_mark" in config
        assert "#F" in config, "tmux's own flags stay on the end"


class TestSendingToAnotherTab:
    """Notes are taken where the output is and belong where the agent is.

    Reading a log, a test run or a diff in a shell tab is the case that
    needs this: the output worth annotating is not always printed by
    something you can talk to, so the batch goes to a tab by its number -
    the number tmux already shows in the status line.
    """

    def panes(self, rows):
        class Fake:
            def run(self, *_args, **_kwargs):
                return "\n".join(rows)
        return Fake()

    def test_a_number_finds_that_tab_s_agent(self, sticky):
        tm = self.panes(["1\t%0\tclaude", "1\t%1\tsidebar",
                         "2\t%2\tclaude", "2\t%3\tsidebar"])
        assert sticky.tab_pane(tm, 2) == "%2"
        assert sticky.tab_pane(tm, 1) == "%0"

    def test_the_sidebar_is_never_the_answer(self, sticky):
        """Nor is an open note prompt: a batch pasted into either would be
        typed at the notes rather than at the agent."""
        tm = self.panes(["3\t%9\tsidebar", "3\t%8\tnote", "3\t%7\tclaude"])
        assert sticky.tab_pane(tm, 3) == "%7"

    def test_a_tab_that_is_not_there_says_so(self, sticky):
        tm = self.panes(["1\t%0\tclaude", "1\t%1\tsidebar"])
        assert sticky.tab_pane(tm, 4) == ""

    def test_the_shell_profile_runs_a_shell_and_promises_nothing(self, sticky):
        """A tab with no agent in it. It cannot be resumed or forked -
        there is no conversation - and that is the whole of it."""
        shell = sticky.SHELL
        assert shell.name == "shell" and shell.command
        assert not shell.can_resume and not shell.can_fork
        assert not shell.can_discover and not shell.names_sessions
        assert sticky.require_agent("shell") is shell


class TestAgentProfiles:
    """Everything that knows *which* agent is in the pane is one record.

    Two things are being checked. The claude profile has to reproduce what
    the program did before profiles existed, to the word, because every
    existing tab depends on it; and a profile that cannot name a
    conversation has to decline the features that need one rather than write
    a command line the agent would refuse.
    """

    class FakeTmux:
        """Pane options and nothing else, which is all `agent_of` reads."""

        def __init__(self, panes):
            self.panes = panes

        def option(self, pane, name):
            return self.panes.get(pane, {}).get(name, "")

    def test_claude_is_what_saying_nothing_gets_you(self, sticky):
        assert sticky.DEFAULT_AGENT == "claude"
        assert sticky.agent_named(None) is sticky.CLAUDE
        assert sticky.require_agent("") is sticky.CLAUDE
        assert sticky.build_parser().parse_args(["start"]).agent is None

    def test_the_claude_profile_is_the_command_line_it_always_was(self, sticky):
        claude = sticky.CLAUDE
        assert claude.command == "claude"
        assert claude.no_menu_args == ("--disallowedTools", "AskUserQuestion")
        assert claude.session_flags == ("--session-id", "--resume", "-r",
                                        "--continue", "-c")
        assert claude.bare_session_flags == ("--continue", "-c",
                                             "--fork-session")
        assert f"claude {claude.session_flag} abc" == "claude --session-id abc"
        assert (" ".join(["claude", *claude.fork_flags,
                          claude.session_flag, "abc"])
                == "claude --continue --fork-session --session-id abc")
        assert dict(claude.env) == {"CLAUDE_CODE_TMUX_SESSION": "sticky",
                                    "CLAUDE_CODE_TMUX_PREFIX": "C-g"}

    def test_the_placer_still_reads_the_output_it_used_to(self, sticky):
        assert (sticky.PROMPT_ROWS, sticky.PROMPT_BOX_ROWS) == (2, 10)
        assert sticky.GLYPH_LANDMARK is sticky.CLAUDE.glyph_landmark
        assert sticky.PASTE_PLACEHOLDER is sticky.CLAUDE.paste_placeholder
        found = sticky.find_landmark(["\u23fa Read(agents.py)", "  a line"])
        assert found["text"] == "\u23fa Read(agents.py)"

    def test_the_prompt_box_is_as_tall_as_the_profile_says(self, sticky):
        """The heuristics come off the profile, not off the module."""
        deep = dataclasses.replace(sticky.CLAUDE, prompt_rows=6)
        assert not sticky.in_prompt_box(90, 96)
        assert sticky.in_prompt_box(90, 96, deep)

    def test_the_generic_profile_claims_nothing_it_cannot_do(self, sticky):
        generic = sticky.GENERIC
        assert generic.command == "", "the command is yours to name"
        assert not generic.names_sessions
        assert not generic.can_resume and not generic.can_fork
        assert generic.env == () and generic.no_menu_args == ()
        assert generic.paste_placeholder is None, "a second paste sends twice"
        # The output heuristics stay where they are: they are a guess about
        # a terminal frame, not about Claude, and nothing better is known.
        assert generic.prompt_rows == sticky.CLAUDE.prompt_rows
        assert generic.prompt_box_rows == sticky.CLAUDE.prompt_box_rows
        assert generic.glyph_landmark is sticky.CLAUDE.glyph_landmark

    def test_generic_leaves_a_command_line_exactly_as_it_found_it(self, sticky):
        line = "aider --resume abc --continue"
        assert not sticky.names_a_session(line, sticky.GENERIC)
        assert sticky.session_named(line, sticky.GENERIC) == ""
        assert sticky.without_session(line, sticky.GENERIC) == line

    def test_resuming_one_asks_for_the_same_thing_again(self, sticky):
        """Declining is a command line with no flag in it that we made up."""
        record = {"agent": "generic", "session": "tab-a1b2c3",
                  "command": "aider --model gpt"}
        assert sticky.resume_command(record) == "aider --model gpt"

    def test_a_record_from_before_profiles_is_still_claude_s(self, sticky):
        record = {"session": "abc", "command": "claude --session-id abc"}
        assert sticky.resume_command(record) == "claude --resume abc"

    def test_two_spellings_of_a_flag_are_not_both_written(self, sticky):
        """`--continue` and `-c` are one flag twice; `resume --last` is two
        words that go together. The recognising set holds every spelling so a
        line the user wrote is never second-guessed, and `continue_words` is
        the one that goes on a line we write."""
        assert sticky.CLAUDE.continue_words == ("--continue",)
        assert sticky.ANTIGRAVITY.continue_words == ("--continue",)
        assert sticky.CODEX.continue_words == ("resume", "--last")
        assert sticky.GEMINI.continue_words == ("--resume",)
        assert sticky.GENERIC.continue_words == ()
        for agent in sticky.AGENTS.values():
            for word in agent.continue_words:
                assert word in agent.continue_flags, \
                    f"{agent.name} would write a flag it does not know"

    def test_antigravity_is_where_gemini_users_are_being_sent(self, sticky):
        """Homebrew's gemini-cli is deprecated in favour of this one, so the
        profile carries the flags `agy --help` lists and nothing else: it can
        be told to reopen a conversation by id, but not to start one under an
        id we chose, so the id is read back off the state database it keeps.

        The five database names are read off the live schema on this machine
        rather than off documentation, and are pinned here because the query
        is built out of them: a typo in one is a profile that silently learns
        nothing."""
        agy = sticky.ANTIGRAVITY
        assert agy.command == "agy" and agy.tested is True
        assert agy.resume_flags == ("--conversation",)
        assert agy.continue_flags == ("--continue", "-c")
        assert agy.can_resume and agy.can_discover and not agy.can_fork
        assert (agy.transcript_glob
                == ".gemini/antigravity-cli/conversations/*.db")
        assert agy.state_db_glob == "", (
            "the summaries database beside it knows the project and would be "
            "the better source, but on a Google AI Pro account it stays "
            "empty - the id is in the name of the conversation's own file")
        for field in ("session_flag", "fork_flags", "env", "no_menu_args"):
            assert not getattr(agy, field), f"{field} was invented"
        assert agy.name == "agy", "every profile is named for its command"
        assert "antigravity" in agy.aliases, "what the cask and product are"
        assert sticky.require_agent("Antigravity") is agy
        assert sticky.known_name("agy") == "agy"
        assert sticky.known_name("nothing-like-that") == ""

    def test_antigravity_marks_its_turns_the_way_it_does(self, sticky):
        """`\u25cf` for a tool and `\u25b8` for a thought, seen on 1.1.27."""
        glyphs = sticky.ANTIGRAVITY.glyph_landmark
        assert glyphs.match("\u25cf Read(/private/tmp/calc.py)")
        assert glyphs.match("\u25b8 Thought for 2s, 590 tokens")
        assert not glyphs.match("  In calc.py, the function subtracts")

    def test_gemini_claims_only_what_gemini_documents(self, sticky):
        """One flag doing two jobs: `--resume <uuid>` opens the conversation
        with that id, `--resume` alone the last one in this directory. Gemini
        still cannot be *told* an id at the start, so the first only works
        once the transcript has said what the id is."""
        gemini = sticky.GEMINI
        assert gemini.tested is True, "tried end to end on Gemini CLI 0.46.0"
        assert "gemini (untested)" not in sticky.agent_choices()
        assert gemini.command == "gemini"
        assert gemini.glyph_landmark.match("  \u2713  ReadFile  calc.py")
        assert gemini.glyph_landmark.match("\u2726 The add function subtracts")
        assert not gemini.glyph_landmark.match("\u2139 update available"), \
            "a banner is not a turn to hang a note on"
        assert gemini.resume_flags == ("--resume",)
        assert gemini.continue_flags == ("--resume",)
        assert not gemini.words_first, "a flag, not a subcommand"
        for field in ("session_flag", "fork_flags", "env", "no_menu_args",
                      "paste_placeholder"):
            assert not getattr(gemini, field), f"{field} was invented"
        assert not gemini.names_sessions, "the id is Gemini's to choose"
        assert gemini.can_resume and not gemini.can_fork
        assert gemini.can_discover, "and ours to read back off the transcript"

    def test_codex_claims_only_what_codex_documents(self, sticky):
        """Its session verbs are subcommands, and both are asked for the
        most recent rather than for the picker: the tab you pressed the key
        in has already answered "which conversation"."""
        codex = sticky.CODEX
        assert codex.tested, "a tab has been through it end to end"
        assert "untested" not in sticky.agent_choices(), \
            "and so has every other profile here"
        assert codex.command == "codex"
        assert codex.resume_flags == ("resume",)
        assert codex.continue_flags == ("resume", "--last")
        assert codex.fork_flags == ("fork", "--last")
        assert codex.words_first
        for field in ("session_flag", "env", "no_menu_args",
                      "paste_placeholder"):
            assert not getattr(codex, field), f"{field} was invented"
        # It can branch, and it cannot be handed an id for what the branch
        # made - the two are separate questions, and the second is answered
        # afterwards by reading what codex called it.
        assert codex.can_fork and not codex.names_sessions
        assert codex.can_resume and codex.can_discover
        # What 0.153.4 calls them, and what the query has always asked for.
        assert (codex.state_db_table, codex.state_db_id_column,
                codex.state_db_time_columns, codex.state_db_project_column) \
            == ("threads", "id", ("created_at", "created_at_ms"), "cwd")

    def test_resuming_puts_a_subcommand_first_too(self, sticky):
        """Hypothetical - no profile here can both branch and be handed an
        id - but the placement rule has to hold at every point that composes
        a line, and `resume_command` is the third of them."""
        agent = dataclasses.replace(sticky.CODEX, resume_flags=("resume",))
        record = {"session": "abc", "command": "codex -m gpt"}
        assert (sticky.resume_command(record, agent)
                == "codex resume abc -m gpt")

    def test_a_subcommand_goes_first_and_a_flag_goes_last(self, sticky):
        """`codex fork --last -m gpt` is a fork; `codex -m gpt fork --last`
        is codex being handed an argument it has never heard of."""
        claude, codex = sticky.CLAUDE, sticky.CODEX
        assert (sticky.with_words("claude --model opus",
                                  ["--session-id", "abc"], claude)
                == "claude --model opus --session-id abc")
        assert (sticky.with_words("codex -m gpt", list(codex.fork_flags),
                                  codex)
                == "codex fork --last -m gpt")
        assert sticky.with_words("codex", list(codex.fork_flags), codex) \
            == "codex fork --last"
        assert sticky.with_words("codex", [], codex) == "codex"

    def test_a_name_that_is_not_a_profile(self, sticky):
        with pytest.raises(SystemExit):
            sticky.require_agent("gemeni")      # typed: worth stopping for
        assert sticky.agent_named("gemeni") is sticky.CLAUDE, \
            "read back off a pane: draw the tab rather than refuse it"

    def test_the_flags_that_pick_one(self, sticky):
        parser = sticky.build_parser()
        assert parser.parse_args(["start", "--agent", "generic"]).agent \
            == "generic"
        assert parser.parse_args(["new", "--agent", "gemini"]).agent == "gemini"

    def test_the_command_is_named_one_way_only(self, sticky):
        """One spelling for one thing: --claude-cmd said the agent's name
        twice on a line that may not be starting Claude at all."""
        parser = sticky.build_parser()
        assert parser.parse_args(["start", "--agent-cmd", "x"]).agent_cmd \
            == "x"
        with pytest.raises(SystemExit):
            parser.parse_args(["start", "--claude-cmd", "x"])
        assert "--claude-cmd" not in sticky.OUR_FLAGS

    def test_the_new_flags_are_ours_rather_than_the_agent_s(self, sticky):
        argv = ["--agent", "generic", "--agent-cmd", "aider", "--model", "opus"]
        assert sticky.split_our_flags(argv) == (
            ["--agent", "generic", "--agent-cmd", "aider"], ["--model", "opus"])

    def test_any_pane_of_a_tab_can_say_which_agent(self, sticky):
        tm = self.FakeTmux({"%1": {"@sticky_agent": "gemini"},
                            "%2": {"@sticky_partner": "%1"}})
        assert sticky.agent_of(tm, "%1") is sticky.GEMINI
        assert sticky.agent_of(tm, "%2") is sticky.GEMINI, "the sidebar asks"
        assert sticky.agent_of(tm, "%9") is sticky.CLAUDE, "a tab from before"
        assert sticky.agent_of(tm, None) is sticky.CLAUDE

    def test_the_help_panel_names_the_agent_it_is_drawing(self, sticky):
        titles = [title for title, _ in sticky.help_sections(sticky.GEMINI)]
        assert "Send to Gemini" in titles
        assert "Send to Claude" in [t for t, _ in sticky.HELP_SECTIONS]


class TestLearningTheIdTheAgentChose:
    """Some agents pick their own conversation id and tell nobody.

    The one place it is written down is the transcript, whose first line is a
    header carrying the id, so a tab on such an agent learns its id by
    reading that line rather than by having handed one out. Every file here
    is fabricated in a temporary HOME: nothing in this class runs an agent.
    """

    ID = "1673c127-5bdd-4771-a91c-2472e26fd0ee"
    OTHER = "9e5d4c3b-2a19-4f87-b6d5-0c1e2f3a4b5c"
    LAUNCH = 1_600_000_000.0            # when the tab in these tests started

    def transcript(self, home, path, first, *rest, when):
        """One fabricated JSONL transcript, at a chosen modification time."""
        full = home / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("\n".join([first, *rest]) + "\n")
        os.utime(full, (when, when))
        return full

    def header(self, session_id):
        return (f'{{"sessionId": "{session_id}", "projectHash": "11cc", '
                f'"kind": "main"}}')

    def continuation(self, home, folder, session, carrying, when):
        """A transcript that carries on from another, as a resume writes it:
        its own id in the header, and the records it copied over still
        carrying the id they were written under."""
        return self.transcript(
            home, f"{folder}/{session}.jsonl", self.header(session),
            f'{{"type": "user", "session_id": "{carrying}", "n": 1}}',
            when=when)

    def test_a_resumed_conversation_is_followed_to_where_it_went(
            self, sticky, tmp_path):
        """Claude Code's --resume does not write on at the end of the record
        it was given: it copies what was said into a file of its own with a
        new id. Every tab reopened once was reading, and would have resumed
        from, a transcript that stopped the moment it was last restarted."""
        home = tmp_path / "home"
        folder = ".claude/projects/-p-thing"
        self.transcript(home, f"{folder}/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH)
        self.continuation(home, folder, self.OTHER, self.ID,
                          when=self.LAUNCH + 5)
        assert sticky.continued_session(sticky.CLAUDE, "/p/thing", self.ID,
                                        0.0, str(home)) == self.OTHER

    def test_the_tab_next_door_is_not_adopted(self, sticky, tmp_path):
        """Three tabs on one project write into one directory, so "the newest
        transcript" would hand this tab the conversation beside it. Only a
        file carrying *our* id is ours."""
        home = tmp_path / "home"
        folder = ".claude/projects/-p-thing"
        self.transcript(home, f"{folder}/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH)
        self.continuation(home, folder, self.OTHER,
                          "5f0e3b7a-1111-2222-3333-444455556666",
                          when=self.LAUNCH + 9)
        assert not sticky.continued_session(sticky.CLAUDE, "/p/thing",
                                            self.ID, 0.0, str(home))

    def test_a_conversation_nobody_resumed_carries_on_nowhere(
            self, sticky, tmp_path):
        home = tmp_path / "home"
        self.transcript(home, f".claude/projects/-p-thing/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH)
        assert not sticky.continued_session(sticky.CLAUDE, "/p/thing",
                                            self.ID, 0.0, str(home))

    def test_the_one_made_when_this_tab_started_is_this_tab_s(
            self, sticky, tmp_path):
        """Resume the same conversation in two tabs and each has a
        continuation carrying its id. The one that appeared as this tab
        started is the one this tab is writing."""
        home = tmp_path / "home"
        folder = ".claude/projects/-p-thing"
        older = "11111111-1111-4111-8111-111111111111"
        mine = "22222222-2222-4222-8222-222222222222"
        self.transcript(home, f"{folder}/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH)
        self.continuation(home, folder, older, self.ID, when=self.LAUNCH + 5)
        self.continuation(home, folder, mine, self.ID, when=self.LAUNCH + 50)
        assert sticky.continued_session(sticky.CLAUDE, "/p/thing", self.ID,
                                        self.LAUNCH + 40, str(home)) == mine
        assert sticky.continued_session(sticky.CLAUDE, "/p/thing", self.ID,
                                        0.0, str(home)) == older, \
            "and with no launch time to go on, the first one after it"

    def test_a_profile_that_does_not_fork_is_not_asked(self, sticky,
                                                       tmp_path):
        """Gemini and codex write on at the end of the record they were
        given, so there is nothing to follow."""
        assert not sticky.continued_session(sticky.GEMINI, "/p/thing",
                                            self.ID, 0.0, str(tmp_path))
        assert not sticky.continued_session(sticky.SHELL, "/p/thing",
                                            self.ID, 0.0, str(tmp_path))

    def test_discovery_asks_the_right_question_per_profile(self, sticky,
                                                           tmp_path):
        """One entry point: the sidebar asks what the agent calls this
        conversation and the profile decides what that means."""
        home = tmp_path / "home"
        folder = ".claude/projects/-p-thing"
        self.transcript(home, f"{folder}/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH)
        self.continuation(home, folder, self.OTHER, self.ID,
                          when=self.LAUNCH + 5)
        assert sticky.discover_session(sticky.CLAUDE, 0.0, str(home),
                                       project="/p/thing",
                                       named=self.ID) == self.OTHER
        assert not sticky.discover_session(sticky.CLAUDE, 0.0, str(home),
                                           project="/p/thing"), \
            "and with no id in hand there is nothing to follow"

    def test_the_newest_transcript_written_since_launch_says_the_id(
            self, sticky, tmp_path):
        home = tmp_path / "home"
        self.transcript(home, ".gemini/tmp/a-project/chats/session-old.jsonl",
                        self.header(self.OTHER), when=self.LAUNCH + 5)
        self.transcript(home, ".gemini/tmp/a-project/chats/session-new.jsonl",
                        self.header(self.ID), when=self.LAUNCH + 9)
        assert (sticky.discover_session(sticky.GEMINI, self.LAUNCH, str(home))
                == self.ID)

    def test_a_transcript_older_than_the_tab_is_somebody_elses(
            self, sticky, tmp_path):
        """The glob has no idea which project it is looking at, so the launch
        time is the whole of what stops yesterday's conversation being
        adopted by a tab opened this morning."""
        home = tmp_path / "home"
        self.transcript(home, ".gemini/tmp/elsewhere/chats/session-a.jsonl",
                        self.header(self.OTHER), when=self.LAUNCH - 1)
        assert (sticky.discover_session(sticky.GEMINI, self.LAUNCH, str(home))
                == "")

    def test_a_header_with_no_id_in_it_teaches_nothing(self, sticky, tmp_path):
        home = tmp_path / "home"
        self.transcript(home, ".gemini/tmp/a-project/chats/session-a.jsonl",
                        '{"kind": "main", "startTime": "2026-05-13"}',
                        when=self.LAUNCH + 5)
        assert (sticky.discover_session(sticky.GEMINI, self.LAUNCH, str(home))
                == "")

    def test_a_field_that_is_not_an_id_is_not_taken_for_one(
            self, sticky, tmp_path):
        """`id` is tried because some agent will spell it that way, and a
        field of that name holding something else is not something any agent
        would take back: no answer beats a wrong one."""
        home = tmp_path / "home"
        path = self.transcript(home, ".agent/sessions/rollout-a.jsonl",
                               '{"id": "yesterdays-chat"}',
                               when=self.LAUNCH + 5)
        assert sticky.session_id_of(str(path)) == ""

    def test_only_the_first_line_is_ever_read(self, sticky, tmp_path):
        """Not an optimisation. A Gemini transcript on the machine this was
        written on had reached 1.6 GB, so the rest of the file is broken here
        on purpose: anything that parsed the whole thing would fail this."""
        home = tmp_path / "home"
        path = self.transcript(home,
                               ".gemini/tmp/a-project/chats/session-a.jsonl",
                               self.header(self.ID),
                               '{"role": "user", not json at all',
                               when=self.LAUNCH + 5)
        with pytest.raises(ValueError):
            json.loads(path.read_text())        # a whole-file read would die
        assert sticky.session_id_of(str(path)) == self.ID
        assert (sticky.discover_session(sticky.GEMINI, self.LAUNCH, str(home))
                == self.ID)

    def test_a_line_with_no_end_to_it_is_not_read_whole(self, sticky,
                                                        tmp_path):
        """The cap is on the read, not on the file: a transcript with no
        newline in it at all must not arrive in memory entire."""
        home = tmp_path / "home"
        path = home / "big.jsonl"
        home.mkdir(parents=True, exist_ok=True)
        path.write_text("{" + "x" * (sticky.HEADER_BYTES * 2))
        assert sticky.session_id_of(str(path)) == ""

    def test_the_file_name_is_the_fallback(self, sticky, tmp_path):
        """Claude Code names the file after the id, so a header that says
        nothing is not the end of the question."""
        home = tmp_path / "home"
        path = self.transcript(home,
                               f".claude/projects/-a/{self.ID}.jsonl",
                               '{"kind": "session_start"}',
                               when=self.LAUNCH + 5)
        assert sticky.session_id_of(str(path)) == self.ID

    def state_db(self, home, rows, name="state_5.sqlite"):
        """A codex state database with just the column its threads are found
        by. The real table has forty; asking for three is what makes this a
        test of the query rather than of a schema that will move again."""
        import sqlite3
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(home / ".codex" / name)
        db.execute("create table threads (id text, created_at integer, "
                   "created_at_ms integer, cwd text)")
        db.executemany("insert into threads values (?, ?, ?, ?)", rows)
        db.commit()
        db.close()

    def test_codex_is_asked_its_database_about_this_project(self, sticky,
                                                            tmp_path):
        """Codex kept JSONL rollouts once and keeps sqlite as of 0.153.4.
        The row knows the project, so the newest one *here* is the answer -
        not the newest anywhere, which is what a glob would have said."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.state_db(home, [(self.ID, self.LAUNCH + 5, None, project),
                             ("11111111-1111-4111-8111-111111111111",
                              self.LAUNCH + 9, None, str(tmp_path / "other"))])
        assert sticky.CODEX.can_discover and not sticky.CODEX.transcript_glob
        assert sticky.discover_session(sticky.CODEX, self.LAUNCH, str(home),
                                       project=project) == self.ID

    def test_a_conversation_older_than_the_tab_is_not_adopted(self, sticky,
                                                              tmp_path):
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.state_db(home, [(self.ID, self.LAUNCH - 60, None, project)])
        assert sticky.discover_session(sticky.CODEX, self.LAUNCH, str(home),
                                       project=project) == ""

    def test_milliseconds_are_told_from_seconds(self, sticky, tmp_path):
        """Both columns exist in the real table and the names do not settle
        which unit is in them, so the scale is normalised, not assumed."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.state_db(home, [(self.ID, 0, int((self.LAUNCH + 5) * 1000),
                              project)])
        assert sticky.discover_session(sticky.CODEX, self.LAUNCH, str(home),
                                       project=project) == self.ID

    def test_a_database_with_another_schema_teaches_nothing(self, sticky,
                                                            tmp_path):
        """It belongs to another program, which is free to move it. Learning
        nothing is the failure that was always allowed for."""
        import sqlite3
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)
        db = sqlite3.connect(home / ".codex" / "state_9.sqlite")
        db.execute("create table conversations (uuid text)")
        db.commit()
        db.close()
        assert sticky.discover_session(sticky.CODEX, self.LAUNCH, str(home),
                                       project=str(tmp_path)) == ""

    def test_antigravitys_id_is_the_name_of_its_own_file(self, sticky,
                                                         tmp_path):
        """One sqlite file per conversation, named for its id, so nothing
        inside is opened - there is no first line to read in a database."""
        home = tmp_path / "home"
        chats = home / ".gemini" / "antigravity-cli" / "conversations"
        chats.mkdir(parents=True)
        for name, when in ((self.ID, self.LAUNCH + 5),
                           ("11111111-1111-4111-8111-111111111111",
                            self.LAUNCH - 60)):
            path = chats / f"{name}.db"
            path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
            os.utime(path, (when, when))
        assert (sticky.discover_session(sticky.ANTIGRAVITY, self.LAUNCH,
                                        str(home)) == self.ID), \
            "the one written after the tab started, not the older one"

    def summaries_profile(self, sticky):
        """A profile shaped like antigravity's `conversation_summaries.db`.

        Synthetic, because antigravity turned out not to need it: that
        database stays empty on a Google AI Pro account, and the id is in the
        name of the per-conversation file instead. The names below are still
        read off the live schema, and the query built from them is the one
        codex uses - so this is the general mechanism under test, with the
        second real schema anybody has seen standing in for a future one.
        """
        return dataclasses.replace(
            sticky.GENERIC,
            state_db_glob=".gemini/antigravity-cli/conversation_summaries.db",
            state_db_table="conversation_summaries",
            state_db_id_column="conversation_id",
            state_db_time_columns=("last_modified_time",),
            state_db_project_column="workspace_uris")

    def summaries_db(self, home, rows, table="conversation_summaries"):
        """An antigravity conversation database, with the three columns a
        conversation is found by. The real table has twenty; these three are
        read off the live schema in ~/.gemini/antigravity-cli/ on this
        machine, and `last_modified_time` is declared exactly as it is
        declared there - `datetime`, which sqlite does not enforce."""
        import sqlite3
        here = home / ".gemini" / "antigravity-cli"
        here.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(here / "conversation_summaries.db")
        db.execute(f"create table {table} (conversation_id text, "
                   "last_modified_time datetime, workspace_uris text)")
        db.executemany(f"insert into {table} values (?, ?, ?)", rows)
        db.commit()
        db.close()

    def test_antigravity_is_asked_the_same_question_codex_is(self, sticky,
                                                             tmp_path):
        """Same shape, different words. Antigravity keeps sqlite too, so the
        profile spells the table and the columns and one query serves both -
        here with the plainest thing `workspace_uris` could hold, the path
        itself, which is what codex's `cwd` holds. The newest conversation
        *here* is the answer: not the older one in this project, and not the
        newer one somewhere else."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.summaries_db(home, [(self.ID, int(self.LAUNCH + 5), project),
                                 ("11111111-1111-4111-8111-111111111111",
                                  int(self.LAUNCH + 1), project),
                                 (self.OTHER, int(self.LAUNCH + 9),
                                  str(tmp_path / "other"))])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == self.ID

    def test_a_project_written_as_a_uri_is_still_this_project(self, sticky,
                                                              tmp_path):
        """The column is called `workspace_uris`, and nobody has written a
        row in it yet: it may hold a path, a `file://` URI, or a JSON list
        of either. All three are matched as a substring rather than guessed
        between."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.summaries_db(home, [(self.ID, int(self.LAUNCH + 5),
                                  f"file://{project}")])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == self.ID

    def test_a_list_of_workspaces_with_this_one_in_it_counts(self, sticky,
                                                             tmp_path):
        """Plural, so the third shape is a JSON list - and a conversation
        open on two workspaces at once is still this project's."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        uris = json.dumps([f"file://{tmp_path / 'other'}",
                           f"file://{project}"])
        self.summaries_db(home, [(self.ID, int(self.LAUNCH + 5), uris)])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == self.ID

    def test_another_projects_conversation_is_never_adopted(self, sticky,
                                                            tmp_path):
        """The one thing a substring match must not cost. A row for another
        workspace is not this tab's, however new it is."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        elsewhere = json.dumps([f"file://{tmp_path / 'other'}"])
        self.summaries_db(home, [(self.OTHER, int(self.LAUNCH + 9),
                                  elsewhere)])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == ""

    def test_a_time_written_as_a_string_is_read_as_one(self, sticky,
                                                       tmp_path):
        """`last_modified_time` is declared `datetime`, which sqlite stores
        as whatever was handed to it. An ISO-8601 string is what a program
        that writes a `datetime` most often means."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(self.LAUNCH + 5)) + ".500000000Z"
        self.summaries_db(home, [(self.ID, stamp, project)])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == self.ID

    def test_a_datetime_arrives_in_whichever_shape_wrote_it(self, sticky):
        """One helper for every shape a time column comes back in, and 0.0 -
        "no idea" - for the ones it cannot read at all."""
        utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.LAUNCH))
        assert sticky.unix_time(utc + "Z") == self.LAUNCH
        assert sticky.unix_time(utc + ".500000000Z") == self.LAUNCH + 0.5
        assert sticky.unix_time(utc.replace("T", " ") + "+00:00") \
            == self.LAUNCH
        here = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.LAUNCH))
        assert sticky.unix_time(here) == self.LAUNCH, "no zone means this one"
        assert sticky.unix_time(int(self.LAUNCH)) == self.LAUNCH
        assert sticky.unix_time(self.LAUNCH) == self.LAUNCH
        assert sticky.unix_time(int(self.LAUNCH * 1000)) == self.LAUNCH
        assert sticky.unix_time(str(int(self.LAUNCH))) == self.LAUNCH
        for nonsense in ("last Tuesday", "", None, b"\xff", "2026-13-40"):
            assert sticky.unix_time(nonsense) == 0.0

    def test_a_time_nobody_can_read_is_not_a_refusal(self, sticky, tmp_path):
        """The trade, said out loud: the row is for this exact project and
        is being looked at within a minute of the tab starting, so it is
        this tab's conversation. Refusing it would lose the feature outright
        on a column whose format nobody has seen written yet."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.summaries_db(home, [(self.ID, "whenever it was", project)])
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home), project=project) == self.ID

    def test_a_database_with_neither_table_teaches_nothing(self, sticky,
                                                           tmp_path):
        """Both profiles read a database another program owns, and either
        one is free to rename the table under us."""
        home = tmp_path / "home"
        self.summaries_db(home, [], table="conversations")
        assert sticky.discover_session(self.summaries_profile(sticky),
                                       self.LAUNCH,
                                       str(home),
                                       project=str(tmp_path)) == ""

    def test_a_profile_that_names_no_columns_asks_nothing(self, sticky,
                                                          tmp_path):
        """A glob is half the question. Until the four names beside it are
        known there is no sentence to ask, and a half-filled profile learns
        nothing rather than guessing at a schema."""
        home = tmp_path / "home"
        project = str(tmp_path / "proj")
        self.summaries_db(home, [(self.ID, int(self.LAUNCH + 5), project)])
        half = dataclasses.replace(self.summaries_profile(sticky),
                                   state_db_table="")
        assert sticky.discover_session(half, self.LAUNCH, str(home),
                                       project=project) == ""

    def test_a_wrapped_header_is_looked_into(self, sticky, tmp_path):
        """A header that wraps its id one level down is a shape agents are
        reported to use, and costs one `get` to allow for."""
        home = tmp_path / "home"
        path = self.transcript(home, ".agent/sessions/rollout-a.jsonl",
                               f'{{"type": "session_meta", "payload": '
                               f'{{"id": "{self.ID}"}}}}',
                               when=self.LAUNCH + 5)
        assert sticky.session_id_of(str(path)) == self.ID

    def test_a_profile_with_no_glob_globs_for_nothing(self, sticky, tmp_path):
        """Claude is asked what it calls the conversation, but never by
        globbing: three tabs on one project write into one directory, so the
        newest file there is as likely to be the tab next door. The question
        it answers is the exact one - which of these carries our id - and with
        no id in hand there is nothing to ask at all."""
        home = tmp_path / "home"
        self.transcript(home, f".claude/projects/-a-project/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH + 5)
        assert sticky.CLAUDE.transcript_glob == ""
        assert not sticky.GENERIC.can_discover, "it writes nothing down"
        assert (sticky.discover_session(sticky.CLAUDE, self.LAUNCH, str(home))
                == "")
        told = dataclasses.replace(sticky.GENERIC, session_flag="--session-id",
                                   transcript_glob=".claude/**/*.jsonl")
        assert not told.can_discover, "an agent we name the conversation for"

    def test_the_discovered_id_reopens_the_conversation(self, sticky):
        """What it is for: `resume` hands the agent the id it chose itself."""
        gemini = {"agent": "gemini", "session": "tab-a1b2c3", "continued": True,
                  "agent_session": self.ID, "command": "gemini"}
        assert (sticky.resume_command(gemini)
                == f"gemini --resume {self.ID}")
        codex = {"agent": "codex", "session": "tab-a1b2c3", "continued": True,
                 "agent_session": self.ID, "command": "codex -m gpt"}
        assert (sticky.resume_command(codex)
                == f"codex resume {self.ID} -m gpt"), "a subcommand goes first"

    def test_a_tab_whose_id_was_never_learned_asks_for_the_last_one(
            self, sticky):
        """The fallback, and the gap it closes: a bare `codex` here would be
        a new conversation wearing the old tab's notes."""
        gemini = {"agent": "gemini", "session": "tab-a1b2c3", "continued": True,
                  "command": "gemini"}
        assert sticky.resume_command(gemini) == "gemini --resume"
        codex = {"agent": "codex", "session": "tab-a1b2c3", "continued": True,
                 "command": "codex -m gpt"}
        assert sticky.resume_command(codex) == "codex resume --last -m gpt"
        # And an agent with no way of being asked for one is still not handed
        # a flag it has never heard of.
        generic = {"agent": "generic", "session": "tab-a1b2c3",
                   "continued": True, "command": "aider --model gpt"}
        assert sticky.resume_command(generic) == "aider --model gpt"

    def test_a_line_that_already_names_one_is_left_alone(self, sticky):
        """Including claude's, which is the line every existing tab has."""
        started = {"agent": "gemini", "session": "tab-a1b2c3",
                   "continued": True, "command": "gemini --resume"}
        assert sticky.resume_command(started) == "gemini --resume"
        claude = {"session": "tab-a1b2c3", "continued": True,
                  "command": "claude --continue"}
        assert sticky.resume_command(claude) == "claude --continue"

    def test_the_notes_keep_the_directory_they_were_written_to(
            self, sticky, tmp_path, monkeypatch):
        """The discovered id reopens the conversation and does nothing else.
        Moving the store would strand every note already taken in the tab."""
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(tmp_path / "home"))
        store = sticky.session_dir(str(tmp_path), "tab-a1b2c3")
        sticky.record_window("tab-a1b2c3", project=str(tmp_path),
                             store=store, agent="gemini", continued=True,
                             command="gemini")
        sticky.record_window("tab-a1b2c3", agent_session=self.ID)
        record, = sticky.known_windows()
        assert record["session"] == "tab-a1b2c3", "the key the notes are under"
        assert record["store"] == store
        assert record["agent_session"] == self.ID
        assert sticky.resume_command(record) == f"gemini --resume {self.ID}"


class TestForkingWithoutANameForTheFork:
    """A branch sticky cannot name is still a branch worth opening.

    Claude takes an id for the fork, and the notes hang off it. Codex picks
    its own and never says which, so the fork is remembered the way a tab
    told to continue is: a `tab-` key of ours, the notes under that, and a
    record that admits the id was never ours to know.
    """

    class FakeTmux:
        """Pane options, and enough of `run` to open a window and a sidebar."""

        def __init__(self, panes):
            self.socket = "test"
            self.panes = panes
            self.launched = ""

        def option(self, pane, name):
            return self.panes.get(pane, {}).get(name, "")

        def set_option(self, pane, name, value):
            self.panes.setdefault(pane, {})[name] = value

        def fmt(self, target, template):
            if "session_name" in template:
                return "sticky:2"
            return self.option(target, template[2:-1])

        def formats(self, *asks):
            # What `nudge` asks, to find the sidebar to poke. There is no
            # sidebar process behind this fake, so the answers say as much
            # and the poke is skipped - the notes file is right either way.
            return [self.fmt(target, template) for target, template in asks]

        def run(self, *args):
            if args[0] == "new-window":
                self.launched = args[-1]
                return "%9\n"
            if args[0] == "split-window":
                return "%10\n"
            return ""

        def ok(self, *args):
            return True

        def pane_exists(self, pane):
            return pane in ("%1", "%9", "%10")

    def fork(self, sticky, tmp_path, monkeypatch, agent, command):
        """Fork a pane running `command` on `agent`; the server is a fake."""
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("STICKY_LOCAL", raising=False)
        project = tmp_path / "project"
        project.mkdir()
        origin = sticky.session_dir(str(project), "tab-origin")
        sticky.Store(str(project), directory=origin).save(
            [{"id": "n1", "status": "pending", "note": "carry me",
              "quote": "a line"},
             {"id": "n2", "status": "committed", "note": "already sent",
              "quote": "another line"}])
        tm = self.FakeTmux({"%1": {"@sticky_role": "claude",
                                   "@sticky_agent": agent,
                                   "@sticky_project": str(project),
                                   "@sticky_store": origin,
                                   "@sticky_agent_cmd": command}})
        monkeypatch.setattr(sticky.commands, "Tmux", lambda socket: tm)
        args = argparse.Namespace(socket=None, pane="%1", project=None,
                                  store=None, client=None, quiet=True)
        assert sticky.cmd_fork(args) == 0
        return tm, str(project)

    def test_the_branch_is_asked_for_as_a_subcommand(
            self, sticky, tmp_path, monkeypatch):
        tm, _ = self.fork(sticky, tmp_path, monkeypatch, "codex", "codex")
        assert tm.launched == "codex fork --last"

    def test_the_notes_land_in_a_store_keyed_on_a_name_of_ours(
            self, sticky, tmp_path, monkeypatch):
        tm, project = self.fork(sticky, tmp_path, monkeypatch, "codex", "codex")
        key = tm.panes["%9"]["@sticky_session"]
        assert key.startswith("tab-"), key
        carried = sticky.Store(project,
                               directory=sticky.session_dir(project, key))
        assert [n["note"] for n in carried.load()] == ["carry me"]

    def test_the_record_says_the_id_was_never_ours(
            self, sticky, tmp_path, monkeypatch):
        """And what it remembers is the tab without the branch: re-running
        `codex fork --last` at every restart would cut a fresh copy each
        time."""
        self.fork(sticky, tmp_path, monkeypatch, "codex", "codex")
        record, = [r for r in sticky.known_windows()
                   if r["session"].startswith("tab-")]
        assert record["continued"] is True
        assert record["agent"] == "codex"
        assert record["command"] == "codex"
        assert "agent_session" not in record, "codex has not said one yet"
        # Nothing knows which conversation the branch became, so the tab
        # comes back on the last one in this directory rather than on a bare
        # `codex`, which would be a new conversation wearing the old notes.
        assert sticky.resume_command(record) == "codex resume --last"

    def test_the_claude_fork_is_the_line_it_always_was(
            self, sticky, tmp_path, monkeypatch):
        """The same code path, on the profile every existing tab uses."""
        tm, _ = self.fork(sticky, tmp_path, monkeypatch, "claude",
                          "claude --model opus")
        session = tm.panes["%9"]["@sticky_session"]
        assert (tm.launched == f"claude --model opus --continue "
                               f"--fork-session --session-id {session}")
        assert not session.startswith("tab-"), "claude takes an id from us"
        assert (tm.panes["%9"]["@sticky_agent_cmd"]
                == f"claude --model opus --session-id {session}")
        record, = sticky.known_windows()
        assert record["continued"] is False
        assert (sticky.resume_command(record)
                == f"claude --model opus --resume {session}")


class TestNamingTheAgentFirst:
    """`sticky codex .` - the same as `start --agent codex .`, read aloud."""

    def start_args(self, sticky, monkeypatch, argv):
        """What `cmd_start` would have been handed, without starting one."""
        seen = {}
        monkeypatch.setattr(sticky.cli, "cmd_start",
                            lambda args: seen.update(vars(args)) or 0)
        assert sticky.main(["sticky", *argv]) == 0
        return seen

    def test_a_known_agent_in_front_starts_a_tab_on_it(
            self, sticky, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = self.start_args(sticky, monkeypatch, ["codex", "."])
        assert (seen["agent"], seen["dir"]) == ("codex", ".")
        assert seen["claude_args"] == []

    def test_what_follows_is_still_the_agent_s_own(
            self, sticky, monkeypatch, tmp_path):
        """The agent's arguments go after `--`, exactly as they do on
        `start`: putting the profile in front changes nothing else."""
        monkeypatch.chdir(tmp_path)
        seen = self.start_args(sticky, monkeypatch,
                               ["claude", ".", "--", "--model", "opus"])
        assert (seen["agent"], seen["dir"]) == ("claude", ".")
        assert seen["claude_args"] == ["--model", "opus"]

    def test_a_directory_really_called_claude_wins(
            self, sticky, monkeypatch, tmp_path):
        """A path you can see in front of you beats a word we happen to
        know, so the line goes on meaning what it meant before: the
        directory is the agent's argument, and no profile is chosen."""
        (tmp_path / "claude").mkdir()
        monkeypatch.chdir(tmp_path)
        seen = self.start_args(sticky, monkeypatch, ["claude"])
        assert seen["agent"] is None
        assert seen["claude_args"] == ["claude"]

    def test_the_socket_may_still_come_first(
            self, sticky, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = self.start_args(sticky, monkeypatch,
                               ["--socket", "s1", "gemini"])
        assert (seen["agent"], seen["socket"]) == ("gemini", "s1")

    def test_a_subcommand_is_still_a_subcommand(
            self, sticky, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = self.start_args(sticky, monkeypatch, ["new", "--agent", "codex"])
        assert seen["agent"] == "codex" and seen["command"] == "new"


class TestCompletingAPath:
    """Tab in the new-tab prompt, which tmux's command-prompt cannot do."""

    @pytest.fixture
    def tree(self, tmp_path):
        for name in ("alpha", "alpine", "beta"):
            (tmp_path / name).mkdir()
        (tmp_path / "alpha-file").write_text("not a directory")
        return tmp_path

    def test_one_match_is_finished_and_opened(self, sticky, tree):
        grown, options = sticky.complete_path(f"{tree}/b")
        assert grown == f"{tree}/beta/"
        assert options == ["beta"]

    def test_several_grow_as_far_as_they_agree(self, sticky, tree):
        grown, options = sticky.complete_path(f"{tree}/a")
        assert grown == f"{tree}/alp"
        assert options == ["alpha", "alpine"]

    def test_a_file_is_never_a_project(self, sticky, tree):
        grown, options = sticky.complete_path(f"{tree}/alpha-f")
        assert (grown, options) == (f"{tree}/alpha-f", [])

    def test_a_tilde_survives_being_completed(self, sticky, monkeypatch, tree):
        monkeypatch.setenv("HOME", str(tree))
        grown, _ = sticky.complete_path("~/bet")
        assert grown == "~/beta/", "the path is spliced onto what was typed"


class TestQuitting:
    def test_quit_is_a_command_of_ours(self, sticky):
        assert "quit" in sticky.build_parser().sticky_commands

    def test_resume_can_take_just_the_last_set(self, sticky):
        args = sticky.build_parser().parse_args(["resume", "--last"])
        assert args.last and not args.all

    def test_the_send_key_presses_enter_too(self, tmp_path_factory):
        home = tmp_path_factory.mktemp("send-home")
        subprocess.run(
            [sys.executable, str(LAUNCHER), "reload",
             "--socket", "sticky-no-such-server"],
            env={**os.environ, "STICKY_HOME": str(home)},
            capture_output=True, text=True)
        config = (home / "tmux.conf").read_text().split("\n")
        lower = [line for line in config if line.startswith("bind s ")]
        upper = [line for line in config if line.startswith("bind S ")]
        assert lower and "--send" not in lower[0]
        assert upper and "--send" in upper[0]


class TestTheSizeATabIsOpenedAt:
    """A tab is opened detached and draws before anybody looks at it.

    tmux sizes a window nobody has seen by `default-size`, which is 80x24,
    and an agent draws its replay into whatever it is given. Since tmux
    never reflows history, 80 columns is what those lines stay wrapped at
    however wide the window becomes later - so a note quoting a full-width
    line has nothing left to match.
    """

    class FakeTmux:
        def __init__(self, clients=""):
            self.socket, self.clients, self.set = "test", clients, []

        def run(self, *args):
            return self.clients if args[0] == "list-clients" else ""

        def ok(self, *args):
            self.set.append(args)
            return True

    def test_it_takes_the_room_a_client_really_has(self, sticky):
        tm = self.FakeTmux("@1\t56\t213\t2\n")
        assert sticky.room_for_new_windows(tm) == (213, 54), "less the status"

    def test_the_largest_client_wins(self, sticky):
        tm = self.FakeTmux("@1\t56\t213\ton\n@2\t30\t100\ton\n")
        assert sticky.room_for_new_windows(tm) == (213, 55)

    def test_with_nobody_attached_it_asks_the_terminal(self, sticky,
                                                       monkeypatch):
        """`resume` typed in a shell starts the server itself: there is no
        client yet, and the terminal it was typed in is the only thing that
        knows how much room there is."""
        monkeypatch.setattr(sticky.config, "terminal_size", lambda: (120, 40))
        assert sticky.room_for_new_windows(self.FakeTmux()) == (120, 40)

    def test_the_option_is_held_at_that(self, sticky):
        tm = self.FakeTmux("@1\t56\t213\t2\n")
        assert sticky.use_client_size(tm) == ("213", "54")
        assert ("set-option", "-g", "default-size", "213x54") in tm.set


class TestTheHooksAreHungOnce:
    """The row-count hook is appended, because two of the hooks it goes on
    carry one of their own - and appending is what has to be done at most
    once. It runs every time a tab opens, so a blind append leaves a copy
    per tab: eleven tabs in, opening the twelfth walked every window eleven
    times over to answer a question with one answer.
    """

    class FakeTmux:
        """A server that remembers what its hooks were set to."""

        def __init__(self, hooks=None):
            self.socket = "test"
            self.hooks = dict(hooks or {})
            self.sent = []

        def run(self, *args):
            if args[0] == "show-options":
                name = args[-1]
                if name not in self.hooks:
                    raise RuntimeError(f"no such option: {name}")
                return "\n".join(f"{name}[{n}] {line}" for n, line
                                  in enumerate(self.hooks[name]))
            return ""

        def ok(self, *args):
            self.sent.append(args)
            if args[0] == "set-hook":
                flags, when = args[1], args[2]
                if "u" in flags:
                    self.hooks.pop(when, None)
                elif "a" in flags:
                    self.hooks.setdefault(when, []).append(args[3])
                else:
                    self.hooks[when] = [args[3]]
            return True

        def fmt(self, target, template):
            return ""

    ROWS = ("after-new-window", "window-unlinked", "window-linked",
            "client-attached", "session-window-changed")

    def mine(self, sticky, tm, when):
        return [line for line in tm.hooks.get(when, [])
                if sticky.HAS_OTHERS in line]

    def test_arriving_wakes_only_the_tab_you_arrived_at(self, sticky):
        """Nine sidebars woken at once is nine processes and two dozen tmux
        calls through a single-threaded server in the same instant. The
        other eight tabs did not move."""
        tm = self.FakeTmux()
        sticky.install_hooks(tm)
        arrive = "\n".join(tm.hooks["session-window-changed"])
        assert "--wake" in arrive and "#{window_id}" in arrive, arrive

    def test_arriving_at_a_tab_is_heard_however_you_got_there(self, sticky):
        """`after-select-window` misses the mouse: tmux's own binding for
        clicking a tab is `switch-client`, which changes the window without
        selecting one. The tab then came up with the sidebar of the tab you
        left, until that agent next printed."""
        tm = self.FakeTmux()
        sticky.install_hooks(tm)
        arrive = tm.hooks["session-window-changed"]
        assert any("fit" in line for line in arrive), arrive
        assert any(sticky.HAS_OTHERS in line for line in arrive), arrive

    def test_the_hook_it_used_to_be_on_is_given_back(self, sticky):
        """Left there it would do the arrival twice for every keystroke."""
        tm = self.FakeTmux({"after-select-window": [
            sticky.ARRIVE_HOOK.format(binary=sticky.self_path())]})
        sticky.install_hooks(tm)
        assert not tm.hooks.get("after-select-window")

    def test_but_not_a_hook_of_your_own(self, sticky):
        yours = "run-shell -b 'my-own-thing'"
        tm = self.FakeTmux({"after-select-window": [
            sticky.ARRIVE_HOOK.format(binary=sticky.self_path()), yours]})
        sticky.install_hooks(tm)
        assert tm.hooks["after-select-window"] == [
            sticky.ARRIVE_HOOK.format(binary=sticky.self_path()), yours]

    def test_opening_tab_after_tab_leaves_one_copy(self, sticky):
        tm = self.FakeTmux()
        for _ in range(5):                   # five tabs, five installs
            sticky.install_hooks(tm)
        for when in self.ROWS:
            assert len(self.mine(sticky, tm, when)) == 1, when

    def test_copies_from_before_are_collapsed(self, sticky):
        """Self-healing, so the machine does not have to be restarted to
        be rid of them."""
        tm = self.FakeTmux({"window-linked": [sticky.ROWS_HOOK] * 11})
        sticky.install_hooks(tm)
        assert tm.hooks["window-linked"] == [sticky.ROWS_HOOK]

    def test_an_override_of_your_own_is_not_collapsed(self, sticky):
        """The hook is shared, and yours is not ours to throw away: it is
        left alone, copies and all."""
        yours = "run-shell -b 'my-own-thing'"
        tm = self.FakeTmux({"window-linked": [sticky.ROWS_HOOK, yours]})
        sticky.install_hooks(tm)
        assert yours in tm.hooks["window-linked"]
        assert len(self.mine(sticky, tm, "window-linked")) == 1


class Pane:
    """A tmux stand-in that remembers what it was told.

    `carry_on` talks to exactly two things - the pane's option and its
    keyboard - so the whole of what it does is visible in a list of calls.
    """

    def __init__(self):
        self.calls = []

    def ok(self, *words):
        self.calls.append(words)

    def typed(self):
        """The words sent to the agent, in order."""
        return [w[-1] for w in self.calls if w[:2] == ("send-keys", "-t")
                or (len(w) > 3 and w[3] == "-l")]

    def armed(self):
        """What the pane's count says now: a number, or None if unset."""
        for words in reversed(self.calls):
            if "@sticky_autocontinue" in words:
                return None if "-u" in words else words[-1]
        return "untouched"


class TestMiddleClickPastes:
    """The X11 habit: mark something, middle-click, it appears. tmux binds
    this itself and almost gets it right - its condition forwards the click
    whenever the pane is in a mode, and sticky leaves the pane in copy mode
    on purpose so what you selected stays lit. So the gesture meant to
    follow a selection was the one a selection disabled."""

    def config(self, sticky, tmp_path, monkeypatch):
        monkeypatch.setenv("STICKY_HOME", str(tmp_path))
        with open(sticky.write_config("/bin/sticky-chat"),
                  encoding="utf-8") as fh:
            return fh.read()

    def rows(self, sticky, tmp_path, monkeypatch, table):
        # By token, not by prefix: the table names are padded so the keys
        # line up in the file, and `copy-mode` is a prefix of
        # `copy-mode-vi`.
        want = ["bind", "-T", table, "MouseDown2Pane"]
        return [row for row in
                self.config(sticky, tmp_path, monkeypatch).splitlines()
                if row.split()[:4] == want]

    def test_it_is_bound_where_a_selection_leaves_you(self, sticky, tmp_path,
                                                      monkeypatch):
        """Both copy-mode tables, because `mode-keys` follows your EDITOR
        and the pane is in one of them after every drag."""
        for table in ("copy-mode", "copy-mode-vi"):
            rows = self.rows(sticky, tmp_path, monkeypatch, table)
            assert len(rows) == 1, table
            assert "paste-buffer -p" in rows[0]

    def test_copy_mode_is_left_on_the_way_through(self, sticky, tmp_path,
                                                   monkeypatch):
        """Like a printable key typed while scrolled back: the paste belongs
        at the prompt, which is where it lands."""
        row = self.rows(sticky, tmp_path, monkeypatch, "copy-mode")[0]
        assert row.index("cancel") < row.index("paste-buffer")

    def test_a_pane_drawing_its_own_screen_still_gets_the_click(
            self, sticky, tmp_path, monkeypatch):
        """An agent with mouse reporting on has its own idea about button
        two, and the wheel bindings make the same exception."""
        row = self.rows(sticky, tmp_path, monkeypatch, "copy-mode")[0]
        assert "#{||:#{alternate_on},#{mouse_any_flag}}" in row
        assert "send-keys -M" in row

    def test_the_sidebar_is_not_pasted_into(self, sticky, tmp_path,
                                            monkeypatch):
        """It reads every character as a command, which is how a batch of
        notes once went into the wrong conversation."""
        row = self.rows(sticky, tmp_path, monkeypatch, "root")[0]
        assert "@sticky_role},sidebar" in row


class TestSendingABatchToAnotherTab:
    """`C-g 1`-`9` in the sidebar. It was a bare digit there, and a bare
    digit in the sidebar is a trap: letters are forwarded to the agent so
    you can start typing at the pane your eye is on, and a digit was
    intercepted instead - pasting the whole pending batch into another
    conversation with nothing asked and nothing said. Typing a message with
    a `2` in it sent four notes into another tab."""

    def test_a_digit_is_no_longer_the_sidebars_own_key(self, sticky):
        for digit in "123456789":
            assert digit not in sticky.SIDEBAR_KEYS, digit
            assert sticky.typing_through(digit), \
                "it goes to the agent, like any other character typed here"

    def test_the_prefix_sends_from_the_sidebar(self, sticky):
        line = next(row for row in
                    sticky.send_to_tab_keys("/bin/sticky-chat").splitlines()
                    if row.startswith("bind 4 "))
        assert "@sticky_role},sidebar" in line
        assert "commit --to-tab 4" in line

    def test_and_still_selects_the_window_anywhere_else(self, sticky):
        """The cost of taking `C-g <digit>` outright would have been a key
        people use all day, to buy one they use now and then."""
        for number in (1, 9):
            line = next(row for row in
                        sticky.send_to_tab_keys().splitlines()
                        if row.startswith(f"bind {number} "))
            assert line.rstrip().endswith(f"'select-window -t :={number}'")

    def test_every_digit_is_bound_and_none_is_zero(self, sticky):
        keys = sticky.send_to_tab_keys().splitlines()
        assert [k.split()[1] for k in keys] == list("123456789")

    def test_the_binary_is_not_left_as_a_marker(self, sticky, tmp_path,
                                                monkeypatch):
        """This text is spliced in after the template's own `@BIN@` has been
        replaced, so a marker left here reaches the file unreplaced - and
        nine bindings pointed at a program called `@BIN@`."""
        monkeypatch.setenv("STICKY_HOME", str(tmp_path))
        with open(sticky.write_config("/bin/sticky-chat"),
                  encoding="utf-8") as fh:
            text = fh.read()
        assert "@BIN@" not in text
        assert "bind 2 if -F" in text


class TestTheOrderTabsComeBackIn:
    """`resume` used to reopen a set in the order the tabs had last been
    *touched*, because that is the order `known_windows` answers in - right
    for a list you are choosing from, wrong for putting a set back. A
    session came back shuffled, and the tab you reach for by its number was
    somewhere else every time."""

    def records(self, *pairs):
        return [{"session": f"s{n}", "name": f"p{n}", "index": i,
                 "created": 1000 + n, "last_seen": 2000 - n}
                for n, i in pairs]

    def test_it_is_the_order_they_sat_in(self, sticky):
        shuffled = self.records((1, 4), (2, 3), (3, 2), (4, 1))
        assert [r["name"] for r in sticky.in_tab_order(shuffled)] == \
            ["p4", "p3", "p2", "p1"], "by index, whatever the timestamps say"

    def test_a_record_from_before_this_was_written_down(self, sticky):
        """Every record on the machine this was added to had no index. They
        go after the ones that do, oldest first - the order they were made
        in, which is the closest thing to where they were."""
        knows = self.records((1, 2), (2, 1))
        blind = [{"session": "old1", "name": "old1", "created": 10},
                 {"session": "old2", "name": "old2", "created": 5}]
        out = [r["name"] for r in sticky.in_tab_order([*knows, *blind])]
        assert out == ["p2", "p1", "old2", "old1"], out

    def test_nothing_is_lost_or_duplicated(self, sticky):
        mixed = [*self.records((1, 3), (2, 1)),
                 {"session": "x", "name": "x"}]
        out = sticky.in_tab_order(mixed)
        assert sorted(r["session"] for r in out) == ["s1", "s2", "x"]
        assert len(out) == 3

    def test_the_chooser_still_lists_newest_first(self, sticky, tmp_path,
                                                  monkeypatch):
        """`C-g o` is a list to pick from, where what you closed a moment
        ago belongs at the top. Only the reopening of a *set* is in tab
        order."""
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(tmp_path))
        for n, seen in ((1, 100), (2, 300), (3, 200)):
            sticky.record_window(f"s{n}", touch=False, name=f"p{n}",
                                 index=n, last_seen=seen)
        assert [r["name"] for r in sticky.known_windows()] == \
            ["p2", "p3", "p1"]


class TestCopyingWithCommandC:
    """Command is not a modifier tmux can see - macOS terminals consume it -
    so a terminal is configured to send Esc-c instead, which arrives as
    `M-c`. Measured on tmux next-3.8: both `\\x1bc` and the kitty-protocol
    form `\\x1b[99;9u` come through as M-c, and `user-keys` never sees
    either, which is why there is no user-keys entry here."""

    def config(self, sticky, tmp_path, monkeypatch):
        monkeypatch.setenv("STICKY_HOME", str(tmp_path))
        return open(sticky.write_config("/bin/sticky-chat"),
                    encoding="utf-8").read()

    def test_it_copies_in_both_copy_mode_tables(self, sticky, tmp_path,
                                                monkeypatch):
        text = self.config(sticky, tmp_path, monkeypatch)
        # Whitespace-insensitive: the two bindings are padded so the key
        # names line up in the file.
        rows = [" ".join(row.split()) for row in text.splitlines()]
        for table in ("copy-mode", "copy-mode-vi"):
            assert f"bind -T {table} M-c send -X copy-pipe-no-clear" in \
                " | ".join(rows), table

    def root(self, sticky, tmp_path, monkeypatch):
        return next(row for row in
                    self.config(sticky, tmp_path, monkeypatch).splitlines()
                    if row.startswith("bind -T root M-c"))

    def test_the_note_prompt_gets_the_key_it_already_knows(self, sticky,
                                                           tmp_path,
                                                           monkeypatch):
        """The prompt's copy key is C-c, so M-c is translated rather than
        taught."""
        line = self.root(sticky, tmp_path, monkeypatch)
        assert "@sticky_role},note" in line and "send-keys C-c" in line

    def test_anywhere_else_the_agent_still_gets_it(self, sticky, tmp_path,
                                                   monkeypatch):
        """Taking a key from the agent is a cost, and this one is avoidable:
        the pane that is not a prompt and not the sidebar is handed M-c."""
        line = self.root(sticky, tmp_path, monkeypatch)
        assert "send-keys M-c" in line
        assert "sidebar" in line, "and the sidebar is the one that is not"

    def test_no_format_sits_inside_the_braces(self, sticky, tmp_path,
                                              monkeypatch):
        """The bug this binding shipped with, and the reason it did nothing.

        tmux does not expand `#{...}` in a command's arguments inside a
        braced list, so `send-keys -t "#{pane_id}" C-c` handed send-keys
        those fifteen characters as a target, matched no pane, and failed
        without a word: command-C in the note prompt did nothing at all. A
        key binding's default target is already the pane the key arrived at,
        so there is no `-t` to get wrong - and the bindings that really do
        need a format put it in a quoted string, which is expanded, rather
        than in braces.

        An `if -F` condition inside braces *is* expanded, because that is
        what `-F` asks for - which is why the else branch here works and the
        target did not, and why this looks for a target rather than for a
        format.
        """
        line = self.root(sticky, tmp_path, monkeypatch)
        assert '-t "#{' not in line, \
            "a format in a target inside braces is handed over literally"
        assert " -t " not in line, \
            "and there is no target to get wrong: the key's own pane is it"

    def test_the_generated_config_holds_no_escape_byte(self, sticky, tmp_path,
                                                       monkeypatch):
        """The comment explains the terminal's side, and `\\x1b` in a
        template that is not raw is an escape: it wrote a real Esc into the
        file, inside a comment, where nothing would ever have noticed."""
        assert "\x1b" not in self.config(sticky, tmp_path, monkeypatch)


class TestWhichLinesGetLitWhileYouReadBack:
    """A copy-mode search costs tmux the whole history and marks every match
    in it - a second or four on a pane with two hundred thousand rows, with
    the server blocked and every other pane with it. So what is asked for is
    bounded twice over: only quotes that could land on the rows you are
    reading, and only as many of them as the history can afford."""

    def notes(self, **over):
        """As a store really writes them: `created` is an ISO-8601 string.

        It was a number in the first version of this test, which is why a
        `float()` of it shipped and put a traceback in the pane on every
        scroll. One of them is a number anyway, because a store old enough
        may hold either and neither may raise.
        """
        made = []
        for i in range(12):
            made.append({"id": f"n{i}", "quote": f"a quoted line number {i}",
                         "note": "x", "status": "pending",
                         "created": f"2026-10-06T00:{i:02d}:24", **over})
        made[4]["created"] = 1_760_000_000
        return made

    def test_a_quote_that_is_not_on_the_rows_is_not_searched_for(self,
                                                                 sticky):
        """The ordinary case, and the one that used to cost the most: a
        pattern matching nothing is the most expensive search there is."""
        near = "a quoted line number 3\nand some other output"
        pattern = sticky.mark_pattern(self.notes(), near)
        assert pattern == "a quoted line number 3"

    def test_nothing_on_the_rows_asks_for_nothing(self, sticky):
        assert sticky.mark_pattern(self.notes(), "unrelated output") == ""

    def test_the_newest_come_first_and_the_sent_ones_last(self, sticky):
        """What is worth lighting is what you are still working on."""
        notes = self.notes()
        notes[0]["status"] = "committed"      # oldest anyway
        notes[11]["status"] = "committed"     # newest, but sent
        near = "\n".join(n["quote"] for n in notes)
        got = sticky.mark_pattern(notes, near, 3).split("|")
        assert got == ["a quoted line number 10", "a quoted line number 9",
                       "a quoted line number 8"], got

    def test_a_time_it_cannot_read_is_not_a_traceback(self, sticky):
        """Whatever is in the field, a scroll must not raise: the hook runs
        on every copy-mode entry and tmux puts what it printed on the
        screen."""
        notes = self.notes()
        notes[3]["created"] = "whenever"
        notes[5]["created"] = None
        del notes[7]["created"]
        near = "\n".join(n["quote"] for n in notes)
        assert sticky.mark_pattern(notes, near, 4).count("|") == 3

    def test_the_count_is_capped(self, sticky):
        near = "\n".join(n["quote"] for n in self.notes())
        assert len(sticky.mark_pattern(self.notes(), near, 2).split("|")) == 2
        assert len(sticky.mark_pattern(self.notes(), near, 1).split("|")) == 1
        assert len(sticky.mark_pattern(self.notes(), near, 0).split("|")) == 1, \
            "zero is one: a budget of none still lights the newest"

    def test_how_many_a_history_affords(self, sticky):
        """Eight quotes over sixty thousand rows measured at nearly a second
        of frozen server, and one over the same rows at a quarter of it."""
        def affords(rows):
            budget, most = sticky.MARK_BUDGET, sticky.MARK_QUOTES
            return (budget // rows if rows > budget // most else most)
        assert affords(500) == sticky.MARK_QUOTES, "a short history, all of them"
        assert affords(3000) == sticky.MARK_QUOTES
        assert affords(60000) < sticky.MARK_QUOTES, "a long one, fewer"
        assert affords(21367) >= affords(60000)
        assert sticky.MARK_BUDGET < 194327, \
            "and the longest go unlit rather than freezing the session"

    def test_a_deleted_note_lights_nothing(self, sticky):
        notes = self.notes()
        for note in notes:
            note["deleted"] = True
        near = "\n".join(n["quote"] for n in notes)
        assert sticky.mark_pattern(notes, near) == ""

    def test_the_whole_store_still_works_without_a_screen(self, sticky):
        """`within` empty means "do not know", not "nothing matches" - the
        capture can fail, and a tab that lit nothing after a failed read
        would look like a tab with no notes."""
        assert sticky.mark_pattern(self.notes()).count("|") == \
            sticky.MARK_QUOTES - 1


class TestStoppingWhatATabIsRunning:
    """`C-g k`. It goes by parentage rather than by process group, because
    the runaway that prompted it sat in a group of its own and never heard
    the terminal's Ctrl-C."""

    def test_it_finds_a_child_and_a_grandchild(self, sticky):
        """Deepest first, so a parent cannot be told its child has gone and
        start another one."""
        deep = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys; "
             "subprocess.run([sys.executable, '-c', 'import time;"
             "time.sleep(30)'])"])
        try:
            for _ in range(40):
                found = sticky.running_under(os.getpid())
                if len(found) >= 2:
                    break
                time.sleep(0.1)
            pids = [pid for pid, _ in found]
            assert deep.pid in pids, "the child"
            assert len(pids) >= 2, f"and its own child: {found}"
            assert pids.index(deep.pid) > 0, "the parent comes last"
        finally:
            deep.kill()
            deep.wait()

    def test_the_pane_s_own_process_is_never_in_the_list(self, sticky):
        """Killing that is killing the tab, which tmux has its own key
        for."""
        assert os.getpid() not in [pid for pid, _
                                   in sticky.running_under(os.getpid())]

    def test_it_knows_which_processes_are_itself(self, sticky):
        """Run from inside the pane it is about to signal - your own shell,
        or the agent's - it is a descendant of that very tree, and without
        this it kills itself halfway through."""
        mine = sticky.ours(os.getpid())
        assert os.getpid() in mine
        assert os.getppid() in mine, "and what it hangs off"
        assert 1 not in mine, "stopping short of init"


class TestCarryingATabOn:
    """`C-g a`. The clock below waits out a limit nobody chose; this is a
    standing instruction to say the same word when a turn ends of its own
    accord, and the whole difficulty is telling a turn of work apart from a
    word echoed back by something that has stopped listening."""

    def test_it_says_the_word_and_counts_down(self, sticky):
        pane = Pane()
        left, saw, said = sticky.carry_on(pane, "%1", 5, "a screen", "")
        assert left == 4 and saw == "a screen"
        assert pane.typed() == [sticky.CONTINUE_WORD, "Enter"]
        assert pane.armed() == "4"
        assert "4 left" in said

    def test_the_last_of_an_arm_takes_the_count_off_the_pane(self, sticky):
        """Nothing left to say is nothing to remember, and a count of zero
        sitting on a pane reads as armed to anybody looking at it."""
        pane = Pane()
        left, _, _ = sticky.carry_on(pane, "%1", 1, "a screen", "")
        assert left == 0 and pane.armed() is None

    def test_without_end_stays_without_end(self, sticky):
        pane = Pane()
        left, _, said = sticky.carry_on(pane, "%1", sticky.AUTO_FOREVER,
                                        "a screen", "before")
        assert left == sticky.AUTO_FOREVER
        assert pane.armed() == str(sticky.AUTO_FOREVER)
        assert "without end" in said

    def test_a_screen_that_did_not_move_spends_the_arm(self, sticky):
        """An agent whose window redraws to exactly what it was did nothing
        with the word, and an arm that cannot tell would type into it every
        few seconds for as long as the count allows."""
        pane = Pane()
        left, saw, said = sticky.carry_on(pane, "%1", sticky.AUTO_FOREVER,
                                          "same", "same")
        assert left == 0 and saw == ""
        assert not pane.typed(), "and nothing was sent"
        assert pane.armed() is None, "off the pane, not just out of the loop"
        assert "achieved nothing" in said

    def test_a_turn_that_did_no_work_spends_it_too(self, sticky):
        """The other half, and the one the screen cannot catch: an agent that
        only echoes the word has a screen that *has* changed - by the width
        of the word - and gives itself away by stopping a fifth of a second
        later."""
        pane = Pane()
        left, _, said = sticky.carry_on(pane, "%1", 3, "new", "old",
                                        worked=False)
        assert left == 0 and pane.armed() is None
        assert not pane.typed()
        assert "achieved nothing" in said

    def test_the_first_stop_is_never_judged(self, sticky):
        """It is somebody's own turn ending, and there is nothing of ours to
        compare it with."""
        pane = Pane()
        left, _, _ = sticky.carry_on(pane, "%1", 2, "a screen", "",
                                     worked=False)
        assert left == 1 and pane.typed()[:1] == [sticky.CONTINUE_WORD]

    def test_what_the_rows_say(self, sticky):
        assert sticky.auto_line(0) == ""
        assert "5 more times" in sticky.auto_line(5)
        assert "1 more time" in sticky.auto_line(1)
        assert "until you say otherwise" in sticky.auto_line(
            sticky.AUTO_FOREVER)
        assert "\u21ba5" in sticky.footer_text([], auto=5)
        assert "\u21ba\u221e" in sticky.footer_text([],
                                                  auto=sticky.AUTO_FOREVER)
        assert "\u21ba" not in sticky.footer_text([]), "and nothing when unarmed"


class TestATabThatSetsItsOwnClock:
    """An agent that runs out of turns says when it will have them back, and
    a tab can wait for that and send one word. What it must not do is take
    a paragraph *about* limits for a notice and type into a tab that was
    waiting for nothing.
    """

    class FakeTmux:
        def __init__(self, said=""):
            self.socket, self.said = "test", said

        def run(self, *args):
            return self.said

    NOTICE = ("Usage limit reached \u00b7 continuing automatically at 4am "
              "\u00b7 esc or type to cancel")
    PLAIN = "5-hour limit reached \u00b7 resets 8pm"
    PROSE = "the usage limit resets at 8pm, so the clock waits until then"

    def transcript(self, tmp_path, *records):
        """A Claude transcript where the agent keeps its own record."""
        home = tmp_path / "home"
        where = home / ".claude" / "projects" / "-tmp-proj"
        where.mkdir(parents=True, exist_ok=True)
        (where / "abc.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n")
        return str(home)

    def notice(self, content):
        return {"type": "system", "subtype": "informational",
                "level": "notice", "content": content}

    def message(self, text):
        return {"type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]}}

    def test_a_notice_in_the_transcript_is_read(self, sticky, tmp_path):
        home = self.transcript(tmp_path, self.message(self.PROSE),
                              self.notice(self.PLAIN))
        said, itself = sticky.limit_notice(sticky.CLAUDE, "/tmp/proj", "abc",
                                          home=home)
        assert said == self.PLAIN and not itself
        assert sticky.reset_notice([said])[0], "and a time comes out of it"

    def test_prose_about_a_limit_is_not_a_notice(self, sticky, tmp_path):
        """The words are the same; the record it is kept in is not. This is
        the distinction no reading of a screen can make, and the one that
        had a tab typing into a conversation waiting for nothing."""
        home = self.transcript(tmp_path, self.message(self.PROSE))
        assert sticky.limit_notice(sticky.CLAUDE, "/tmp/proj", "abc",
                                   home=home) == ("", False)

    def test_an_agent_that_carries_on_by_itself_says_so(self, sticky, tmp_path):
        """Claude Code waits and resumes on its own now, so a clock of ours
        is a second poke at a conversation already coming back."""
        home = self.transcript(tmp_path, self.notice(self.NOTICE))
        said, itself = sticky.limit_notice(sticky.CLAUDE, "/tmp/proj", "abc",
                                           home=home)
        assert itself and "automatically" in said

    def test_the_newest_notice_is_the_one_that_counts(self, sticky, tmp_path):
        home = self.transcript(tmp_path, self.notice(self.PLAIN),
                              self.notice("Usage limit reset \u00b7 continuing "
                                          "automatically"))
        said, itself = sticky.limit_notice(sticky.CLAUDE, "/tmp/proj", "abc",
                                           home=home)
        assert itself, f"the limit has lifted: {said!r}"

    def test_a_profile_that_keeps_no_transcript_is_not_asked(self, sticky,
                                                             tmp_path):
        """Codex prints its notice and writes nothing we can read, so that
        one is still read off the screen."""
        assert sticky.limit_notice(sticky.CODEX, "/tmp/proj", "abc",
                                   home=str(tmp_path)) == ("", False)

    def test_the_path_matches_the_agents_own_naming(self, sticky):
        assert (sticky.transcript_slug("/Users/me/Code/arrange_signature")
                == "-Users-me-Code-arrange-signature")

    def test_a_warning_is_not_a_stop(self, sticky):
        """"You've used 76% of your limit - resets Oct 3 at 4am" says the
        same words a notice does and means the opposite: the agent is
        working, and telling you how much is left. A tab armed on one of
        those sat on a clock for six days."""
        warning = ("You've used 76% of your Fable limit \u00b7 resets Oct 3 "
                   "at 4am (Europe/Berlin)")
        assert not sticky.reset_notice([warning])[0]
        assert sticky.reset_notice([self.PLAIN])[0], "and a stop still reads"

    def test_a_standing_clock_is_dropped_when_its_reason_goes(self, sticky,
                                                             tmp_path):
        """Output scrolls the notice away, a new turn starts, the limit
        lifts - and a clock left standing then sends a word into a
        conversation that is already talking."""
        home = self.transcript(tmp_path, self.notice(self.PLAIN))
        args = (sticky.CLAUDE, "/tmp/proj", "abc")
        # The home is passed in rather than assumed, which is also what a
        # tab in a container does: its agent writes the transcript into a
        # directory of ours, and the reading is the same reading.
        assert sticky.still_limited(*args, "", "", home)
        gone = self.transcript(tmp_path / "gone", self.message(self.PROSE))
        assert not sticky.still_limited(*args, "", "", gone)

    def test_a_screen_agent_is_asked_of_its_screen(self, sticky):
        """Codex keeps no record, so the row that armed the clock has to
        still be on the screen - and a clock nobody can explain is not one
        to throw away on a guess."""
        row = "You've hit your usage limit. resets 13:22"
        assert sticky.still_limited(sticky.CODEX, "/p", "s",
                                    f"other\n{row}\nmore", row)
        assert not sticky.still_limited(sticky.CODEX, "/p", "s", "gone", row)
        assert sticky.still_limited(sticky.CODEX, "/p", "s", "anything", "")

    def test_nothing_arms_when_it_is_turned_off(self, sticky):
        for said in ("off", "OFF", "0", "no", "false"):
            assert not sticky.arms_itself(self.FakeTmux(said)), said

    def test_it_arms_by_default(self, sticky):
        for said in ("", "on", "yes", "anything else"):
            assert sticky.arms_itself(self.FakeTmux(said)), said


class TestTheLastRunOfTabs:
    """Which tabs went away together, read off the stamps their sidebars
    left behind.

    Nothing is written on the way out - that is the point. A reboot, a
    crash or a lid closed on a Friday gives no warning, and those are
    exactly the exits you want your tabs back from. What they do leave is a
    row of `last_seen` stamps that stop within a heartbeat of each other,
    which is what a run is.
    """

    class FakeTmux:
        """A server that answers list-panes and remembers being killed."""

        def __init__(self, panes=""):
            self.socket, self.panes, self.killed = "test", panes, False

        def server_running(self):
            return True

        def run(self, *args):
            return self.panes if args[0] == "list-panes" else ""

        def ok(self, *args):
            self.killed = self.killed or args[0] == "kill-server"
            return True

        def fmt(self, target, template):
            return ""

    def remember(self, sticky, home, monkeypatch, *ages):
        """One record per age, in seconds before now. Newest first.

        Numbered left to right as well, because that is what a real record
        carries and what a set is put back in: `tab-0` is the newest here
        *and* the leftmost, which is the one combination the stamps alone
        cannot tell you.
        """
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(home))
        now = time.time()
        for number, age in enumerate(ages):
            sticky.record_window(
                f"tab-{number}", touch=False, project=str(home),
                name=f"tab-{number}", agent="claude",
                command=f"claude --session-id tab-{number}",
                index=number + 1, last_seen=now - age)
        return [f"tab-{number}" for number in range(len(ages))]

    def test_the_tabs_that_stopped_together_are_the_run(
            self, sticky, tmp_path, monkeypatch):
        """A sidebar stamps its record once a minute, so tabs that were up
        at the same moment are a heartbeat or two apart - and the one you
        closed at lunchtime is hours behind them."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 45, 70, 7 * 3600)
        assert [r["name"] for r in sticky.last_run()] == [
            "tab-0", "tab-1", "tab-2"]

    def test_a_single_tab_is_a_run_of_its_own(
            self, sticky, tmp_path, monkeypatch):
        self.remember(sticky, tmp_path, monkeypatch, 3 * 86400)
        assert [r["name"] for r in sticky.last_run()] == ["tab-0"]

    def test_quitting_does_not_restamp_the_tabs_it_did_not_close(
            self, sticky, tmp_path, monkeypatch):
        """The stamps are the only evidence of which tabs were up. A quit
        that wrote this moment onto every record ever made would file a tab
        closed in August under tonight's run - which is how resume came to
        offer a year of them at once."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 40 * 86400)
        was = {r["session"]: r["last_seen"] for r in sticky.known_windows()}
        tm = self.FakeTmux(f"claude\ttab-0\t{tmp_path}\tclaude\n")
        monkeypatch.setattr(sticky.commands, "Tmux", lambda socket: tm)
        assert sticky.cmd_quit(argparse.Namespace(
            socket=None, yes=True, quiet=True)) == 0
        assert tm.killed
        now = {r["session"]: r for r in sticky.known_windows()}
        assert now["tab-1"]["last_seen"] == was["tab-1"], "left where it was"
        assert now["tab-0"]["last_seen"] > was["tab-0"], "it went just now"
        assert now["tab-0"]["open_at_quit"] is True
        assert [r["session"] for r in sticky.last_run()] == ["tab-0"]

    def resume(self, sticky, monkeypatch, tm, answer, **flags):
        """Run `resume`; returns what was asked, and what was opened."""
        asked, opened = [], []

        def ask(question, choices):
            asked.append(question)
            return answer

        monkeypatch.setattr(sticky.commands, "ask", ask)
        monkeypatch.setattr(sticky.commands, "open_tab",
                            lambda tm, record, command, client:
                            opened.append(record["name"]) or "%9")
        monkeypatch.setattr(sticky.commands, "Tmux", lambda socket: tm)
        args = argparse.Namespace(socket=None, client=None, detach=True,
                                  **{"all": False, "last": False,
                                     "yes": False, **flags})
        assert sticky.cmd_resume(args) == 0
        return asked, opened

    def test_resume_offers_the_last_run_rather_than_everything(
            self, sticky, tmp_path, monkeypatch):
        """One question about the set, because the set is how it went away
        - not one question per tab about every tab there has ever been."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 30, 6 * 3600, 86400)
        asked, opened = self.resume(
            sticky, monkeypatch, self.FakeTmux(), "y")
        assert len(asked) == 1, asked
        assert "2 tabs were open" in asked[0] and "tab-0, tab-1" in asked[0]
        assert opened == ["tab-0", "tab-1"]

    def test_saying_no_opens_nothing(
            self, sticky, tmp_path, monkeypatch):
        self.remember(sticky, tmp_path, monkeypatch, 0, 30)
        assert self.resume(sticky, monkeypatch, self.FakeTmux(), "n")[1] == []

    def test_a_tab_already_on_screen_is_not_offered_again(
            self, sticky, tmp_path, monkeypatch):
        """Reopening it would be a second tab on a live conversation. The
        panes say so, because a record's stamp is kept warm by the very
        sidebar that would make it look closed."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 30)
        asked, opened = self.resume(
            sticky, monkeypatch, self.FakeTmux("tab-0\t\n"), "y")
        assert opened == ["tab-1"]
        assert "1 tab was open" in asked[0]

    def test_the_walk_says_what_its_letters_do(
            self, sticky, tmp_path, monkeypatch, capsys):
        """Four letters at the end of a row are four letters to guess at."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 30)
        self.resume(sticky, monkeypatch, self.FakeTmux(), "p")
        said = capsys.readouterr().out
        for letter, means in (("y", "reopens"), ("n", "skips"),
                              ("a", "all the rest"), ("q", "stops")):
            assert f"{letter} {means}" in said or means in said, said

    def test_all_reaches_back_past_the_last_run(
            self, sticky, tmp_path, monkeypatch):
        """The rarer job: finding one tab from last week. Still asked about
        one at a time, and `a` takes the rest."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 6 * 3600, 86400)
        asked, opened = self.resume(
            sticky, monkeypatch, self.FakeTmux(), "a", all=True)
        assert opened == ["tab-0", "tab-1", "tab-2"]
        assert len(asked) == 1, "`a` means stop asking"

    def test_yes_asks_nothing_at_all(self, sticky, tmp_path, monkeypatch):
        """What a login item runs."""
        self.remember(sticky, tmp_path, monkeypatch, 0, 30, 86400)
        asked, opened = self.resume(
            sticky, monkeypatch, self.FakeTmux(), "n", yes=True)
        assert asked == [] and opened == ["tab-0", "tab-1"]


    def codex_tabs(self, sticky, home, monkeypatch, *found):
        """Codex tabs in one project, newest first, each with the
        conversation id its sidebar read back - or none."""
        monkeypatch.setattr(sticky.store, "STATE_HOME", str(home))
        now = time.time()
        for number, agent_session in enumerate(found):
            sticky.record_window(
                f"tab-{number}", touch=False, project=str(home),
                name=f"tab-{number}", agent="codex", command="codex",
                continued=True, index=number + 1, last_seen=now - number,
                **({"agent_session": agent_session} if agent_session
                   else {}))

    def test_two_tabs_without_an_id_are_one_conversation(
            self, sticky, tmp_path, monkeypatch, capsys):
        """Both would come back as `codex resume --last`, which is the same
        conversation in two tabs. The newer is kept, the other said."""
        self.codex_tabs(sticky, tmp_path, monkeypatch, "", "")
        opened = self.resume(sticky, monkeypatch, self.FakeTmux(), "y")[1]
        assert opened == ["tab-0"]
        assert "left out tab-1" in capsys.readouterr().out

    def test_two_tabs_with_the_same_id_are_one_conversation(
            self, sticky, tmp_path, monkeypatch):
        self.codex_tabs(sticky, tmp_path, monkeypatch, "c0de", "c0de", "f00d")
        opened = self.resume(sticky, monkeypatch, self.FakeTmux(), "y")[1]
        assert sorted(opened) == ["tab-0", "tab-2"]

    def test_tabs_with_ids_of_their_own_all_come_back(
            self, sticky, tmp_path, monkeypatch):
        """And one id-less tab beside them still gets the last
        conversation: nothing else is asking for it."""
        self.codex_tabs(sticky, tmp_path, monkeypatch, "c0de", "f00d", "")
        opened = self.resume(sticky, monkeypatch, self.FakeTmux(), "y")[1]
        assert sorted(opened) == ["tab-0", "tab-1", "tab-2"]

    def test_a_conversation_running_already_is_not_opened_again(
            self, sticky, tmp_path, monkeypatch):
        """Under another record: a pane on screen running the same
        conversation, or any codex pane in the project for a record that
        would ask for the last one there."""
        self.codex_tabs(sticky, tmp_path, monkeypatch, "c0de", "f00d")
        running = f"%5\tclaude\tcodex\ttab-9\tc0de\t{tmp_path}\n"
        opened = self.resume(sticky, monkeypatch,
                             self.FakeTmux(running), "y")[1]
        assert opened == ["tab-1"]

        other = tmp_path / "other"
        other.mkdir()
        self.codex_tabs(sticky, other, monkeypatch, "")
        running = f"%5\tclaude\tcodex\ttab-9\t\t{other}\n"
        assert sticky.conversation_of(sticky.known_windows()[0]) == (
            "codex", os.path.realpath(str(other)), "last")
        _, repeats = sticky.once_each(sticky.known_windows(),
                                         self.FakeTmux(running))
        assert [r["session"] for r in repeats] == ["tab-0"]

    def test_a_new_conversation_collides_with_nothing(self, sticky):
        """A line that opens a fresh one - an agent that cannot be asked
        for an old one - has no key, and is never left out."""
        assert sticky.conversation_of(
            {"agent": "shell", "command": "/bin/zsh", "project": "/"}) == ()

class TestTheDismissingClick:
    """Clicking away from a prompt finishes the note, and nothing else.

    Reaching for the nearest place to click is not the same as asking to
    strike a note out, so the first click is spent on the prompt. The
    question is asked two ways because the answer comes from two
    directions: the prompt may still be there, or the sweep may have got to
    it first and left behind the time it did.
    """

    class FakeTmux:
        def __init__(self, roles="", dismissed=""):
            self.roles, self.dismissed = roles, dismissed

        def run(self, *args):
            if args[0] == "list-panes":
                return self.roles
            if args[0] == "show-options":
                return self.dismissed
            return ""

    def test_a_prompt_that_is_still_open(self, sticky):
        tm = self.FakeTmux(roles="claude\nsidebar\nnote\n")
        assert sticky.dismissing(tm, "%1")

    def test_a_prompt_the_sweep_has_already_closed(self, sticky):
        tm = self.FakeTmux(roles="claude\nsidebar\n",
                           dismissed=str(time.time()))
        assert sticky.dismissing(tm, "%1")

    def test_an_ordinary_click_later_on(self, sticky):
        tm = self.FakeTmux(roles="claude\nsidebar\n",
                           dismissed=str(time.time() - sticky.DISMISS_GRACE - 1))
        assert not sticky.dismissing(tm, "%1")

    def test_a_server_that_has_never_dismissed_one(self, sticky):
        assert not sticky.dismissing(self.FakeTmux(roles="claude\nsidebar\n"),
                                     "%1")


class TestStartAsksWhatItWasNotTold:
    """`sticky-chat start` answered two of a tab's three questions in
    silence: the agent was claude whatever else was installed, and the fence
    was whatever the choice file said. It asks them now - the new-tab
    prompt's own lines, through the same code, with whatever the command line
    already answered printed where it would have been asked.

    The gate is tested as a function of its own because the interesting part
    is which invocations are *not* asked: a key binding through `run-shell`,
    a login item, a script, and anybody who said both answers up front.
    """

    def args(self, **fields):
        return argparse.Namespace(**{"ask_here": False, "no_ask": False,
                                     "agent": None, "agent_cmd": None,
                                     "sandbox": None, **fields})

    def test_a_terminal_with_nothing_said_gets_asked(self, sticky):
        assert sticky.start_asks(self.args(), True) is True

    def test_without_a_terminal_nothing_is_asked(self, sticky):
        """`prompt_line` needs a tty to go raw on, and this is the path a
        key binding, a login item and a script all take."""
        assert sticky.start_asks(self.args(), False) is False

    def test_both_answers_given_leaves_nothing_to_ask(self, sticky):
        assert sticky.start_asks(
            self.args(agent="codex", sandbox="off"), True) is False

    def test_one_answer_given_still_asks_the_other(self, sticky):
        assert sticky.start_asks(self.args(agent="codex"), True) is True
        assert sticky.start_asks(self.args(sandbox="off"), True) is True

    def test_a_command_of_your_own_answers_the_agent_line(self, sticky):
        """`--agent-cmd` is a more specific answer than a profile name, so
        the agent line has nothing left to ask - the fence still does."""
        assert sticky.start_asks(
            self.args(agent_cmd="weird --thing"), True) is True
        assert sticky.start_asks(
            self.args(agent_cmd="weird --thing", sandbox="off"),
            True) is False

    def test_no_ask_is_the_silence_it_used_to_be(self, sticky):
        assert sticky.start_asks(self.args(no_ask=True), True) is False

    def test_the_floating_prompt_asks_whatever_it_is_handed(self, sticky):
        """`--ask-here` is what `C-g c` runs in a pane of its own, and it
        asks about the project as well."""
        assert sticky.start_asks(
            self.args(ask_here=True, agent="codex", sandbox="off"),
            True) is True
        assert sticky.start_asks(self.args(ask_here=True), False) is True

    def asked(self, sticky, monkeypatch, tmp_path, *replies):
        """The prompt's lines, answered in order; returns what it asked."""
        lines = []

        def prompt_line(prompt, initial="", **_):
            lines.append((prompt.strip(), initial))
            return replies[len(lines) - 1]

        monkeypatch.setattr(sticky.commands, "prompt_line", prompt_line)
        monkeypatch.setattr(sticky.commands, "installed_agents",
                            lambda: [sticky.CLAUDE, sticky.SHELL])
        monkeypatch.setattr(sticky.commands, "warm_up", lambda: None)
        # No engine is asked anything, and nothing is written into the state
        # directory you are using: answering the fence line remembers it.
        monkeypatch.setattr(sticky.commands, "unavailable", lambda fence: "")
        monkeypatch.setattr(sticky.sandbox, "STATE_HOME",
                            str(tmp_path / "state"))
        return lines

    def test_the_path_you_typed_is_not_asked_about_again(
            self, sticky, monkeypatch, tmp_path, capsys):
        lines = self.asked(sticky, monkeypatch, tmp_path, ("save", "shell"))
        got = sticky.ask_project(None, None, str(tmp_path),
                                 ask_where=False, fence="off")
        assert got == (str(tmp_path), "shell", "off")
        assert [prompt for prompt, _ in lines] == ["agent>"]
        said = capsys.readouterr().out
        assert f"project> {tmp_path}" in said, "printed where it was not asked"
        assert "sandbox> off" in said

    def test_a_path_that_is_not_there_is_asked_about(self, sticky,
                                                     monkeypatch, tmp_path):
        """Even off the command line: a line with the path still on it is
        how a typo gets fixed, and dying would mean retyping the lot."""
        good = tmp_path / "here"
        good.mkdir()
        lines = self.asked(sticky, monkeypatch, tmp_path,
                           ("save", str(good)), ("save", "shell"))
        got = sticky.ask_project(None, None, str(tmp_path / "nope"),
                                 ask_where=False, fence="off")
        assert got == (str(good), "shell", "off")
        assert [prompt for prompt, _ in lines] == ["project>", "agent>"]

    def test_the_agent_you_named_is_not_asked_about(self, sticky, monkeypatch,
                                                    tmp_path, capsys):
        lines = self.asked(sticky, monkeypatch, tmp_path, ("save", "docker"))
        got = sticky.ask_project(None, None, str(tmp_path),
                                 ask_where=False, agent="codex")
        assert got == (str(tmp_path), "codex", "docker")
        assert [prompt for prompt, _ in lines] == ["sandbox>"]
        assert "agent>   codex" in capsys.readouterr().out

    def test_enter_on_the_agent_line_takes_what_is_on_it(self, sticky,
                                                         monkeypatch,
                                                         tmp_path):
        """Which is the whole of why this is cheap: the common case is the
        keystrokes it always was."""
        self.asked(sticky, monkeypatch, tmp_path, ("save", ""))
        got = sticky.ask_project(None, None, str(tmp_path),
                                 ask_where=False, fence="off")
        assert got[1] == "claude"

    def test_a_name_that_is_not_an_agent_is_asked_again(self, sticky,
                                                        monkeypatch,
                                                        tmp_path, capsys):
        lines = self.asked(sticky, monkeypatch, tmp_path,
                           ("save", "gemeni"), ("save", "gemini"))
        got = sticky.ask_project(None, None, str(tmp_path),
                                 ask_where=False, fence="off")
        assert got[1] == "gemini"
        assert len(lines) == 2, "the line came back with the typo on it"
        assert "no such agent: gemeni" in capsys.readouterr().out

    def test_escape_on_any_line_cancels_the_whole_thing(self, sticky,
                                                        monkeypatch,
                                                        tmp_path):
        """Three values back, every time. Unpacking is what the caller does
        with the answer, and a cancel that returned two of them raised
        ValueError instead of opening nothing."""
        for where, replies in (
                ("project", (("cancel", ""),)),
                ("agent", (("save", str(tmp_path)), ("cancel", ""))),
        ):
            self.asked(sticky, monkeypatch, tmp_path, *replies)
            got = sticky.ask_project(None, None, str(tmp_path / "nope"),
                                     ask_where=False, fence="off")
            assert got == ("", "", ""), f"cancelled at the {where} line"


class TestWhenTmuxWillNotStart:
    """Two things that made the same remote machine look broken.

    A terminal whose terminfo ships with the terminal - Ghostty, kitty,
    WezTerm - leaves `$TERM` naming an entry the machine you ssh into does
    not have, and tmux will not start without one. And the shortcut a bare
    `sticky-chat` takes when the last tabs are worth offering reached
    `cmd_resume` outside the handler, so that refusal arrived as a traceback
    through four frames of ours.
    """

    def test_a_terminal_tmux_does_not_know_says_what_to_do(self, sticky,
                                                           monkeypatch):
        monkeypatch.setenv("TERM", "xterm-ghostty")
        said = sticky.tmux.why("missing or unsuitable terminal: xterm-ghostty")
        assert "infocmp -x xterm-ghostty" in said, "copy the entry over"
        assert "TERM=xterm-256color" in said, "or use one that is there"

    def test_every_other_failure_is_left_as_it_was(self, sticky):
        assert sticky.tmux.why("no server running on /tmp/sticky") == ""
        assert sticky.tmux.why("can't find window: 9") == ""

    def test_the_hint_rides_along_on_the_error(self, sticky, monkeypatch):
        """Which is what the shortcuts print: one line of tmux's own words,
        then what to do about them."""
        monkeypatch.setenv("TERM", "xterm-ghostty")

        class Done:
            returncode = 1
            stdout = ""
            stderr = "missing or unsuitable terminal: xterm-ghostty"

        monkeypatch.setattr(sticky.tmux.subprocess, "run",
                            lambda *a, **k: Done())
        with pytest.raises(RuntimeError) as raised:
            sticky.Tmux("/tmp/none").run("new-session", "-d")
        assert "no terminfo entry" in str(raised.value)

    def test_a_refusal_is_a_message_and_not_a_traceback(self, sticky):
        """`guarded` is what every dispatch goes through now."""
        def boom(_args):
            raise RuntimeError("tmux new-session: missing or unsuitable "
                               "terminal: xterm-ghostty")

        with pytest.raises(SystemExit):
            sticky.guarded(boom, argparse.Namespace())

    def test_what_a_subcommand_returns_comes_back(self, sticky):
        assert sticky.guarded(lambda _args: 3, argparse.Namespace()) == 3


def fake_tmux(tmp_path, client="tmux 3.4", server="3.4", running=True):
    """A `tmux` on PATH that answers the version questions and fails the
    rest the way an old one does - `new-pane` is a command it never heard
    of."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = -V ] && {{ echo "{client}"; exit 0; }}\n'
        'for word in "$@"; do\n'
        f'  case "$word" in\n'
        f'    has-session) exit {0 if running else 1} ;;\n'
        f"    '#{{version}}') echo {server}; exit 0 ;;\n"
        '    new-pane) echo "unknown command: new-pane" >&2; exit 1 ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n")
    script.chmod(0o755)
    return str(bin_dir)


class TestAnOldTmuxIsSaidAtTheStart:
    """tmux 3.4 is what a managed Ubuntu box has, and it has no floating
    panes. Nothing failed at the start: the session came up, and then
    `C-g c` said `returned 1` and a drag over the agent said nothing at
    all. So the version is checked where a person starts, every time, and
    a prompt that cannot open says why on the message line."""

    def test_versions_are_read_by_their_leading_numbers(self, sticky):
        assert sticky.too_old("tmux 3.4")
        assert not sticky.too_old("tmux 3.7c")
        assert not sticky.too_old("tmux next-3.8"), "newer than 3.8"
        assert not sticky.too_old("tmux master"), "unreadable is no reason"

    def test_an_old_tmux_on_the_path_is_named(self, sticky, tmp_path,
                                              monkeypatch):
        monkeypatch.setenv("PATH", fake_tmux(tmp_path, running=False))
        said = sticky.old_tmux("/tmp/none")
        assert "tmux 3.4 is older than tmux 3.7" in said
        assert "~/.local" in said, "and the way out that needs no root"

    def test_an_old_server_under_a_new_client_is_named_too(self, sticky,
                                                          tmp_path,
                                                          monkeypatch):
        """What installing a newer tmux leaves behind until the old server
        is stopped: tmux -V says 3.7c and every prompt still fails."""
        monkeypatch.setenv("PATH", fake_tmux(tmp_path, client="tmux 3.7c"))
        said = sticky.old_tmux("/tmp/none")
        assert "server already running is tmux 3.4" in said
        assert "sticky-chat quit" in said

    def test_a_new_enough_tmux_says_nothing(self, sticky, tmp_path,
                                            monkeypatch):
        monkeypatch.setenv("PATH", fake_tmux(tmp_path, client="tmux 3.7c",
                                             server="3.7c"))
        assert sticky.old_tmux("/tmp/none") == ""

    def test_the_warning_waits_for_the_person_it_is_for(self, sticky,
                                                        monkeypatch, capsys):
        """Held at a question, because attaching repaints the screen over
        anything printed just before it."""
        monkeypatch.setattr(sticky.cli, "old_tmux", lambda s: "tmux 3.4 ...")
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        asked = []
        monkeypatch.setattr(sticky.cli, "ask",
                            lambda q, c: asked.append(q) or "y")
        assert sticky.tmux_too_old(None) is False, "Enter goes on"
        assert asked, "and it was asked"
        assert "tmux 3.4" in capsys.readouterr().err
        monkeypatch.setattr(sticky.cli, "ask", lambda q, c: "n")
        assert sticky.tmux_too_old(None) is True, "n stops"

    def test_a_key_binding_is_not_held_up(self, sticky, monkeypatch):
        """run-shell has no terminal, and nobody to answer."""
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
        monkeypatch.setattr(sticky.cli, "old_tmux",
                            lambda s: pytest.fail("not even asked"))
        assert sticky.tmux_too_old(None) is False

    def test_a_prompt_that_cannot_open_says_why(self, sticky, tmp_path,
                                                monkeypatch):
        monkeypatch.setenv("PATH", fake_tmux(tmp_path))
        with pytest.raises(RuntimeError) as raised:
            sticky.commands.open_prompt(sticky.Tmux("/tmp/none"), ["true"], None)
        said = str(raised.value)
        assert "tmux 3.4 has no floating panes" in said
        assert "3.7 or newer" in said

    def test_the_new_tab_key_leaves_its_message_standing(self, sticky,
                                                         tmp_path,
                                                         monkeypatch):
        """A 1 from run-shell is answered with tmux's own `returned 1`,
        written over the message that said why."""
        monkeypatch.setenv("PATH", fake_tmux(tmp_path))
        said = []
        tm = sticky.Tmux("/tmp/none")
        monkeypatch.setattr(tm, "ok", lambda *a: said.append(a) or True)
        args = argparse.Namespace(
            socket="/tmp/none", client=None, source_pane=None,
            virtual_rows=None, sidebar_width=sticky.DEFAULT_SIDEBAR_WIDTH,
            local=False, menus=False, agent=None, agent_cmd=None,
            sandbox=None, sandbox_image=None, sandbox_arg=None, dir=None)
        assert sticky.open_project_prompt(tm, "sticky-chat", args) == 0
        assert any("no floating panes" in " ".join(a) for a in said)
