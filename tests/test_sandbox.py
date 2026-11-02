"""The fence around an agent, and what a fenced command line still allows.

The container itself is the engine's business. What is ours is the command
line: it has to say the same thing to the agent that an unfenced one did,
survive being edited by everything that edits command lines, and be readable
back - which fence, which home - by a sidebar that never sees the container.

Where podman is tested on its own it is because it is not docker underneath:
it is rootless, so who the container runs as is a thing the line has to say
out loud, and it keeps its own image store, so every question about the image
has an answer per engine. Everything else is asserted to be the same line,
which is the point of the table.

The namespace fence has a second reason to be tested here and tested hard.
Nobody has ever run it - bwrap is Linux only and this was written on a Mac -
so the composed line, the order of the binds and the boundary they parse back
to are the only part of it that anything checks at all. Everything below that
needs a kernel is one `@pytest.mark.integration` test that skips itself.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from conftest import LAUNCHER, SOCKET


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    """Nothing here writes into the state directory you are using.

    Building a command line does not touch the disk - the home is made when
    the tab is opened, not when the line is written - but `remember` does,
    and a test that changed what your next tab defaults to would be a test
    with a side effect nobody asked for."""
    import sticky_chat.sandbox as box

    monkeypatch.setattr(box, "STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def box(sticky):
    return sticky.sandbox if hasattr(sticky, "sandbox") else __import__(
        "sticky_chat.sandbox", fromlist=["sandbox"])


def wrapped(box, sticky, command="claude --dangerously-skip-permissions",
            project="/Users/me/Code/thing"):
    return box.container_command(command, project, sticky.CLAUDE)


class TestWhatTheContainerCanReach:

    def test_the_project_is_mounted_at_its_own_path(self, box, sticky):
        """Not at /workspace. Every path either side names is then the same
        path - which matters most for the directory the agent files its
        transcript under, because that directory is named after the
        project's own path."""
        words = shlex.split(wrapped(box, sticky))
        mounts = [words[i + 1] for i, w in enumerate(words) if w == "-v"]
        assert "/Users/me/Code/thing:/Users/me/Code/thing" in mounts
        assert "-w" in words
        assert words[words.index("-w") + 1] == "/Users/me/Code/thing"

    def test_nothing_else_of_yours_goes_in(self, box, sticky):
        """Two mounts, and the second is a directory sticky made."""
        words = shlex.split(wrapped(box, sticky))
        mounts = [words[i + 1] for i, w in enumerate(words) if w == "-v"]
        assert len(mounts) == 2
        home = [m for m in mounts if m.endswith(f":{box.CONTAINER_HOME}")]
        assert home and home[0].startswith(box.sandbox_dir())
        assert os.path.expanduser("~/.ssh") not in " ".join(mounts)

    def test_it_runs_as_you(self, box, sticky):
        """A container writing into the bind mount as anybody else leaves
        files you cannot edit, and an agent that is root refuses to skip
        permission prompts."""
        words = shlex.split(wrapped(box, sticky))
        assert words[words.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"

    def test_a_labelled_mount_still_reads_back(self, box, sticky,
                                               monkeypatch):
        """On a machine with SELinux the mount carries `:z`, which is the
        difference between a project the container can read and a tab that
        comes up on an empty directory - and the home has to be findable
        through it, because that is where the transcript is."""
        monkeypatch.setattr(box, "relabel", lambda: ":z")
        line = box.container_command("claude", "/p/x", sticky.CLAUDE)
        assert " -v /p/x:/p/x:z " in f" {line} "
        assert box.home_of(line) == box.agent_home("claude")
        assert box.inner_command(line) == "claude"

    def test_a_secret_is_named_and_never_copied(self, box, sticky,
                                                monkeypatch):
        """`-e NAME` with no value: it is the host's environment that
        decides whether there is one, and the key itself never goes onto a
        command line that is written down in a window record."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        line = wrapped(box, sticky)
        assert " -e ANTHROPIC_API_KEY " in f" {line} "
        assert "sk-secret" not in line


    def test_building_a_line_touches_nothing(self, box, sticky):
        """The home is made when a tab opens, not when a line is written -
        and lines are written by things that are only looking at them."""
        line = wrapped(box, sticky)
        assert not os.path.exists(box.agent_home("claude"))
        box.make_home(line)
        assert os.path.isdir(box.agent_home("claude"))


class TestTakingTheFenceOffAgain:

    def test_a_fenced_line_reads_back(self, box, sticky):
        line = wrapped(box, sticky)
        assert box.inner_command(line) == "claude --dangerously-skip-permissions"
        assert box.fenced_by(line) == "docker"
        assert box.home_of(line) == box.agent_home("claude")

    def test_a_plain_line_is_left_alone(self, box, sticky):
        plain = "claude --dangerously-skip-permissions"
        assert box.split_command(plain) == ("", plain)
        assert box.fenced_by(plain) == "off"
        assert box.home_of(plain) == "", "the home is yours, and known"

    def test_the_fence_can_be_swapped_for_another(self, box, sticky):
        """`C-g c` beside a container inherits the container's whole command
        line; answering `off` at the prompt has to leave the agent's own
        arguments alone while replacing everything in front of them."""
        line = wrapped(box, sticky)
        bare = box.refence(line, box.OFF, "/Users/me/Code/thing", sticky.CLAUDE)
        assert bare == "claude --dangerously-skip-permissions"
        again = box.refence(bare, box.DOCKER, "/Users/me/Code/thing",
                            sticky.CLAUDE)
        assert again == line, "and putting it back is the same line"
        assert box.refence(line, box.DOCKER, "/Users/me/Code/thing",
                           sticky.CLAUDE) == line

    def test_a_conversation_id_lands_on_the_agent(self, box, sticky):
        """The one that would break silently: an id put where this agent
        takes its subcommand - first, before anything else - lands on
        `docker run` and the tab dies as it opens."""
        for agent in (sticky.CLAUDE, sticky.CODEX):
            line = box.container_command(agent.command, "/p", agent)
            named = sticky.with_words(line, ["--session-id", "abc"], agent)
            assert box.inner_command(named).startswith(agent.command.split()[0])
            assert "--session-id abc" in box.inner_command(named)
            assert "--session-id" not in box.split_command(named)[0]

    def test_asking_what_it_names_asks_the_agents_half(self, box, sticky):
        line = box.container_command("claude --session-id abc", "/p",
                                     sticky.CLAUDE)
        assert sticky.session_named(line, sticky.CLAUDE) == "abc"
        assert sticky.names_a_session(line, sticky.CLAUDE)
        dropped = sticky.without_session(line, sticky.CLAUDE)
        assert box.fenced_by(dropped) == "docker", "still fenced"
        assert box.inner_command(dropped) == "claude"

    def test_a_sandboxed_tab_comes_back_sandboxed(self, box, sticky):
        """What `resume` and `C-g o` replay is the recorded command line."""
        line = box.container_command("claude", "/p", sticky.CLAUDE)
        back = sticky.resume_command({"command": line, "agent": "claude",
                                      "session": "1234", "agent_session": ""})
        assert box.fenced_by(back) == "docker"
        assert box.inner_command(back) == "claude --resume 1234"


class TestWhichFenceATabGets:

    def args(self, **fields):
        return argparse.Namespace(**{"sandbox": "", "agent_cmd": None,
                                     "sandbox_image": "", "sandbox_arg": [],
                                     **fields})

    def test_a_command_you_named_is_left_alone(self, sticky):
        """`--agent-cmd` is the one case where we have no idea what the
        words mean, so a default fence does not go around them."""
        got = sticky.fence_tab("weird --thing", "/p", sticky.CLAUDE,
                               self.args(agent_cmd="weird --thing"))
        assert got == "weird --thing"

    def test_a_tab_opened_beside_another_keeps_its_fence(self, sticky):
        line = "claude --dangerously-skip-permissions"
        assert sticky.fence_tab(line, "/p", sticky.CLAUDE, self.args(),
                                inherited=True) == line

    def test_a_fence_nobody_asked_for_steps_aside(self, sticky, monkeypatch,
                                                  capsys):
        """The default is a preference. A preference should not be able to
        make `sticky-chat start` fail on a laptop where docker is down."""
        monkeypatch.setattr(sticky.commands, "default_fence", lambda: "docker")
        monkeypatch.setattr(sticky.commands, "unavailable",
                            lambda fence: "the docker daemon is not running")
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert got == "claude"
        assert "not running" in capsys.readouterr().err

    def test_a_fence_you_asked_for_stops_the_tab(self, sticky, monkeypatch):
        """Opening it unfenced instead would be the one outcome nobody
        asked for."""
        monkeypatch.setattr(sticky.commands, "unavailable",
                            lambda fence: "docker is not installed")
        with pytest.raises(SystemExit):
            sticky.fence_tab("claude", "/p", sticky.CLAUDE,
                             self.args(sandbox="docker"))

    def test_an_unknown_fence_is_a_mistake_worth_stopping_for(self, sticky):
        with pytest.raises(SystemExit):
            sticky.fence_tab("claude", "/p", sticky.CLAUDE,
                             self.args(sandbox="chroot"))

    def test_the_agents_own_sandbox_flag_is_pointed_at_the_dashes(
            self, sticky, capsys):
        """`--sandbox danger-full-access` is codex's flag, and sticky's
        parser takes the name first. The answer is `--`, and the message
        says so rather than listing four fences that are not what was
        meant."""
        with pytest.raises(SystemExit):
            sticky.fence_tab("codex", "/p", sticky.CODEX,
                             self.args(sandbox="danger-full-access"))
        said = capsys.readouterr().err
        assert "-- --sandbox danger-full-access" in said
        assert "reaches the agent untouched" in said

    def test_off_takes_a_fence_off(self, sticky, box):
        line = box.container_command("claude", "/p", sticky.CLAUDE)
        assert sticky.fence_tab(line, "/p", sticky.CLAUDE,
                                self.args(sandbox="off")) == "claude"


class TestRememberingTheAnswer:

    def test_what_you_said_last_time_is_the_next_default(self, box):
        assert box.remembered() == ""
        box.remember("off")
        assert box.remembered() == "off"
        assert box.default_fence() == "off"
        box.remember("docker")
        assert box.default_fence() == "docker"

    def test_a_fence_that_does_not_exist_is_not_remembered(self, box):
        box.remember("chroot")
        assert box.remembered() == ""

    def test_the_dockerfile_is_yours_once_it_exists(self, box):
        """It is a file rather than a string so that you can add your
        toolchain to it, and a build that reverted your edits would be
        worse than no file at all."""
        path = box.write_dockerfile()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\nRUN echo mine\n")
        assert box.write_dockerfile() == path
        with open(path, encoding="utf-8") as fh:
            assert "RUN echo mine" in fh.read()


class TestNothingIsFencedUntilYouSaySo:
    """The default is no fence, and a build is asked for.

    This is here because the first version of it got it wrong and shipped:
    `default_fence` inferred the answer from what the machine could run, so
    `sticky-chat start` on a machine with a container engine up went away for
    minutes pulling a base image and building one, for a fence nobody had
    asked for. The two halves of the fix are asserted apart - what an unasked
    tab gets, and what happens when the image it would need is not there -
    because they failed together and either one alone would have been enough
    to be caught by.
    """

    def args(self, **fields):
        return argparse.Namespace(**{"sandbox": "", "agent_cmd": None,
                                     "sandbox_image": "", "sandbox_arg": [],
                                     **fields})

    def ready(self, sticky, monkeypatch, built=False, answer=None):
        """A machine where every fence could be had, and a note of whether
        anything asked to build."""
        done = []
        monkeypatch.setattr(sticky.commands, "unavailable", lambda fence: "")
        monkeypatch.setattr(sticky.commands, "unreachable",
                            lambda *a, **k: "")
        monkeypatch.setattr(sticky.commands, "image_built",
                            lambda *a, **k: built)
        monkeypatch.setattr(sticky.commands, "ensure_image",
                            lambda *a, **k: done.append("built") or "")
        if answer is not None:
            monkeypatch.setattr(sticky.commands, "ask",
                                lambda question, choices:
                                done.append(question) or answer)
        return done

    def test_a_fence_that_could_be_had_is_suggested_not_taken(self, box,
                                                              monkeypatch):
        monkeypatch.setattr(box, "unavailable", lambda fence: "")
        assert box.suggested_fence() == "docker", "there is one to be had"
        assert box.default_fence() == "off", "and nobody has asked for it"

    def test_the_answer_on_record_is_the_default(self, box, monkeypatch):
        monkeypatch.setattr(box, "unavailable", lambda fence: "")
        box.remember("podman")
        assert box.default_fence() == "podman"

    def test_an_unasked_tab_opens_on_the_host(self, sticky, monkeypatch):
        """And asks no engine anything on the way: a start that has to wait
        for a daemon to answer before the first pane comes up is the other
        half of what was wrong with inferring this."""
        done = self.ready(sticky, monkeypatch)
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert got == "claude"
        assert done == []

    def test_a_remembered_fence_asks_before_it_builds(self, sticky,
                                                      monkeypatch, capsys):
        """`n` leaves the tab unfenced rather than leaving it unopened: a
        remembered answer is a preference, and the image is a thing you can
        build later with one line, which is the line it prints."""
        done = self.ready(sticky, monkeypatch, answer="n")
        sticky.sandbox.remember("docker")
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert got == "claude"
        assert "built" not in done, "nothing was built"
        assert "Build it now?" in done[0]
        said = capsys.readouterr().err
        assert "not fenced" in said and "sandbox --build docker" in said

    def test_no_terminal_to_ask_at_is_a_no(self, sticky, monkeypatch,
                                           capsys):
        """`ask` answers nothing when there is no tty, and nothing is not
        yes. This is the path a script takes."""
        done = self.ready(sticky, monkeypatch)
        sticky.sandbox.remember("docker")
        assert not sys.stdin.isatty(), "the premise of the test"
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert got == "claude"
        assert done == []
        assert "not fenced" in capsys.readouterr().err

    def test_saying_yes_builds_it_and_fences_the_tab(self, sticky,
                                                     monkeypatch):
        done = self.ready(sticky, monkeypatch, answer="y")
        sticky.sandbox.remember("docker")
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert "built" in done
        assert got.startswith("docker run")

    def test_a_fence_you_named_is_not_asked_about(self, sticky, monkeypatch):
        """Naming a container is asking for the image it runs in. The
        question is for the answer that came off a file, not for the one on
        the command line in front of you."""
        done = self.ready(sticky, monkeypatch,
                          answer=None)   # `ask` left real: calling it fails
        monkeypatch.setattr(sticky.commands, "ask",
                            lambda *a: pytest.fail("asked anyway"))
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE,
                               self.args(sandbox="docker"))
        assert "built" in done
        assert got.startswith("docker run")

    def test_a_built_image_is_not_asked_about_either(self, sticky,
                                                     monkeypatch):
        """The question is about the minutes, and there are none to spend
        when the image is already in the store."""
        self.ready(sticky, monkeypatch, built=True)
        monkeypatch.setattr(sticky.commands, "ask",
                            lambda *a: pytest.fail("asked anyway"))
        sticky.sandbox.remember("docker")
        got = sticky.fence_tab("claude", "/p", sticky.CLAUDE, self.args())
        assert got.startswith("docker run")


class TestAptInsideARootlessContainer:
    """The Dockerfile's one concession to how podman maps uids.

    apt drops to the `_apt` user to fetch, and a rootless container whose uid
    map holds a single id cannot: `setgroups 65534 failed - Operation not
    permitted`, then `Method http has died unexpectedly`, and a build that
    ends at step 2 having pulled the base image for nothing. Met on a machine
    whose home is on NFS and whose /etc/subuid has no ranges in it.
    """

    def test_apt_is_told_to_fetch_as_root(self, box):
        text = box.DOCKERFILE
        assert 'APT::Sandbox::User "root"' in text
        assert text.index("apt.conf.d") < text.index("apt-get update"), \
            "and told before the first fetch, not after it"

    def test_the_file_on_disk_says_it_too(self, box):
        """The template is what a new machine gets; the file is what builds.
        An older machine keeps its own file, which is the point of the file -
        and is why this was worth saying out loud in the docs."""
        with open(box.write_dockerfile(), encoding="utf-8") as fh:
            assert 'APT::Sandbox::User "root"' in fh.read()


class TestMoreThanOneEngine:
    """docker and podman, and the one place they really differ: who the
    container runs as.

    Everything else a line says is asserted to be the same line, because a
    fence is meant to be a row in a table rather than a branch - the day that
    stops being true is the day `split_command`, `home_of` and `fenced_by`
    each have to be told which engine wrote the line they are reading.
    """

    def both(self, box, sticky, command="claude", project="/p"):
        return (box.container_command(command, project, sticky.CLAUDE,
                                      box.DOCKER),
                box.container_command(command, project, sticky.CLAUDE,
                                      box.PODMAN))

    def test_podman_is_a_row_in_the_table(self, box):
        assert box.fence_named("podman") is box.PODMAN
        assert box.PODMAN in box.FENCES
        assert "podman" in box.fence_choices()
        assert box.PODMAN.cli == "podman" and box.PODMAN.needs == "podman"

    def test_a_fence_without_an_engine_asks_for_no_program(self, box):
        """`cli` and `needs` are two questions, which is why they are two
        fields: a fence that fences without a container to run has something
        to install and nothing to type `run` at."""
        assert box.OFF.cli == "" and box.OFF.needs == ""

    def test_the_line_differs_only_in_the_engine_and_the_mapping(self, box,
                                                                sticky):
        """The one assertion that keeps the two honest. Everything a line is
        later asked - which fence, which home, which conversation - is read
        back off words that are in the same places.

        Three words may differ and no others: the engine at the front, the
        mapping that makes rootless podman run as you, and the marker, which
        names the fence rather than the engine because that is the word `C-g b`
        reads to know what it is taking off."""
        docker, podman = self.both(box, sticky)
        assert podman.split(" ", 1)[0] == "podman"

        def bones(line):
            words = shlex.split(line.split(" ", 1)[1])
            return [w for w in words
                    if w != "--userns" and not w.startswith("keep-id")
                    and not w.startswith(f"{box.FENCE_ENV}=")]

        assert bones(podman) == bones(docker)

    def test_rootless_podman_is_told_to_keep_your_uid(self, box, sticky):
        """The whole of what makes podman not docker. Without this the
        container runs as a subuid of yours - uid 501 inside is 100500 out -
        and the files the agent writes into your project come back owned by a
        number you have no account for; without the `--user` beside it the
        agent is root, and Claude Code will not skip permission prompts as
        root. Spelled out rather than the bare `keep-id` because on macOS the
        user podman maps is the Linux account inside its machine VM, whose gid
        is not the one the image's `agent` was built with."""
        docker, podman = self.both(box, sticky)
        words = shlex.split(podman)
        assert words[words.index("--userns") + 1] == \
            f"keep-id:uid={os.getuid()},gid={os.getgid()}"
        assert words[words.index("--user") + 1] == \
            f"{os.getuid()}:{os.getgid()}"
        assert "--userns" not in shlex.split(docker), \
            "docker's daemon is root and calls keep-id an error"

    def test_root_is_not_told_to_keep_an_id_it_already_has(self, box, sticky,
                                                          monkeypatch):
        """podman refuses `keep-id` outright for the root user, and a fence
        that asked for it there would be a tab that will not start."""
        monkeypatch.setattr(box.os, "getuid", lambda: 0)
        monkeypatch.setattr(box.os, "getgid", lambda: 0)
        line = box.container_command("claude", "/p", sticky.CLAUDE, box.PODMAN)
        assert "--userns" not in shlex.split(line)

    def test_a_podman_line_reads_back_like_any_other(self, box, sticky):
        """The marker carries the fence's name, so the pane knows which box it
        is in and `C-g b` knows what it is taking off."""
        _, podman = self.both(box, sticky,
                              command="claude --dangerously-skip-permissions")
        assert box.fenced_by(podman) == "podman"
        assert box.inner_command(podman) == \
            "claude --dangerously-skip-permissions"
        assert box.home_of(podman) == box.agent_home("claude")

    def test_both_engines_share_one_home(self, box, sticky):
        """Decided rather than fallen into: both are made to run as you, so a
        login written in a docker tab is a login a podman tab can use, and
        moving a tab between them carries no conversation because the
        conversation is already where the agent will look."""
        docker, podman = self.both(box, sticky)
        assert box.home_of(docker) == box.home_of(podman)

    def test_a_tab_moves_straight_from_one_engine_to_the_other(self, box,
                                                              sticky):
        """`refence` takes the old fence off before putting the new one on, so
        docker to podman is one move and not two."""
        docker, podman = self.both(box, sticky)
        assert box.refence(docker, box.PODMAN, "/p", sticky.CLAUDE) == podman
        assert box.refence(podman, box.DOCKER, "/p", sticky.CLAUDE) == docker
        assert box.refence(podman, box.OFF, "/p", sticky.CLAUDE) == "claude"

    def test_a_conversation_id_still_lands_on_the_agent(self, box, sticky):
        """The failure that would break silently, asked of podman too: the
        `--userns` argument is another word in front of the agent's own, and a
        parser that counted words rather than finding the marker would put the
        id on `podman run`."""
        line = box.container_command("claude", "/p", sticky.CLAUDE, box.PODMAN)
        named = sticky.with_words(line, ["--session-id", "abc"], sticky.CLAUDE)
        assert box.inner_command(named) == "claude --session-id abc"
        assert "--session-id" not in box.split_command(named)[0]
        assert box.fenced_by(named) == "podman"
        assert box.home_of(named) == box.agent_home("claude")

    def test_a_podman_tab_comes_back_a_podman_tab(self, box, sticky):
        line = box.container_command("claude", "/p", sticky.CLAUDE, box.PODMAN)
        back = sticky.resume_command({"command": line, "agent": "claude",
                                      "session": "1234", "agent_session": ""})
        assert box.fenced_by(back) == "podman"
        assert box.inner_command(back) == "claude --resume 1234"


class TestAskingWhatThisMachineCanRun:

    def probe(self, box, monkeypatch, answers):
        """Stand in for the engines, and record what was typed at them."""
        asked = []

        def fake(command, **kw):
            asked.append(list(command))
            code = 0 if answers.get(command[0], False) else 1
            return subprocess.CompletedProcess(command, code, "", "")

        monkeypatch.setattr(box, "_DAEMON", {})
        monkeypatch.setattr(box.subprocess, "run", fake)
        monkeypatch.setattr(box.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        return asked

    def test_the_probe_asks_for_no_field_only_one_of_them_has(self, box,
                                                             monkeypatch):
        """The bug this replaced: `info --format {{.ServerVersion}}` is a
        docker key, podman's info has no such field, and a template that
        cannot execute makes podman exit non-zero - so a probe reading only
        the exit status called a running engine down and the fence was never
        offered."""
        asked = self.probe(box, monkeypatch, {"podman": True})
        assert box.daemon_running("podman") is True
        assert asked == [["podman", "info"]]

    def test_each_engine_is_asked_on_its_own(self, box, monkeypatch):
        """One cached boolean for the machine would have had podman inherit
        docker's answer: offered a fence that cannot start, and a tab whose
        pane dies as it opens."""
        self.probe(box, monkeypatch, {"docker": True})
        assert box.unavailable(box.DOCKER) == ""
        assert "podman machine start" in box.unavailable(box.PODMAN)
        assert box.suggested_fence() == "docker"

    def test_an_engine_that_is_down_is_said_so_in_its_own_words(self, box,
                                                               monkeypatch):
        """The fix is not the same fix: docker has a daemon to start and
        podman, off Linux, has a virtual machine."""
        self.probe(box, monkeypatch, {})
        assert box.unavailable(box.DOCKER) == \
            "the docker daemon is not running"
        assert "machine" in box.unavailable(box.PODMAN)

    def test_a_missing_program_is_said_before_anything_is_asked(self, box,
                                                               monkeypatch):
        self.probe(box, monkeypatch, {})
        monkeypatch.setattr(box.shutil, "which", lambda name: None)
        assert box.unavailable(box.PODMAN) == "podman is not installed"

    def test_podman_alone_is_the_one_that_gets_suggested(self, box,
                                                         monkeypatch):
        """A machine with one engine should not have to be told which."""
        self.probe(box, monkeypatch, {"podman": True})
        assert box.suggested_fence() == "podman"
        assert box.engine_named() is box.PODMAN

    def test_warming_up_asks_every_engine_at_once(self, box, monkeypatch):
        """They are independent round trips and the prompt prints all of them,
        so waiting for one before starting the next would cost the second of
        keystrokes this was written to save."""
        self.probe(box, monkeypatch, {"docker": True, "podman": True})
        box.warm_up()
        for _ in range(100):
            if len(box._DAEMON) == 2:
                break
            time.sleep(0.05)
        assert box._DAEMON == {"docker": True, "podman": True}


class TestTwoStoresAndOneTag:
    """Each engine keeps its own images, so the same tag is two images and
    every question about one carries the engine being asked."""

    def asked(self, box, monkeypatch, out=""):
        seen = []

        def fake(command, **kw):
            seen.append(list(command))
            return subprocess.CompletedProcess(command, 0, out, "")

        monkeypatch.setattr(box.subprocess, "run", fake)
        return seen

    def test_the_image_is_asked_of_the_engine_that_will_run_it(self, box,
                                                              monkeypatch):
        """Asking docker whether a podman tab can start answers yes and then
        respawns the pane onto a `podman run` of an image that is not there -
        and a pane whose command dies takes its window and the sidebar."""
        seen = self.asked(box, monkeypatch, out="abc123\n")
        assert box.image_built("sticky-agent:latest", "podman")
        assert seen == [["podman", "image", "ls", "-q", "sticky-agent:latest"]]

    def test_the_probe_for_the_agent_runs_in_that_engine_too(self, box,
                                                             monkeypatch):
        seen = self.asked(box, monkeypatch, out="/usr/local/bin/claude\n")
        assert box.runs_in_image("sticky-agent:latest", "claude", [], "podman")
        assert seen[0][0] == "podman"

    def test_a_build_fills_one_store_from_the_one_dockerfile(self, box,
                                                             monkeypatch):
        """podman reads the file docker wrote, with the same build arguments,
        because the account it bakes in is the uid both engines are then told
        to run as."""
        seen = self.asked(box, monkeypatch)
        assert box.build_image("sticky-agent:latest", "podman") == ""
        assert seen[0][:2] == ["podman", "build"]
        assert box.dockerfile_path() in seen[0]
        assert f"UID={os.getuid()}" in seen[0]

    def test_a_build_says_which_store_it_is_filling(self, box, monkeypatch):
        """`--build` on its own means the engine a new tab would open in,
        which on a one-engine machine is the only answer there was; named, it
        is whichever you said."""
        monkeypatch.setattr(box, "unavailable", lambda fence: "")
        box.remember("podman")
        assert box.engine_named("") is box.PODMAN
        assert box.engine_named("docker") is box.DOCKER
        assert box.engine_named("off") is None, "nothing to build for no fence"
        assert box.engine_named("chroot") is None

    def test_a_machine_with_no_engine_is_told_what_is_missing(self, box,
                                                              monkeypatch):
        """Rather than being asked to rephrase: the answer names docker so
        that `unavailable` can say it is not installed."""
        monkeypatch.setattr(box, "unavailable", lambda fence: "nothing here")
        assert box.engine_named("") is box.DOCKER


class TestCarryingAConversationAcross:
    """Boxing a running tab restarts the agent, and an agent restarted in a
    container looks for its conversation in the container's home - where
    there has never been one. This is the copy that stops `--resume` coming
    back as a new conversation wearing the old notes."""

    def written(self, home, sticky, session, project="/Users/me/Code/thing",
                text="before"):
        path = sticky.conversation_file(sticky.CLAUDE, project, session,
                                        str(home))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_the_conversation_goes_where_the_agent_will_look(self, box,
                                                            sticky, tmp_path):
        """Same project path either side, so the directory the agent files it
        under is the same name - a copy, not a translation."""
        host, boxed = tmp_path / "host", tmp_path / "boxed"
        self.written(host, sticky, "abc")
        carried, why = box.carry_history(sticky.CLAUDE, "/Users/me/Code/thing",
                                        "abc", str(host), str(boxed))
        assert carried and not why
        landed = sticky.conversation_file(sticky.CLAUDE,
                                         "/Users/me/Code/thing", "abc",
                                         str(boxed))
        with open(landed, encoding="utf-8") as fh:
            assert fh.read() == "before"

    def test_the_hour_in_the_box_comes_back_out(self, box, sticky, tmp_path):
        host, boxed = tmp_path / "host", tmp_path / "boxed"
        self.written(host, sticky, "abc", text="before")
        there = self.written(boxed, sticky, "abc", text="before\nand after")
        os.utime(there, (time.time() + 10, time.time() + 10))
        carried, why = box.carry_history(sticky.CLAUDE, "/Users/me/Code/thing",
                                        "abc", str(boxed), str(host))
        assert carried and not why
        with open(sticky.conversation_file(
                sticky.CLAUDE, "/Users/me/Code/thing", "abc",
                str(host)), encoding="utf-8") as fh:
            assert fh.read() == "before\nand after"

    def test_an_older_copy_does_not_overwrite_a_newer_one(self, box, sticky,
                                                          tmp_path):
        """Newer wins, or unboxing twice would put back what the box had
        already carried on from."""
        host, boxed = tmp_path / "host", tmp_path / "boxed"
        old = self.written(host, sticky, "abc", text="stale")
        self.written(boxed, sticky, "abc", text="current")
        os.utime(old, (time.time() - 600, time.time() - 600))
        assert box.carry_history(sticky.CLAUDE, "/Users/me/Code/thing", "abc",
                                 str(host), str(boxed)) == (True, "")
        with open(sticky.conversation_file(
                sticky.CLAUDE, "/Users/me/Code/thing", "abc",
                str(boxed)), encoding="utf-8") as fh:
            assert fh.read() == "current"

    def test_a_tab_with_nothing_said_yet_moves_freely(self, box, sticky,
                                                     tmp_path):
        assert box.carry_history(sticky.CLAUDE, "/p", "abc",
                                 str(tmp_path / "host"),
                                 str(tmp_path / "boxed")) == (True, "")

    def test_a_shell_has_nothing_to_carry(self, box, sticky, tmp_path):
        """It writes nothing down, so there is nothing to lose."""
        carried, why = box.carry_history(sticky.SHELL, "/p", "tab-abc123",
                                        "", str(tmp_path))
        assert carried and not why

    def test_a_conversation_nobody_can_name_stops_the_move(self, box, sticky,
                                                           tmp_path):
        """codex keeps its history in a database that cannot be named from an
        id, and a bare --continue tab has no id at all. Arriving without the
        conversation looks like success and is not."""
        for agent in (sticky.CODEX, sticky.CLAUDE):
            session = "tab-abc123" if agent is sticky.CLAUDE else "some-id"
            carried, why = box.carry_history(agent, "/p", session, "",
                                            str(tmp_path))
            assert not carried, agent.name
            assert "cannot be carried" in why


def wait_for(look, gone: bool = False, tries: int = 40):
    """Poll `look` until it answers, or stops answering. Engines are slow."""
    answer = look()
    for _ in range(tries):
        if bool(answer) is not gone:
            return answer
        time.sleep(0.3)
        answer = look()
    return answer


def engine_ready(cli: str = "docker") -> bool:
    """Whether this engine is here with the image already built in its store.

    Per engine, because the stores are separate: a machine that has built the
    image for docker has not built it for podman, and the podman tests have to
    skip rather than spend four minutes building one inside a test."""
    if not shutil.which(cli):
        return False
    try:
        done = subprocess.run([cli, "image", "ls", "-q",
                               "sticky-agent:latest"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("docker"),
                    reason="needs docker and `sticky-chat sandbox --build`")
def test_a_fenced_tab_is_a_tab_like_any_other(tmux, server, run_sticky,
                                              project):
    """The whole point, end to end: tmux runs the container as the pane's
    command, so the pane is a pane, the sidebar is beside it, and the only
    thing that changed is what the agent can reach.

    A shell rather than an agent, because a container with no login in it
    would spend this test on a sign-in screen. What is being tested is the
    pane, and a pane does not know what is drawing into it."""
    before = boxes()
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--sandbox", "docker",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane, "the tab did not open"
    try:
        tmux("send-keys", "-t", pane, "pwd; ls /Users 2>&1 | head -1", "Enter")
        want = project.rstrip("/")
        for _ in range(40):
            screen = tmux("capture-pane", "-p", "-t", pane).replace("\n", "")
            if want in screen:
                break
            time.sleep(0.3)
        assert want in screen, "the project is not where it says it is"
        home = tmux("show-options", "-pqv", "-t", pane, "@sticky_home").strip()
        assert home and os.path.isdir(home), "the agent's home is on this disk"
        partner = tmux("show-options", "-pqv", "-t", pane,
                       "@sticky_partner").strip()
        assert partner, "and it has a sidebar, like any other tab"
        mine = wait_for(lambda: boxes() - before)
        assert mine, "the tab is a container"
    finally:
        tmux("kill-window", "-t", pane)
    assert not wait_for(lambda: mine & boxes(), gone=True), \
        "and closing the tab takes the container with it"


def boxes(cli: str = "docker") -> set[str]:
    """The sticky containers running right now, in one engine's own store.

    A set, and compared against what was there before, because a machine
    running this suite is a machine with sticky tabs open on it: "none at
    all" is not a thing a test may assert."""
    return set(subprocess.run([cli, "ps", "--filter", "label=sticky-chat",
                               "-q"], capture_output=True,
                              text=True).stdout.split())


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("docker"),
                    reason="needs docker and `sticky-chat sandbox --build`")
def test_a_running_tab_moves_into_a_box_and_back(tmux, server, run_sticky,
                                                 project):
    """`C-g B` and `C-g b`. The pane is respawned rather than replaced, which
    is what keeps the window, the sidebar and everything hung off the pane
    while what is running inside it changes."""
    before = boxes()
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane
    try:
        partner = tmux("show-options", "-pqv", "-t", pane,
                       "@sticky_partner").strip()
        run_sticky("refence", "--sandbox", "docker", "--pane", pane)
        for _ in range(40):
            if tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_home").strip():
                break
            time.sleep(0.3)
        home = tmux("show-options", "-pqv", "-t", pane, "@sticky_home").strip()
        assert home and os.path.isdir(home), "it is in a box"
        mine = wait_for(lambda: boxes() - before)
        assert mine, "with a container behind it"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip() == partner, \
            "and the same sidebar beside it"
        tmux("send-keys", "-t", pane, "ls /Users 2>&1 | head -1", "Enter")
        for _ in range(40):
            screen = tmux("capture-pane", "-p", "-t", pane).replace("\n", "")
            if "No such file" in screen or "cannot access" in screen:
                break
            time.sleep(0.3)
        assert "No such file" in screen or "cannot access" in screen, screen

        run_sticky("refence", "--sandbox", "off", "--pane", pane)
        for _ in range(40):
            if not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip():
                break
            time.sleep(0.3)
        assert not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip(), "and back out again"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip() == partner
        # And the container went with it. A docker client killed outright
        # leaves its container running, so unboxing has to ask it to stop.
        assert not wait_for(lambda: mine & boxes(), gone=True), \
            "unboxing left the container behind"
    finally:
        tmux("kill-window", "-t", pane)


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("docker"),
                    reason="needs docker and `sticky-chat sandbox --build`")
def test_a_tab_can_be_boxed_from_inside_itself(tmux, server, run_sticky,
                                               project, sticky_home):
    """The one move a tab cannot make on its own.

    Restarting the pane kills everything in it, so a `refence` run *in* the
    tab is cut short by its own work: the pane came back in a container while
    the options still said it was on the host - a tab in a box that sticky
    could not tell from one that had never moved, so `C-g b` would not take
    it out again. tmux does it instead, which is what the key binding has
    always done."""
    before = boxes()
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane
    try:
        inside = (f"STICKY_HOME={sticky_home} {sys.executable} "
                  f"{LAUNCHER} refence --sandbox docker "
                  f"--socket {SOCKET} --pane {pane}")
        tmux("send-keys", "-t", pane, inside, "Enter")
        home = wait_for(lambda: tmux("show-options", "-pqv", "-t", pane,
                                     "@sticky_home").strip())
        assert home, "it moved, and said so"
        assert wait_for(lambda: boxes() - before), "with a container behind it"
        line = tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_agent_cmd").strip()
        assert "STICKY_SANDBOX=docker" in line, \
            "and the line it would be put back from is the fenced one"
    finally:
        tmux("kill-window", "-t", pane)
    assert not wait_for(lambda: (boxes() - before) & boxes(), gone=True)


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("docker"),
                    reason="needs docker and `sticky-chat sandbox --build`")
def test_a_tab_that_cannot_be_boxed_keeps_running(tmux, server, run_sticky,
                                                  project, tmp_path):
    """Every check happens before anything is killed: an image that is not
    there, or an agent that is not in it, has to leave the tab exactly as it
    was rather than respawn a pane onto a command that dies - which would
    take the window and the sidebar with it.

    The agent here is a script outside the project, so the container cannot
    see it: only the project is mounted."""
    outside = tmp_path / "agent.sh"
    outside.write_text("#!/bin/sh\nexec bash --norc -i\n")
    outside.chmod(0o755)
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--agent-cmd", str(outside)).strip()
    assert pane
    try:
        with pytest.raises(RuntimeError, match="not built"):
            run_sticky("refence", "--sandbox", "docker", "--pane", pane,
                       "--sandbox-image", "sticky-nosuch:latest")
        with pytest.raises(RuntimeError, match="has no"):
            run_sticky("refence", "--sandbox", "docker", "--pane", pane)
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_home").strip() == "", "it did not move"
        assert tmux("display-message", "-p", "-t", pane,
                    "#{pane_dead}").strip() in ("0", ""), "and it is alive"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip(), "sidebar still beside it"
    finally:
        tmux("kill-window", "-t", pane)


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("podman"),
                    reason="needs podman, its machine up, and "
                           "`sticky-chat sandbox --build podman`")
def test_a_podman_tab_runs_as_you_and_writes_files_you_own(
        tmux, server, run_sticky, project):
    """The one thing podman could get wrong that docker cannot.

    Rootless podman has already put us in a user namespace before `run` is
    typed, so a container that is not told to keep our uid runs either as root
    - where Claude Code refuses to skip permission prompts - or as a subuid,
    where every file the agent writes into the bind-mounted project comes back
    owned by 100500 and cannot be edited afterwards. Both of those are quiet
    failures: the tab opens, the agent draws, and the damage is found later.
    So this asks the pane, from inside the container, who it is and what it
    left behind.

    A shell rather than an agent, because a container with no login in it
    would spend this test on a sign-in screen."""
    before = boxes("podman")
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--sandbox", "podman",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane, "the tab did not open"
    try:
        tmux("send-keys", "-t", pane,
             "echo WHO=$(id -u):$(id -g); touch written-inside; "
             "ls /Users 2>&1 | head -1; echo DONE", "Enter")
        for _ in range(60):
            screen = tmux("capture-pane", "-p", "-t", pane)
            if "\nDONE" in screen:
                break
            time.sleep(0.3)
        assert "\nDONE" in screen, screen
        assert f"WHO={os.getuid()}:{os.getgid()}" in screen, \
            f"the container is not running as you:\n{screen}"
        assert "WHO=0:0" not in screen, \
            "the agent is root in there, and will not skip permission prompts"
        assert ("No such file" in screen or "cannot access" in screen), \
            f"the rest of your disk is visible from in there:\n{screen}"
        wrote = os.path.join(project, "written-inside")
        assert os.path.exists(wrote), "the project is not writable from inside"
        assert os.stat(wrote).st_uid == os.getuid(), \
            "it wrote a file you do not own"
        home = tmux("show-options", "-pqv", "-t", pane, "@sticky_home").strip()
        assert home and os.path.isdir(home), "the agent's home is on this disk"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip(), "and it has a sidebar"
        mine = wait_for(lambda: boxes("podman") - before)
        assert mine, "the tab is a podman container"
    finally:
        tmux("kill-window", "-t", pane)
    assert not wait_for(lambda: mine & boxes("podman"), gone=True), \
        "and closing the tab takes the container with it"


@pytest.mark.integration
@pytest.mark.skipif(not engine_ready("podman"),
                    reason="needs podman, its machine up, and "
                           "`sticky-chat sandbox --build podman`")
def test_a_running_tab_moves_into_a_podman_box_and_back(tmux, server,
                                                        run_sticky, project):
    """`C-g B`'s work, done to podman. The pane is respawned rather than
    replaced, and a `podman run` client asked to stop has to forward that to
    its container the way a docker client does - or unboxing would leave a
    container running with nothing attached to it."""
    before = boxes("podman")
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--sandbox", "off",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane
    try:
        partner = tmux("show-options", "-pqv", "-t", pane,
                       "@sticky_partner").strip()
        run_sticky("refence", "--sandbox", "podman", "--pane", pane)
        home = wait_for(lambda: tmux("show-options", "-pqv", "-t", pane,
                                     "@sticky_home").strip())
        assert home and os.path.isdir(home), "it is in a box"
        line = tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_agent_cmd").strip()
        assert "STICKY_SANDBOX=podman" in line, \
            "and the line it would be put back from says which box"
        mine = wait_for(lambda: boxes("podman") - before)
        assert mine, "with a podman container behind it"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip() == partner, \
            "and the same sidebar beside it"

        run_sticky("refence", "--sandbox", "off", "--pane", pane)
        for _ in range(60):
            if not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip():
                break
            time.sleep(0.3)
        assert not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip(), "and back out again"
        assert not wait_for(lambda: mine & boxes("podman"), gone=True), \
            "unboxing left the container behind"
    finally:
        tmux("kill-window", "-t", pane)


@pytest.fixture
def at_home(monkeypatch):
    """A home to hide, and a state directory inside it.

    Both pinned, because the namespace fence puts our home on top of yours
    and the interesting thing about the pair is that the source is under the
    target - which is true on a real machine and would not be true of a
    tmp_path. Nothing is written either side of it: these are strings on a
    command line."""
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.setattr("sticky_chat.sandbox.STATE_HOME", "/home/me/.sticky")
    return "/home/me"


def namespaced(box, sticky, command="claude --dangerously-skip-permissions",
               project="/home/me/Code/thing"):
    return box.bwrap_command(command, project, sticky.CLAUDE)


def binds(words, flag="--bind"):
    """Every (source, target) the line binds with `flag`."""
    return [(words[i + 1], words[i + 2])
            for i, word in enumerate(words[:-2]) if word == flag]


class TestWhatTheNamespaceFenceCanReach:
    """Untested in the only sense that matters - no kernel has ever been
    handed one of these lines - so what can be checked is checked: which
    paths are on it, which way round, and read-only or not."""

    def test_the_project_is_bound_writable_at_its_own_path(self, box, sticky,
                                                           at_home):
        """Not at /workspace, and not read-only: the same reasoning as the
        container, and the agent has to be able to write the code."""
        words = shlex.split(namespaced(box, sticky))
        here = "/home/me/Code/thing"
        assert (here, here) in binds(words)
        assert (here, here) not in binds(words, "--ro-bind")
        assert words[words.index("--chdir") + 1] == here

    def test_the_system_is_bound_read_only(self, box, sticky, at_home):
        """The point of this fence is the host's own toolchain, so /usr is in
        there - and the cost of that is that /usr is in there, which is why
        every one of them is read-only."""
        words = shlex.split(namespaced(box, sticky))
        every = binds(words, "--ro-bind") + binds(words, "--ro-bind-try")
        for path in box.MUST_BIND:
            assert (path, path) in binds(words, "--ro-bind"), path
        for path in box.MAY_BIND:
            # `-try`, because which of these a distribution has is its own
            # business and a bind of something absent is a fence that will
            # not start.
            assert (path, path) in binds(words, "--ro-bind-try"), path
        writable = [target for _, target in binds(words)]
        assert not [t for t in writable if any(t == p for p, _ in every)]

    def test_your_home_is_covered_by_one_of_ours(self, box, sticky, at_home):
        """The whole fence in one line: your home's own path, with a
        directory of ours on top of it, and HOME pointing at it so the two
        answers to "where is ~" agree."""
        words = shlex.split(namespaced(box, sticky))
        assert (box.agent_home("claude"), "/home/me") in binds(words)
        assert words[words.index("--setenv") + 1:
                     words.index("--setenv") + 3] == ["HOME", "/home/me"]
        assert "/home/me/.ssh" not in words
        assert box.home_of(namespaced(box, sticky)) == box.agent_home("claude")

    def test_the_home_goes_on_before_the_project(self, box, sticky, at_home):
        """The bug this order is the fix for: projects live under your home,
        and a home bound after the project hides it. Nothing about the line
        says so once the words are in the wrong order, so it is said here."""
        words = shlex.split(namespaced(box, sticky))
        order = [target for _, target in binds(words)]
        assert order.index("/home/me") < order.index("/home/me/Code/thing")

    def test_the_home_we_bind_lives_inside_the_home_it_covers(self, box,
                                                              sticky,
                                                              at_home):
        """Which sounds circular and is not: bwrap resolves a bind's source
        against the host's root, which it has pivoted aside, so covering a
        path cannot hide what lives under it. Worth pinning, because the day
        somebody "fixes" this by moving the sandbox directory is the day the
        reason for the order above is lost too."""
        assert box.agent_home("claude").startswith(at_home + "/")

    def test_a_machine_with_no_home_gets_the_container_s(self, box, sticky,
                                                         monkeypatch):
        """Nothing to hide, so there is nothing to hide it behind: the agent
        gets ours at the fixed path the container uses. It still parses,
        because the target is read off the line rather than assumed."""
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(box.os.path, "expanduser", lambda path: path)
        line = namespaced(box, sticky)
        assert box.real_home() == box.CONTAINER_HOME
        assert (box.agent_home("claude"), box.CONTAINER_HOME) \
            in binds(shlex.split(line))
        assert box.home_of(line) == box.agent_home("claude")

    def test_the_network_is_shared_and_nothing_else_is(self, box, sticky,
                                                       at_home):
        """`--unshare-all` and then the network back, because the agent's
        whole job is a conversation with an API. And `--die-with-parent`,
        which is what closing the tab has instead of a drain."""
        words = shlex.split(namespaced(box, sticky))
        assert words.index("--unshare-all") < words.index("--share-net")
        assert "--die-with-parent" in words

    def test_there_is_no_new_session(self, box, sticky, at_home):
        """setsid would leave the pane's session, and with it the pty's
        foreground process group: Ctrl-C would stop reaching the agent,
        which is the one key an agent tab cannot do without. What
        `--new-session` guards against needs a terminal shared with a
        process outside the fence, and this pane has none."""
        assert "--new-session" not in shlex.split(namespaced(box, sticky))

    def test_the_hosts_tmp_and_processes_are_not_in_there(self, box, sticky,
                                                          at_home):
        """A /tmp of its own is also how the tmux server's socket stops
        being something the agent can find and talk to."""
        words = shlex.split(namespaced(box, sticky))
        assert words[words.index("--tmpfs") + 1] == "/tmp"
        assert words[words.index("--proc") + 1] == "/proc"
        assert words[words.index("--dev") + 1] == "/dev"

    def test_a_secret_is_not_named_because_it_does_not_have_to_be(
            self, box, sticky, at_home, monkeypatch):
        """The other way round from docker, which names each variable it
        lets through: a namespace inherits the environment whole, so the key
        arrives without being written onto a line that gets stored. The one
        that is taken away is the agent socket, which is not in there."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        line = namespaced(box, sticky)
        assert "sk-secret" not in line and "ANTHROPIC_API_KEY" not in line
        words = shlex.split(line)
        assert words[words.index("--unsetenv") + 1] == "SSH_AUTH_SOCK"

    def test_your_own_arguments_come_last(self, box, sticky, at_home):
        """So that an argument of yours can widen what we decided - which is
        how a toolchain that is not under /usr gets in."""
        line = box.bwrap_command("claude", "/p", sticky.CLAUDE,
                                 ["--ro-bind-try", "/nix", "/nix"])
        words = shlex.split(line)
        assert words[words.index("/nix") - 1] == "--ro-bind-try"
        assert words.index("--chdir") < words.index("/nix")
        assert box.inner_command(line) == "claude"

    def test_no_image_is_named_anywhere_on_it(self, box, sticky, at_home):
        """There is nothing to build, which is most of the reason this fence
        exists: the compilers in there are the ones you already have."""
        assert box.DEFAULT_IMAGE not in namespaced(box, sticky)
        assert not box.BWRAP.uses_image


class TestTakingTheNamespaceOffAgain:

    def test_a_namespaced_line_reads_back(self, box, sticky, at_home):
        line = namespaced(box, sticky)
        assert box.inner_command(line) == \
            "claude --dangerously-skip-permissions"
        assert box.fenced_by(line) == "bwrap"
        assert box.home_of(line) == box.agent_home("claude")

    def test_the_marker_is_the_last_word_of_the_fence(self, box, sticky,
                                                      at_home):
        """Where docker has an image after it. bwrap ends its own arguments
        with `--` and the marker is the `env NAME=value` that follows, which
        is the whole reason `Fence.follows` exists."""
        fence, inside = box.split_command(namespaced(box, sticky))
        assert shlex.split(fence)[-3:] == ["--", "/usr/bin/env",
                                           "STICKY_SANDBOX=bwrap"]
        assert inside == "claude --dangerously-skip-permissions"
        assert box.BWRAP.follows == 0 and box.DOCKER.follows == 1

    def test_a_conversation_id_lands_on_the_agent(self, box, sticky, at_home):
        """The failure this whole split exists to stop, now with a second
        shape of fence in front of it: an id put first, where codex takes
        its subcommand, must land on the agent and not on bwrap."""
        for agent in (sticky.CLAUDE, sticky.CODEX):
            line = box.bwrap_command(agent.command, "/p", agent)
            named = sticky.with_words(line, ["--session-id", "abc"], agent)
            assert box.inner_command(named).startswith(
                agent.command.split()[0])
            assert "--session-id abc" in box.inner_command(named)
            assert "--session-id" not in box.split_command(named)[0]
            assert box.fenced_by(named) == "bwrap"

    def test_asking_what_it_names_asks_the_agents_half(self, box, sticky,
                                                       at_home):
        line = box.bwrap_command("claude --session-id abc", "/p",
                                 sticky.CLAUDE)
        assert sticky.session_named(line, sticky.CLAUDE) == "abc"
        assert sticky.names_a_session(line, sticky.CLAUDE)
        dropped = sticky.without_session(line, sticky.CLAUDE)
        assert box.fenced_by(dropped) == "bwrap", "still fenced"
        assert box.inner_command(dropped) == "claude"
        assert box.home_of(dropped) == box.agent_home("claude")

    def test_one_fence_can_be_swapped_for_another(self, box, sticky, at_home):
        """All three ways round, because `C-g B` and the prompt both do it
        to a line that already has a fence on it."""
        here = "/home/me/Code/thing"
        boxed = box.container_command("claude", here, sticky.CLAUDE)
        namely = box.bwrap_command("claude", here, sticky.CLAUDE)
        assert box.refence(boxed, box.BWRAP, here, sticky.CLAUDE) == namely
        assert box.refence(namely, box.DOCKER, here, sticky.CLAUDE) == boxed
        assert box.refence(namely, box.OFF, here, sticky.CLAUDE) == "claude"
        assert box.refence(namely, box.BWRAP, here, sticky.CLAUDE) == namely

    def test_a_namespaced_tab_comes_back_namespaced(self, box, sticky,
                                                    at_home):
        line = box.bwrap_command("claude", "/p", sticky.CLAUDE)
        back = sticky.resume_command({"command": line, "agent": "claude",
                                      "session": "1234",
                                      "agent_session": ""})
        assert box.fenced_by(back) == "bwrap"
        assert box.inner_command(back) == "claude --resume 1234"
        assert box.home_of(back) == box.agent_home("claude")

    def test_a_marker_we_do_not_recognise_still_splits(self, box):
        """A line written by a version with a fence this one has never heard
        of. Read as the only fence that ever carried a marker was - one word
        after it, an image - because a line split in the wrong place is a
        line an id lands in the middle of."""
        line = ("runner run -e STICKY_SANDBOX=nosuchfence some:image "
                "claude --resume abc")
        assert box.inner_command(line) == "claude --resume abc"
        assert box.fenced_by(line) == "nosuchfence"

    def test_the_docker_line_is_word_for_word_what_it_was(self, box, sticky,
                                                          monkeypatch):
        """The generalisation that let a second fence in went through
        `split_command`, `home_of` and `refence`, which are the three things
        the first fence is made of. So the first fence's line is pinned
        whole: not a word moved, not a word added."""
        monkeypatch.setattr(box, "relabel", lambda: "")
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        assert box.container_command("claude -c", "/Users/me/Code/thing",
                                  sticky.CLAUDE) == (
            "docker run --rm -it --init --label sticky-chat=agent "
            "-v /Users/me/Code/thing:/Users/me/Code/thing "
            "-w /Users/me/Code/thing "
            f"-v {box.agent_home('claude')}:/home/agent "
            "-e HOME=/home/agent "
            f"--user {os.getuid()}:{os.getgid()} "
            "-e TERM -e COLORTERM -e LANG -e LC_ALL "
            "-e CLAUDE_CODE_TMUX_SESSION -e CLAUDE_CODE_TMUX_PREFIX "
            "-e STICKY_SANDBOX=docker sticky-agent:latest claude -c")

    def test_the_namespace_line_is_word_for_word_this(self, box, sticky,
                                                      at_home):
        """The same pinning for the fence nobody has run, which is the only
        record of what was intended: if a flag here is wrong, this is where
        the correction gets made and seen."""
        assert namespaced(box, sticky, "claude -c") == (
            "bwrap --unshare-all --share-net --die-with-parent "
            "--ro-bind /usr /usr --ro-bind /etc /etc "
            "--ro-bind-try /bin /bin --ro-bind-try /sbin /sbin "
            "--ro-bind-try /lib /lib --ro-bind-try /lib32 /lib32 "
            "--ro-bind-try /lib64 /lib64 --ro-bind-try /opt /opt "
            "--ro-bind-try /sys /sys "
            "--ro-bind-try /run/systemd/resolve /run/systemd/resolve "
            "--ro-bind-try /run/resolvconf /run/resolvconf "
            "--ro-bind-try /run/NetworkManager /run/NetworkManager "
            "--proc /proc --dev /dev --tmpfs /tmp "
            "--bind /home/me/.sticky/sandbox/claude /home/me "
            "--setenv HOME /home/me "
            "--setenv DISABLE_AUTOUPDATER 1 --unsetenv SSH_AUTH_SOCK "
            "--bind /home/me/Code/thing /home/me/Code/thing "
            "--chdir /home/me/Code/thing "
            "-- /usr/bin/env STICKY_SANDBOX=bwrap claude -c")


class TestWhetherTheNamespaceFenceCanBeHad:

    def test_a_mac_is_told_what_it_is_rather_than_what_is_missing(
            self, box, monkeypatch):
        """Saying "bwrap is not installed" here would send somebody looking
        for a package that does not exist. It is a Linux fence, and saying
        that is the difference between a sentence and ten minutes."""
        monkeypatch.setattr(box.sys, "platform", "darwin")
        why = box.unavailable(box.BWRAP)
        assert "Linux" in why and "not installed" not in why

    def test_on_linux_it_asks_the_kernel_and_says_what_it_said(
            self, box, monkeypatch):
        """There is no one setting to read - Debian had a sysctl, Ubuntu has
        an AppArmor profile, a setuid bwrap needs neither - so it runs the
        thing, and what comes back is bwrap's own first line."""
        monkeypatch.setattr(box, "_NAMESPACES", [])
        monkeypatch.setattr(box.sys, "platform", "linux")
        monkeypatch.setattr(box.shutil, "which", lambda name: "/usr/bin/bwrap")
        calls = []

        class Done:
            returncode = 1
            stdout = ""
            stderr = "bwrap: No permissions to creating new namespace\n"

        def fake(command, **kw):
            calls.append(command)
            return Done()

        monkeypatch.setattr(box.subprocess, "run", fake)
        assert box.unavailable(box.BWRAP) == \
            "bwrap: No permissions to creating new namespace"
        assert box.unavailable(box.BWRAP), "and it is still refused"
        assert len(calls) == 1, "asked once and kept: a prompt waits on this"
        assert calls[0][0] == "bwrap" and "--unshare-all" in calls[0]

    def test_a_willing_kernel_is_also_remembered(self, box, monkeypatch):
        monkeypatch.setattr(box, "_NAMESPACES", [])
        monkeypatch.setattr(box.sys, "platform", "linux")
        monkeypatch.setattr(box.shutil, "which", lambda name: "/usr/bin/bwrap")
        calls = []

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(box.subprocess, "run",
                            lambda command, **kw: calls.append(1) or Done())
        assert box.unavailable(box.BWRAP) == ""
        assert box.unavailable(box.BWRAP) == ""
        assert len(calls) == 1

    def test_a_linux_with_neither_offers_neither(self, box, monkeypatch):
        """And the suggestion falls all the way through to no fence rather
        than to one that would take the tab with it."""
        monkeypatch.setattr(box.sys, "platform", "linux")
        monkeypatch.setattr(box.shutil, "which", lambda name: None)
        why = box.unavailable(box.BWRAP)
        assert why == "bwrap is not installed (the package is called " \
                      "bubblewrap)", "the program and the package differ"
        assert box.suggested_fence() == "off"
        assert box.default_fence() == "off"

    def test_the_container_is_preferred_where_there_is_a_choice(
            self, box, monkeypatch):
        """It fences more: a namespace shares the host's /usr, its kernel and
        its uid, and a container shares only the last two."""
        monkeypatch.setattr(box, "unavailable", lambda fence: "")
        assert box.suggested_fence() == "docker"

    def test_a_fence_nobody_has_run_is_never_the_default(self, box,
                                                         monkeypatch):
        """Offered by the prompt, chosen by hand, and remembered after that.
        A default that breaks the first tab somebody opens after an upgrade
        is worse than no default at all - and the whole of the fix is
        `tested=True`, the day a tab has been through it."""
        monkeypatch.setattr(box, "unavailable",
                            lambda fence: "" if fence is box.BWRAP else "no")
        assert not box.BWRAP.tested
        assert box.suggested_fence() == "off"
        box.remember("bwrap")
        assert box.default_fence() == "bwrap", "once you have said so"

    def test_the_help_says_which_one_is_unproven(self, box):
        """The way `agent_choices` says it of a profile nobody has run."""
        assert "bwrap (untested)" in box.fence_choices()
        assert "docker (untested)" not in box.fence_choices()

    def test_every_fence_is_offered_with_its_own_reason(self, box):
        """What the third line of the new-tab prompt is built from: one row
        per fence, its blurb, and why it cannot be used here."""
        assert [fence.name for fence in box.FENCES] == ["off", "docker",
                                                        "podman", "bwrap"]
        assert box.fence_named("bwrap") is box.BWRAP
        assert "bwrap" in box.fence_choices()
        assert box.unavailable(box.OFF) == ""


class TestWhetherTheAgentIsInThere:
    """The namespace fence runs the host's own binary, so "is the agent in
    there" is a question about paths - and the answer is no more often than
    it looks, because an agent installed by npm or by Claude's own installer
    lives under the home this fence replaces."""

    def test_a_program_under_usr_is_in_there(self, box):
        assert box.unreachable(box.BWRAP, "/usr/local/bin/claude", "/p") == ""

    def test_a_program_under_your_home_is_not(self, box, at_home):
        why = box.unreachable(box.BWRAP, "/home/me/.local/bin/claude", "/p")
        assert why and "would not be there" in why
        assert "the home bwrap replaces" in why, "which is why, precisely"
        assert "--sandbox-arg=/home/me/.local/bin" in why, \
            "and it says how to let it in, joined on so argparse takes it"

    def test_a_program_in_the_project_is_in_there(self, box, tmp_path):
        """A repository's own `./bin/agent` is bound writable with the rest
        of the project, so it is as much in there as /usr is."""
        agent = tmp_path / "bin" / "agent"
        agent.parent.mkdir()
        agent.write_text("#!/bin/sh\n")
        assert box.unreachable(box.BWRAP, str(agent), str(tmp_path)) == ""

    def test_an_argument_of_yours_lets_it_in(self, box, at_home):
        """`--sandbox-arg --ro-bind` is exactly how a toolchain outside
        these roots gets in, so a path you bound counts as an answer."""
        program = "/home/me/.local/bin/claude"
        assert box.unreachable(box.BWRAP, program, "/p",
                               ["--ro-bind", "/home/me/.local",
                                "/home/me/.local"]) == ""

    def test_a_program_in_none_of_the_roots_is_not_in_there(self, box,
                                                            at_home):
        """A toolchain in /srv or /nix is not refused because it is odd but
        because nothing binds it, and the message says which of the two
        things went wrong - that being the difference between reading it and
        reading it twice."""
        why = box.unreachable(box.BWRAP, "/srv/tools/agent", "/p")
        assert "none of the directories" in why
        assert "--sandbox-arg=/srv/tools" in why

    def test_a_program_nobody_has_gets_no_opinion(self, box):
        """A tab that would not have started unfenced either is not the
        fence's failure to report."""
        assert box.unreachable(box.BWRAP, "no-such-program-anywhere",
                               "/p") == ""

    def test_the_fence_that_has_an_image_answers_elsewhere(self, box, sticky):
        """`runs_in_image` starts a container to find out, which is a second
        and a key you pressed on purpose - not a thing to do on every tab."""
        assert box.unreachable(box.DOCKER, "/home/me/bin/claude", "/p") == ""
        assert box.unreachable(box.OFF, "/home/me/bin/claude", "/p") == ""

    def test_it_asks_of_the_agents_half_of_a_fenced_line(self, box, sticky,
                                                         at_home):
        """Because that is what it is handed: `cmd_refence` asks about a tab
        that is already behind one fence and about to be behind another."""
        line = box.container_command("/home/me/.local/bin/claude", "/p",
                                  sticky.CLAUDE)
        assert box.unreachable(box.BWRAP, line, "/p")


class TestWhichFenceANamespacedTabGets:

    def args(self, **fields):
        return argparse.Namespace(**{"sandbox": "", "agent_cmd": None,
                                     "sandbox_image": "", "sandbox_arg": [],
                                     **fields})

    def test_nothing_is_built_for_a_fence_that_has_no_image(self, sticky, box,
                                                            monkeypatch):
        """A first tab that went away for three minutes to build an image it
        would never run would be hard to forgive."""
        def never(*args, **fields):
            raise AssertionError("a namespace has no image to build")

        monkeypatch.setattr(sticky.commands, "ensure_image", never)
        monkeypatch.setattr(sticky.commands, "unavailable", lambda fence: "")
        got = sticky.fence_tab("/bin/sh", "/p", sticky.CLAUDE,
                               self.args(sandbox="bwrap"))
        assert box.fenced_by(got) == "bwrap"
        assert box.inner_command(got) == "/bin/sh"

    def test_an_agent_outside_the_fence_stops_a_tab_that_asked_for_it(
            self, sticky, monkeypatch):
        """Rather than opening a pane on a command that is not there, which
        dies and takes the window and the sidebar with it."""
        monkeypatch.setattr(sticky.commands, "unavailable", lambda fence: "")
        with pytest.raises(SystemExit):
            sticky.fence_tab("/home/me/.local/bin/claude", "/p", sticky.CLAUDE,
                             self.args(sandbox="bwrap"))

    def test_an_agent_outside_the_fence_makes_a_default_step_aside(
            self, sticky, monkeypatch, capsys):
        """The same rule the missing daemon gets: a preference may not stop a
        tab from opening."""
        monkeypatch.setattr(sticky.commands, "default_fence", lambda: "bwrap")
        monkeypatch.setattr(sticky.commands, "unavailable", lambda fence: "")
        got = sticky.fence_tab("/home/me/.local/bin/claude", "/p",
                               sticky.CLAUDE, self.args())
        assert got == "/home/me/.local/bin/claude"
        assert "bwrap sandbox" in capsys.readouterr().err


def bwrap_ready() -> bool:
    """Whether this machine can run the namespace fence at all.

    Which no machine this was written on could, so everything below is the
    part of the feature nobody has seen work. It is here so that the first
    Linux box to run the suite is the thing that finds out, rather than a
    tab somebody was trying to work in.
    """
    if sys.platform != "linux" or not shutil.which("bwrap"):
        return False
    try:
        done = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--share-net",
             "--die-with-parent", "true"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def namespaces() -> set[str]:
    """The sticky namespace fences running right now, by pid.

    A set, compared against what was there before, for the same reason
    `boxes` is one: a machine running this suite is a machine with tabs open
    on it.
    """
    done = subprocess.run(["pgrep", "-f", "STICKY_SANDBOX=bwrap"],
                          capture_output=True, text=True)
    return set(done.stdout.split())


@pytest.mark.integration
@pytest.mark.skipif(not bwrap_ready(),
                    reason="needs Linux and bubblewrap")
def test_a_namespaced_tab_is_a_tab_like_any_other(tmux, server, run_sticky,
                                                  project):
    """The whole point, end to end, for the fence that has never run: tmux
    starts bwrap as the pane's command, so the pane is a pane and the sidebar
    is beside it, and what changed is only what the agent can reach.

    A shell rather than an agent, because what is being tested is the pane
    and a pane does not know what is drawing into it. Three things are asked
    of it: the project is where it says it is, $HOME is the directory sticky
    made and not the one you live in - proved by a file put there from out
    here, which is also the proof that the transcript would be readable from
    out here - and the fence goes when the tab does.
    """
    before = namespaces()
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--sandbox", "bwrap",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane, "the tab did not open"
    try:
        home = ""
        for _ in range(40):
            home = tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip()
            if home:
                break
            time.sleep(0.3)
        assert home and os.path.isdir(home), "the agent's home is on this disk"
        with open(os.path.join(home, "sentinel"), "w", encoding="utf-8") as fh:
            fh.write("ours\n")
        tmux("send-keys", "-t", pane,
             "pwd; cat $HOME/sentinel; ls $HOME/.ssh 2>&1 | tail -1", "Enter")
        want = project.rstrip("/")
        screen = ""
        for _ in range(40):
            screen = tmux("capture-pane", "-p", "-t", pane).replace("\n", "")
            if "ours" in screen:
                break
            time.sleep(0.3)
        assert want in screen, "the project is not where it says it is"
        assert "ours" in screen, "$HOME is not the directory sticky made"
        partner = tmux("show-options", "-pqv", "-t", pane,
                       "@sticky_partner").strip()
        assert partner, "and it has a sidebar, like any other tab"
        mine = wait_for(lambda: namespaces() - before)
        assert mine, "the tab is a namespace"
    finally:
        tmux("kill-window", "-t", pane)
    # `--die-with-parent`, which is what this fence has instead of a drain and
    # a forwarded signal.
    assert not wait_for(lambda: mine & namespaces(), gone=True), \
        "closing the tab left the fence running"


@pytest.mark.integration
@pytest.mark.skipif(not bwrap_ready(),
                    reason="needs Linux and bubblewrap")
def test_a_running_tab_moves_into_a_namespace_and_back(tmux, server,
                                                       run_sticky, project):
    """`C-g B` and `C-g b` again, for the other fence. The pane is respawned
    rather than replaced, so the window, the sidebar and everything hung off
    the pane stay while what is running inside changes."""
    before = namespaces()
    pane = run_sticky("start", project, "--detach", "--agent", "shell",
                      "--agent-cmd", "bash --norc -i").strip()
    assert pane
    try:
        partner = tmux("show-options", "-pqv", "-t", pane,
                       "@sticky_partner").strip()
        run_sticky("refence", "--sandbox", "bwrap", "--pane", pane)
        home = wait_for(lambda: tmux("show-options", "-pqv", "-t", pane,
                                     "@sticky_home").strip())
        assert home and os.path.isdir(home), "it is in a namespace"
        mine = wait_for(lambda: namespaces() - before)
        assert mine, "with a bwrap behind it"
        assert tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_partner").strip() == partner, \
            "and the same sidebar beside it"
        line = tmux("show-options", "-pqv", "-t", pane,
                    "@sticky_agent_cmd").strip()
        assert "STICKY_SANDBOX=bwrap" in line, \
            "and the line it would be put back from is the fenced one"

        run_sticky("refence", "--sandbox", "off", "--pane", pane)
        for _ in range(40):
            if not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip():
                break
            time.sleep(0.3)
        assert not tmux("show-options", "-pqv", "-t", pane,
                        "@sticky_home").strip(), "and back out again"
        assert not wait_for(lambda: mine & namespaces(), gone=True), \
            "unfencing left the old one running"
    finally:
        tmux("kill-window", "-t", pane)
