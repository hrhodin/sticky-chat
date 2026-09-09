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
        assert codex.tested is False
        assert "codex (untested)" in sticky.agent_choices()
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

    def test_a_profile_with_no_glob_looks_at_nothing(self, sticky, tmp_path):
        """Which is what keeps every Claude tab out of this code path: the
        id was ours to choose, so there is nothing on disk to learn."""
        home = tmp_path / "home"
        self.transcript(home, f".claude/projects/-a-project/{self.ID}.jsonl",
                        self.header(self.ID), when=self.LAUNCH + 5)
        assert not sticky.CLAUDE.can_discover and not sticky.GENERIC.can_discover
        assert sticky.CLAUDE.transcript_glob == ""
        assert (sticky.discover_session(sticky.CLAUDE, self.LAUNCH, str(home))
                == "")
        told = dataclasses.replace(sticky.CLAUDE,
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
