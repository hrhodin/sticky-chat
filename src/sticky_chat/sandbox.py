"""A fence around the agent: what it may read, change and delete.

An agent tab runs with the permission prompts turned off, which is the only
way to work in it and also the whole of the risk: whatever the agent decides
to run, runs, with your account behind it. The fence is the answer to
"whatever it decides to run, where can it reach?" - and the honest answer on
a bare machine is "everywhere you can", which is your ssh keys, your browser
profile, every other repository on the disk and the disk itself.

What is here is three fences, and the shape for others. `docker` and `podman`
are the same fence with a different engine behind it; `bwrap` is the same
fence again with no container at all.

`docker` runs the agent inside a container that has the project bind-mounted
at the same absolute path it has outside, a home directory of its own, and
nothing else of yours: `rm -rf ~` in there takes out a directory sticky made
and rebuilds on the next tab. The container is not a security boundary
against a determined attacker with a kernel bug, and the warning in Claude
Code's own dev container page applies here too - a hostile repository can
still exfiltrate anything the container can reach, which is the project and
the network. It is a boundary against the thing that actually happens: an
agent that misreads an instruction and deletes, rewrites or reads the wrong
tree.

`podman` is that container with no daemon behind it, and rootless by default -
which on Linux is the difference between a fence and membership of a group
that is root in all but name. One flag separates the two engines, and it is
the one that matters: see `keep_id`. They keep an image store each, so the
same tag names two images and every image question has to say which engine it
is asking.

`bwrap` is the same fence drawn without a container. Bubblewrap makes a mount
namespace and runs the agent in it, so the agent is an ordinary process of
yours with an extraordinary view of the disk: the project at its own path, a
home of ours over the top of yours, `/usr` and `/etc` read-only, and nothing
else. No daemon, no image, no rebuild - and, the reason it is worth having at
all, the host's own toolchain. A container image has to be taught python,
cargo and a jdk before a project can be built in it, once per project and
again whenever the project moves on; a namespace inherits the compilers you
already have, because they are still /usr's. What it gains in convenience it
gives up in isolation, and `bwrap_command` says exactly where: the kernel,
the uid and the network are shared. It is written from bubblewrap's
documentation and has never been run - Linux only, and this was written on a
Mac - which `bwrap_command` also says.

The three things that make this fit sticky rather than sit beside it:

*The pane runs the container.* tmux starts `docker run -it ...` as the tab's
command, the docker client hands the container's stdio to that pty, and the
agent draws into the pane exactly as it did before. tmux, sticky, the
sidebar, the notes and the placer all stay on the host and none of them can
tell the difference: they read the pane, not the process.

*The project is at the same path inside.* `-v /Users/you/Code/thing` onto
`/Users/you/Code/thing` rather than onto `/workspace`, so every path either
side says is the same path - which matters most where the agent writes down
what it called the conversation, because the directory it writes it in is
named after the project's own path.

*The home comes back out.* The container's home is a directory of ours on the
host, so the login survives a rebuild and the transcript the sidebar reads -
the record that says the agent has run out of turns - is a file we can open
without entering the container at all. `home_of` is how a tab says where.

The command line is the record. `container_command` puts the container in
front of the agent's own command, `split_command` takes it off again, and
everything that edits a command line - adding the conversation id, taking it
out again for a new tab, asking which one it names - goes through
`split_command` first, so a flag meant for the agent can never land on
`docker run`. It finds its way by the `STICKY_SANDBOX=<fence>` the composer
always puts last, which is why none of that cares which engine ran it.

Two engines, one shape. `docker` and `podman` differ in one thing that
matters and several that do not. The one that matters is who the container
runs as: docker's daemon is root, so `--user 501:20` is simply obeyed, while
rootless podman has already put you in a user namespace where your own uid is
the container's root and uid 501 inside is subuid 100500 outside - the same
`--user` would leave the files it wrote in your project owned by a number you
have no account for, and running without it would hand the agent root, which
Claude Code refuses to skip permission prompts as. `--userns=keep-id` is the
flag that undoes that, and the explicit `keep-id:uid=,gid=` form is the one
used here, because on macOS the user podman is mapping is the Linux user
inside the machine VM - `core`, with a gid of its own - and not the account
whose uid the image's `agent` was built with. See `container_command`.

The several that do not matter, and were checked rather than assumed: `-e
NAME` with no value takes the value from the host environment on both and is
silently dropped on both when there is none; `--init`, `--label` and
`--filter label=` behave the same; a locally built short tag resolves to the
local image rather than a registry; and a `podman run` client asked to stop
forwards that to its container exactly as a docker client does, which is what
`drain` relies on. The one thing that is not shared is the image store: the
same tag built by each engine is two images, and `image_built` has to ask the
engine the tab will actually use.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

from .agents import Agent, conversation_file
from .util import STATE_HOME, shell_quote

# ------------------------------------------------------------------ layout

# Where the container's home lives on the host: one directory per agent
# profile, because a login, a shell history and a package cache belong to the
# agent that wrote them rather than to all of them at once.
def sandbox_dir() -> str:
    return os.path.join(STATE_HOME, "sandbox")


def agent_home(agent: str) -> str:
    """The host directory a sandboxed `agent` tab gets as its home.

    One home per agent and not one per agent and engine, which was a real
    question: a home written into by two containers that disagreed about who
    you are is a home half of whose files the other one cannot read. It is
    the same home because both engines are made to run as the same uid - see
    the `keep_id` row of the fence table - so a login docker wrote is a login
    podman can use, and `C-g B` from a docker tab to a podman one carries no
    conversation anywhere because the conversation is already there. The cost
    of being wrong about that is a permission error in a tab, which is loud;
    the cost of splitting them would have been a second sign-in and a
    transcript the sidebar reads from the wrong directory, which is quiet.
    """
    return os.path.join(sandbox_dir(), agent)


# Where that directory is mounted. Fixed, because it is written into every
# command line we generate and parsed back out of them: see `home_of`.
CONTAINER_HOME = "/home/agent"

# The image a container tab runs, and the file it is built from. The file is
# written once and never rewritten - it is yours to edit from then on, and
# `sticky-chat sandbox --build` picks up what you changed.
#
# One tag for every engine, rather than `sticky-agent-podman:latest` beside
# it. docker and podman keep separate image stores, so this one name is
# already two images and there is nothing for them to collide over - and a
# tag is a name for what an image is, which is the same thing built from the
# same Dockerfile either way. What follows from the stores being separate is
# that "is it built" has an answer per engine rather than per machine: every
# question about the image carries the cli that is being asked, and
# `sticky-chat sandbox` prints one line per fence rather than one for the
# image.
DEFAULT_IMAGE = "sticky-agent:latest"

# Set inside the container so the agent can tell it is fenced, and - because
# `container_command` always puts it last of the engine's own arguments - the
# word that tells our parser where those arguments end and the agent's begin.
# Its value is the fence's name, so a line records which fence it carries and
# not merely that it carries one.
FENCE_ENV = "STICKY_SANDBOX"

# What the namespace fence binds read-only: the parts of the host's root that
# have to exist before any program starts. `MUST_BIND` because a Linux
# without /usr or /etc is not one anything runs on, and failing loudly beats
# a tab that comes up unable to find `sh`. `MAY_BIND` because which of the
# rest a distribution has is its own business - a merged-usr machine has
# /bin as a symlink, a 32-bit one has /lib32, and /etc/resolv.conf is a
# symlink into /run on every machine whose resolver writes it there - three
# of them, because systemd-resolved, resolvconf and NetworkManager each
# choose a different directory, and a fence that has the network but no
# names to use it on fails as "could not reach the API". So
# `--ro-bind-try` takes what is there and passes over what is not.
#
# Fixed lists, rather than a look at this disk as the line is built, because
# the line is written down in a window record and replayed later - by
# `resume`, possibly on another day. A command line that means something
# different on the machine that replays it is a bug nobody finds.
MUST_BIND = ("/usr", "/etc")
MAY_BIND = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/opt", "/sys",
            "/run/systemd/resolve", "/run/resolvconf",
            "/run/NetworkManager")

# How a namespace tab carries the marker. It has to be one word of the form
# NAME=value, because that is what every parser of ours looks for, and bwrap
# spells its own environment `--setenv NAME VALUE`, which is two words and
# would be found by none of them. `env` says it in one, and then execs the
# agent rather than staying between it and the pane. By absolute path - the
# one path every Linux agrees on - so the fence does not depend on the PATH
# it happened to inherit.
ENV_PROGRAM = "/usr/bin/env"

DOCKERFILE = """\
# The image a sandboxed sticky-chat tab runs in.
#
# Written by sticky-chat the first time a tab asked for a container, and
# never rewritten: this file is yours now. Add the toolchain your projects
# build with, then `sticky-chat sandbox --build` to pick the changes up.
#
# What a tab mounts into it: the project at its own absolute path, and
# ~/.sticky/sandbox/<agent> as /home/agent. Nothing else of the host is
# visible from in here.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl git less procps ripgrep \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# The user the agent runs as: you, by number. A container writing into the
# bind-mounted project as anybody else leaves you files you cannot edit, and
# an agent running as root is an agent that refuses to skip permission
# prompts. The account exists rather than being conjured by `--user` alone
# because node asks the system who it is running as, and a number with no
# name behind it is an error rather than an answer.
ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID agent 2>/dev/null || true \\
    && useradd -u $UID -g $GID -d /home/agent -s /bin/bash agent \\
    && mkdir -p /home/agent && chown $UID:$GID /home/agent
ENV HOME=/home/agent
# The image is the version. An agent that updated itself inside a container
# that is thrown away at exit has updated nothing.
ENV DISABLE_AUTOUPDATER=1
WORKDIR /home/agent
"""


# ------------------------------------------------------------------ fences


@dataclass(frozen=True)
class Fence:
    """One way of fencing an agent in, and what to say about it."""

    name: str
    label: str
    blurb: str                  # one line: what the agent can reach
    # The second half of the answer, for `sticky-chat sandbox` rather than
    # for the prompt: what this fence asks of you. A prompt you see every
    # time you open a tab is not the place for a paragraph, and the question
    # it has to answer is "how much do I trust this agent with this
    # repository" - but the thing people are caught by is the other
    # question, which is what the fence costs to keep working.
    costs: str = ""
    needs: str = ""             # the program that has to be installed
    tested: bool = True         # whether anybody has run a tab behind it
    caveat: str = ""            # what exactly is unproven, when some of it is
    # The program that runs the container, for a fence that has one. Apart
    # from `needs` because the two are not the same question: `needs` is what
    # has to be on the path before the fence can be offered, and this is what
    # gets a `run`, a `build` and an `image ls` typed at it. A fence that
    # fences without an engine - a chroot, a seccomp profile - needs the
    # first and has nothing to put in the second.
    cli: str = ""
    # What to say when the cli is installed but will not answer. One per
    # fence because the fix is not the same: docker has a daemon to start and
    # podman, on anything but Linux, has a virtual machine.
    down: str = ""
    # Whether a container of this engine has to be told to keep your uid.
    # Rootless podman maps your own uid to root inside the container and
    # everything above it to subuids, so `--user <you>` means a subuid there
    # and files written into the bind-mounted project come back owned by a
    # number with no account behind it. `--userns=keep-id` is what makes the
    # uid inside the container the one outside it. docker's daemon is root
    # and needs none of this; asking it for `keep-id` would be an error.
    #
    # Checked on macOS, where podman's engine lives in a virtual machine and
    # virtiofs hands files back owned by you whatever the container thought.
    # On Linux, where it is the uid that decides, this is the flag podman
    # documents for it - and it needs the subuid ranges a distribution's
    # `useradd` normally writes into /etc/subuid: a machine without them has a
    # podman that runs and a `keep-id` that will not.
    keep_id: bool = False
    # How many of the fence's own words come after the marker before the
    # agent's command starts. The container has one, the image; bubblewrap
    # has none, because it ends its own arguments with `--` and the marker
    # is the `env NAME=value` that follows. `split_command` reads the fence
    # name out of the marker and asks here, so where the boundary is gets
    # declared once by the fence rather than counted in four callers.
    #
    # Defaults to the container's shape rather than to none, because that is
    # the shape a fence that runs an image has and the next one along will
    # be one. A wrong answer here splits a command line in the wrong place,
    # which is a conversation id landing in the middle of a runner's
    # arguments, so the default is the one that is right more often.
    follows: int = 1
    # Whether a tab behind this fence runs an image that has to be built
    # first. The container does, which is what `ensure_image`, the
    # Dockerfile and `runs_in_image` are all for; a namespace runs the
    # host's own programs, so there is nothing to build and the question
    # "is the agent in there" is a question about paths instead.
    uses_image: bool = False


OFF = Fence(
    name="off",
    label="no fence",
    blurb="every file you own, and nothing in the way",
    costs="nothing to set up, and nothing in the way of an accident either",
)

DOCKER = Fence(
    name="docker",
    label="container",
    blurb="this project only; the toolchain is the image's",
    costs="an image to build, and what your project builds with added to it; "
          "one sign-in inside, because your login is not readable from in there",
    needs="docker",
    cli="docker",
    down="the docker daemon is not running",
    follows=1,                  # the image, which the marker sits in front of
    uses_image=True,
)

PODMAN = Fence(
    name="podman",
    label="container",
    blurb="the same, rootless: no daemon, no docker group",
    costs="the same image, built once more: each engine keeps its own store",
    needs="podman",
    cli="podman",
    down="podman is not answering - on macOS or Windows its virtual machine "
         "has to be up: podman machine start",
    keep_id=True,
    follows=1,                  # the image, as docker's
    uses_image=True,
)

# Untested, in the sense `agents.py` uses the word: written from bubblewrap's
# own documentation and its argument parser, checked as far as a command line
# can be checked without a kernel that has namespaces, and never once run.
# The caveat is not "it might not work" but which part is unproven - see
# `bwrap_command`, which is where the reasoning it was written from is.
BWRAP = Fence(
    name="bwrap",
    label="Linux namespaces",
    blurb="this project plus your own toolchain, not your home",
    costs="nothing to build - it runs the programs you have; Linux only",
    needs="bwrap",
    follows=0,                  # nothing: `--` and the marker end the line
    tested=False,
    caveat=("the command line it composes is checked and bwrap itself has "
            "never run it: it is Linux only and this was written on a Mac"),
)


FENCES = (OFF, DOCKER, PODMAN, BWRAP)

# What is true of all of them, and so belongs under the list rather than in
# any one row. All three are things people are caught by: an agent that can
# reach the API can reach anything else it likes through the same hole; a
# container's compilers are the image's, which is how a tab in a box came to
# be unable to run the tests of the project it was boxed for; and "nothing
# else of yours" means nothing - a mounted share, a sibling checkout and
# /Volumes are all simply absent, which is the fence working and reads as a
# missing disk until you know it.
FENCE_NOTES = (
    "none of them fence the network: what reaches the API reaches the rest",
    "a container holds this project and nothing else - a mounted drive, a "
    "sibling repo, /Volumes: put one in with --sandbox-arg=-v, and end it "
    ":ro to put it in read-only",
    "a container's toolchain is the image's, so what your project builds "
    "with goes in the Dockerfile",
)


def fence_named(name: str) -> Fence | None:
    for fence in FENCES:
        if fence.name == (name or "").strip().lower():
            return fence
    return None


def fence_choices() -> str:
    """The names for --help, the same way `agent_choices` says them: a fence
    nobody has run says so where it is offered, not in a paragraph
    elsewhere."""
    return ", ".join(fence.name if fence.tested else f"{fence.name} (untested)"
                     for fence in FENCES)


def unavailable(fence: Fence) -> str:
    """Why this fence cannot be used here, or empty if it can.

    Asked before a tab is opened rather than after, because a container that
    turns out not to be there takes the tab with it: the pane's command
    fails, the pane dies, and what is left on screen is a sidebar with
    nothing beside it.
    """
    if not fence.needs:
        return ""
    if fence is BWRAP and sys.platform != "linux":
        # Named for what it is rather than for what is missing. Bubblewrap
        # fences with namespaces, and a machine with no namespaces to
        # unshare has no bwrap to install either: "bwrap is not installed"
        # would send somebody looking for a package that does not exist,
        # and on a Mac that is a brew search and ten minutes.
        return "bwrap fences with Linux namespaces, and this is not Linux"
    if fence is BWRAP and not shutil.which(fence.needs):
        # Said with the package name, because the program is `bwrap` and the
        # package is `bubblewrap` everywhere - which is the kind of
        # difference that costs somebody an `apt install bwrap` and then a
        # search to find out why it was not that.
        return "bwrap is not installed (the package is called bubblewrap)"
    if not shutil.which(fence.needs):
        return f"{fence.needs} is not installed"
    if fence.cli and not daemon_running(fence.cli):
        return fence.down or f"{fence.cli} is not answering"
    if fence is BWRAP:
        return namespaces_refused()
    return ""


# Asked once per process, and asked early. The answer costs the best part of
# a second - it is a round trip to a daemon - and every prompt that offers a
# fence needs it. A second of silence in front of a prompt is a second of
# keystrokes typed into nothing, because a prompt puts the terminal into raw
# mode as it starts and that throws away whatever was typed before it got
# there: it cost a `C-u`, and the tab opened on a path with itself in it
# twice. So `warm_up` asks in the background while you are still answering
# an earlier question, and by the time the fence is offered it is known.
#
# One answer per engine, because a machine can have both installed with only
# one of them up: a single cached boolean would have had podman inherit
# docker's answer, offered a fence that cannot start and taken the tab with
# it.
_DAEMON: dict[str, bool] = {}
_ASKING = threading.Lock()


def daemon_running(cli: str = DOCKER.cli) -> bool:
    """Whether `cli` has something behind it that will answer.

    Asked with a bare `info` and no `--format`. The template that was here
    before, `{{.ServerVersion}}`, is a docker field: podman's `info` has no
    such key, so the template fails to execute, podman exits non-zero, and a
    probe that only reads the exit status concludes the engine is down while
    it is running containers. A field neither of them shares is not worth the
    round trip it would save.
    """
    with _ASKING:
        if cli in _DAEMON:
            return _DAEMON[cli]
        try:
            done = subprocess.run([cli, "info"], capture_output=True,
                                  text=True, timeout=20)
            answer = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            answer = False
        _DAEMON[cli] = answer
        return answer


# The same question for the namespace fence: will this kernel make the
# namespaces? Asked by running bwrap rather than by reading a setting,
# because there is no one setting to read - Debian had
# `kernel.unprivileged_userns_clone`, Ubuntu 24.04 decides it per binary in
# an AppArmor profile, RHEL had a maximum of zero for a while, and a bwrap
# that is setuid root needs none of them to be right. Reading any one of
# them and guessing would refuse a machine that works, which is the worse
# mistake: the fence you cannot turn on is the fence you run without.
#
# Cheap where the docker question is slow - a namespace is a syscall, not a
# round trip to a daemon - but it is still a process, so it is asked once and
# kept, and `warm_up` asks it in the background like the other. Its own lock,
# so the daemon question timing out cannot hold this answer up behind it.
#
# A list of one, which may be the empty string: "" means the kernel is
# willing, and a list holding "" is still a list that has been filled in.
_NAMESPACES: list[str] = []
_PROBING = threading.Lock()


def namespaces_refused() -> str:
    """Why bwrap will not start here, or empty if it will.

    The smallest sandbox bubblewrap documents, asking for exactly the
    namespaces a tab asks for, running the smallest program there is. What
    comes back when it fails is bwrap's own first line - "No permissions to
    creating new namespace" and its relatives - which is a better message
    than anything we could write, because it is the message the person
    searching for it will find.
    """
    with _PROBING:
        if _NAMESPACES:
            return _NAMESPACES[0]
        try:
            done = subprocess.run(
                ["bwrap", "--ro-bind", "/", "/", "--unshare-all",
                 "--share-net", "--die-with-parent", "true"],
                capture_output=True, text=True, timeout=20)
            said = "" if done.returncode == 0 else next(
                (line.strip() for line in (done.stderr or "").splitlines()
                 if line.strip()),
                f"bwrap would not start (it exited {done.returncode})")
        except (OSError, subprocess.SubprocessError) as exc:
            said = f"bwrap would not start: {exc}"
        _NAMESPACES.append(said)
        return said


def warm_up() -> None:
    """Start finding out what this machine can run, and do not wait for it.

    Every engine that is installed, in parallel: they are independent round
    trips, and the prompt has to print all of them at once.
    """
    for fence in FENCES:
        if (fence.cli and fence.cli not in _DAEMON
                and shutil.which(fence.needs)):
            threading.Thread(target=daemon_running, args=(fence.cli,),
                             daemon=True).start()
    if (not _NAMESPACES and sys.platform == "linux"
            and shutil.which(BWRAP.needs)):
        threading.Thread(target=namespaces_refused, daemon=True).start()


# ------------------------------------------------------------- the choosing


def choice_file() -> str:
    return os.path.join(sandbox_dir(), "choice")


def remembered() -> str:
    """The fence chosen last time, or empty if nobody has chosen one yet."""
    try:
        with open(choice_file(), encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        return ""
    return name if fence_named(name) else ""


def remember(name: str) -> None:
    """Keep this choice as the next tab's default."""
    if not fence_named(name):
        return
    try:
        os.makedirs(sandbox_dir(), exist_ok=True)
        with open(choice_file(), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
    except OSError:
        pass                            # a default is not worth failing over


def default_fence() -> str:
    """What to offer when a tab is opened: last time's answer, or the best
    fence this machine can actually run.

    A fence you have to ask for is a fence nobody has on, so the first tab on
    a machine with docker running is offered the container. The answer is
    still a prompt away, and once it is given it is what comes back here.

    The first engine in the table that this machine can run, which on a
    machine with both is docker - not because it is better but because it was
    here first and a default that moved when podman was installed would be a
    surprise. `sticky-chat sandbox --set podman` is the answer to wanting the
    other one, and it is remembered above.

    The container first, where there is a choice, because it fences more: a
    namespace shares the host's /usr, the host's kernel and the host's uid,
    and a container shares only the last two.

    And only fences somebody has run. An untested fence is not a thing to
    hand to a machine that never asked for one - a default that breaks the
    first tab after an upgrade is worse than no default - so `bwrap` is
    offered by the prompt, chosen by hand, and from then on remembered like
    any other answer. The day a tab has been taken through it end to end,
    `tested=True` is what puts it in this list.
    """
    chosen = remembered()
    if chosen:
        return chosen
    for fence in FENCES:
        if fence is not OFF and fence.tested and not unavailable(fence):
            return fence.name
    return OFF.name


def engine_named(name: str = "") -> Fence | None:
    """The fence an image is being built for, or None if `name` has no engine.

    `sticky-chat sandbox --build` has to know which store it is filling now
    that there is more than one. An empty `name` is "the one a new tab would
    open in", so a machine with one engine never has to be told which engine
    it means; a machine with both builds for the fence its tabs are actually
    using, which is what `--set` and the prompt have been writing down all
    along. docker last of all rather than nothing, so that a machine with no
    engine at all is told what is missing instead of being asked to rephrase.
    """
    if name:
        fence = fence_named(name)
        return fence if fence and fence.cli else None
    preferred = fence_named(default_fence())
    if preferred and preferred.cli:
        return preferred
    for fence in FENCES:
        if fence.cli and not unavailable(fence):
            return fence
    return DOCKER


# ------------------------------------------------------------------ the image


def dockerfile_path() -> str:
    return os.path.join(sandbox_dir(), "Dockerfile")


def write_dockerfile() -> str:
    """Put the Dockerfile in place if it is not there, and say where it is.

    Never overwritten. The point of it being a file on disk rather than a
    string in here is that you can add your toolchain to it, and a build
    that quietly reverted your edits would be worse than no file at all.
    """
    path = dockerfile_path()
    if not os.path.exists(path):
        os.makedirs(sandbox_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(DOCKERFILE)
    return path


def image_built(image: str = DEFAULT_IMAGE, cli: str = DOCKER.cli) -> bool:
    """Whether the image is here already, in `cli`'s own store.

    Which store is the whole of why the cli is a parameter: docker and podman
    do not share one, so the same tag built by one of them is not there for
    the other, and asking docker whether a podman tab can start would answer
    yes and then respawn a pane onto a `podman run` that pulls nothing and
    dies.

    Asked with `image ls` rather than `image inspect`, which on docker 29
    answers "no such image" for an image it will happily run: the build puts
    a multi-platform index in the containerd store and inspect does not read
    one. A wrong answer here is a rebuild before every tab.
    """
    try:
        done = subprocess.run([cli, "image", "ls", "-q", image],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def build_image(image: str = DEFAULT_IMAGE, cli: str = DOCKER.cli,
                quiet: bool = False) -> str:
    """Build the image the tabs run in. Empty string when it worked.

    Output goes where the caller's does, because the first build takes
    minutes and a prompt that sits there saying nothing reads as a hang.

    The same Dockerfile for either engine, and the same build arguments:
    podman reads the file docker wrote, and the account it bakes in is the
    one you are, by number, because that is the uid both engines are then
    told to run as.
    """
    path = write_dockerfile()
    command = [cli, "build", "-t", image, "-f", path,
               "--build-arg", f"UID={os.getuid()}",
               "--build-arg", f"GID={os.getgid()}",
               sandbox_dir()]
    try:
        done = subprocess.run(command,
                              capture_output=quiet,
                              text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if done.returncode:
        tail = (done.stderr or "").strip().splitlines()[-5:] if quiet else []
        return "\n".join([f"{cli} build failed", *tail])
    return ""


def ensure_image(image: str = DEFAULT_IMAGE, cli: str = DOCKER.cli,
                 quiet: bool = False) -> str:
    """The image, built if this is the first tab to ask for it.

    Once per engine rather than once per machine: a tab that is the first to
    ask podman for it builds it again, in podman's store, however many times
    docker has already built the same tag in its own.
    """
    if image_built(image, cli):
        return ""
    if image != DEFAULT_IMAGE:
        # Somebody else's image: building it from our Dockerfile would
        # produce something that is not what they named, under their name.
        return f"no such image: {image}"
    print(f"sticky: building {image} for {cli} - this happens once per "
          f"engine, and takes a while")
    return build_image(image, cli, quiet=quiet)


# -------------------------------------------------------- wrapping a command


def relabel() -> str:
    """The suffix a bind mount needs on this machine, if any.

    On a distribution with SELinux enforcing - Fedora, RHEL and their
    relatives - a container cannot read a bind mount that is not labelled
    for it, and the tab comes up on a directory it says is empty. `:z`
    labels it shared, which is the right one for a project you also work in
    outside the container; `:Z` would take it for this container alone.
    Nothing on macOS, where the mount is a filesystem of its own.
    """
    try:
        with open("/sys/fs/selinux/enforce", encoding="utf-8") as fh:
            return ":z" if fh.read().strip() == "1" else ""
    except OSError:
        return ""


def container_command(command: str, project: str, agent: Agent,
                      fence: Fence = DOCKER,
                      image: str = DEFAULT_IMAGE,
                      extra: list[str] | None = None) -> str:
    """`command`, to be run inside a container that can reach `project`.

    The same line for docker and for podman, down to the order of the words,
    because the two take the same arguments for all of this and a line that
    forked by engine would be a line that has to be read back by engine.

    The container's arguments, and why each one:

    `--rm`, `-it`, `--init`: a tab is a terminal session, it goes when the
    pane goes, and something has to reap what the agent spawns.

    `-v project:project` and `-w project`: the same absolute path inside as
    out. A tab on `/Users/you/Code/thing` is the only part of your disk in
    there, and everything either side says about a path agrees.

    `-v home:/home/agent` with `-e HOME`: the login, the shell history and
    the agent's own transcripts, kept on the host so they survive the
    container and can be read from outside it - which is how the sidebar
    knows the agent has run out of turns.

    `--user`: as whoever you are, so that what the agent writes into the
    project is owned by you and not by root, and so that an agent which
    refuses to skip permission prompts as root will start.

    `--userns=keep-id:uid=,gid=`, for an engine whose table row asks for it:
    the same sentence again, said in the one place it is not already true.
    Rootless podman runs the container inside a user namespace it made
    before we got here, and in that namespace your own uid is already root
    and the numbers above it are subuids - so `--user 501:20` alone picks a
    subuid, the project fills up with files owned by 100500, and dropping
    `--user` instead hands the agent root. `keep-id` maps you to you. The
    explicit `uid=,gid=` form rather than the bare flag because the user
    podman is mapping is the one its engine runs as, which on macOS is the
    Linux account inside the machine VM and not the account whose numbers the
    image's `agent` was built with: spelling them out says what the container
    should see rather than hoping the two agree. Skipped when you are root,
    where there is no namespace to undo and podman calls it an error.

    Then the agent's own environment by name, so a variable the pane was
    given - which conversation this is, which key sticky has taken - reaches
    the agent through the container rather than stopping at it. By name on
    both engines: each takes the value from its client's own environment, and
    each drops the argument without complaint when there is no such variable.
    """
    home = agent_home(agent.name)
    here, label = os.path.abspath(project), relabel()
    args = [
        fence.cli, "run", "--rm", "-it", "--init",
        "--label", "sticky-chat=agent",
        "-v", f"{here}:{here}{label}",
        "-w", here,
        "-v", f"{home}:{CONTAINER_HOME}{label}",
        "-e", f"HOME={CONTAINER_HOME}",
        "--user", f"{os.getuid()}:{os.getgid()}",
    ]
    if fence.keep_id and os.getuid():
        args += ["--userns", f"keep-id:uid={os.getuid()},gid={os.getgid()}"]
    # By name, with no value: the engine takes these from the pane's own
    # environment, which is where tmux put them when the tab was opened.
    for name in ("TERM", "COLORTERM", "LANG", "LC_ALL",
                 *(name for name, _ in agent.env)):
        args += ["-e", name]
    # An API key or a token if you work that way. Not mounted, not copied:
    # named, so it is the host's environment that decides whether there is
    # one, and a machine without one logs in inside the container instead.
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(name):
            args += ["-e", name]
    args += list(extra or [])
    # Last of the container's arguments, always: `split_command` finds this
    # word and counts two - itself and the image - to reach the agent's own
    # command. Its value is the fence's name rather than the engine's, which
    # is the same word for both of these but is what `fenced_by` answers and
    # what `C-g b` has to be able to take back off.
    args += ["-e", f"{FENCE_ENV}={fence.name}", image]
    return " ".join([*(shell_quote(word) for word in args), command])


def runs_in_image(image: str, program: str,
                  extra: list[str] | None = None,
                  cli: str = DOCKER.cli) -> bool:
    """Whether the program a tab is about to run exists in the container.

    Asked before anything is killed. The image installs one agent; boxing a
    tab that runs a different one would respawn the pane on a command that
    is not there, and a pane whose command dies takes the window and the
    sidebar beside it. One container start, about a second, on a key you
    pressed on purpose.

    `extra` is the tab's own container arguments, applied here too, because
    a program that arrives through a mount of yours is a program that is
    there - and a probe that could not see it would refuse the one case you
    went out of your way to set up.

    Asked of the engine the tab will run under, because the image it is
    asking about is that engine's own: the tag docker has says nothing about
    what podman would start.
    """
    try:
        done = subprocess.run(
            [cli, "run", "--rm", *(extra or []), image, "sh", "-c",
             f"command -v {shell_quote(program)}"],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())

# ------------------------------------------------- wrapping it in namespaces


def real_home() -> str:
    """The home directory the namespace fence covers over.

    Our home goes on top of yours, at your home's own path, rather than at a
    `/home/agent` of our choosing the way the container's does. The reason is
    the passwd file: in a namespace it is the host's, read-only, and it says
    your home is where it has always been. Programs that ask the system where
    `~` is rather than reading $HOME - and there are enough of them, from
    shells writing a history file to libraries looking for a config - would
    be sent to a path that is not in the mount namespace at all. Covering the
    path keeps both answers the same and leaves the contents ours. The
    container can do it the other way round because it brings its own passwd
    file, and the Dockerfile writes /home/agent into it.

    A machine that will not say where home is, or says it is the root
    directory, has no home worth hiding: the agent gets ours at the same
    fixed path the container uses. `home_of` reads the target back off the
    line rather than assuming one, so either answer parses.
    """
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return home if home.startswith("/") and home.rstrip("/") \
        else CONTAINER_HOME


def bwrap_command(command: str, project: str, agent: Agent,
                  extra: list[str] | None = None) -> str:
    """`command`, to be run in a namespace that can reach `project`.

    Untested, and this is the docstring that says so. Every word of it comes
    from bubblewrap's documentation and its argument parser; everything about
    it that can be checked without a Linux kernel - which words, in which
    order, and the boundary they parse back to - is checked in the tests; and
    bwrap itself has never run, because the machine this was written on is a
    Mac. Read the reasoning below as reasoning, not as a report.

    The arguments, and why each one:

    `--unshare-all --share-net`: every namespace bubblewrap can make, and
    then the network handed straight back, because the agent's whole job is a
    conversation with an API. What the others buy is that the agent cannot
    see, signal or wait on your processes, cannot reach your System V IPC and
    does not share your cgroup or your hostname. `--unshare-all` asks for the
    user namespace with `-try`, so a kernel that refuses unprivileged ones
    still gets the rest through a bwrap that is setuid root. The pid
    namespace brings a reaper with it - bwrap runs as its pid 1 unless told
    not to - which is the job `--init` does for the container.

    `--die-with-parent`: the pane's process *is* the fence, so when tmux
    takes the pane away the whole namespace goes with it, down to a SIGKILL
    of everything inside. For the container this took a drain and a forwarded
    signal; here it is one flag, and it is the flag that stops a moved tab
    leaving anything behind.

    `--ro-bind /usr`, `/etc` and `MAY_BIND`: what has to exist before a
    program starts, read-only. This is the line where the fence differs from
    the container in the way that makes it worth having - the toolchain in
    here is the toolchain you already have, so a project that builds on this
    machine builds in the fence with no image to teach anything about python,
    cargo or a jdk. It is also where it differs in the way that costs: /usr
    and /etc are the host's, and a process in here can read every
    world-readable file in them, your accounts and your installed software
    included.

    `--proc`, `--dev`, `--tmpfs /tmp`: a /proc showing the namespace's own
    processes rather than yours, the handful of devices bubblewrap makes
    rather than the host's, and a /tmp of its own - which is also how the
    tmux server's socket stops being something the agent can find and talk
    to.

    `--bind home your-home` with `--setenv HOME`: the same directory the
    container fence uses, `~/.sticky/sandbox/<agent>`, so a login, a shell
    history and the agent's own transcripts survive the tab and can be read
    from outside it - which is how the sidebar knows the agent has run out of
    turns. Two things about the way it goes on.

    It is bound *before* the project, because a project under your home -
    which is where projects live - would otherwise be hidden the moment the
    home went on top of it. The order is the whole of the fix, and the cost
    of it is a footprint: bwrap makes the project's mount point inside the
    new root, and the new root's home is a real directory of ours, so
    `~/.sticky/sandbox/<agent>/Code/thing` gets made on the host as an empty
    place to mount on. Empty directories in a directory sticky owns is a
    price worth paying for a project that is visible.

    And the source lives inside the very directory it covers, which sounds
    circular and is not: bubblewrap pivots the host's root aside and resolves
    every source against *that*, not against the root it is building, so
    covering a path cannot hide what lives under it.

    `--setenv DISABLE_AUTOUPDATER`: the agent in here is the host's install,
    and the fence cannot write to it. An updater that tried would either fail
    against a read-only /usr or succeed into a home the next tab starts
    fresh - which is the kind of failure that looks like success. The
    Dockerfile says the same thing for the same reason.

    `--unsetenv SSH_AUTH_SOCK`: the environment is inherited whole here,
    which is how the API key, the terminal and the tab's own variables get in
    without being named one at a time the way docker needs. The agent socket
    that variable points at is in /run or /tmp and is not in the namespace,
    so left set it turns every `git push` into a hang or an error instead of
    a clean "no key here".

    `--bind project project` with `--chdir`: the project at its own absolute
    path, writable, and the tab starting in it. Same reasoning as the
    container, and it matters most where the agent writes down what it called
    the conversation, because the directory it writes that in is named after
    the project's own path.

    No `--new-session`, and this is the one place the safe-looking flag is
    the wrong one. It calls setsid, and a process that leaves the pane's
    session behind leaves the pty's foreground process group behind with it:
    Ctrl-C would stop reaching the agent, which is the one key an agent tab
    cannot do without. What `--new-session` exists to stop - TIOCSTI, a
    sandboxed process pushing characters into a terminal that something else
    is reading - needs a terminal shared with a process outside the fence,
    and a pane whose only process is the agent has none: what it could
    inject it can already print.

    Then `-- /usr/bin/env STICKY_SANDBOX=bwrap`, the end of bwrap's own
    arguments and the marker in one. `Fence.follows` is 0 for this fence
    because of it: the marker is the last word before the agent's command,
    with no image after it.

    What this does not fence, in one place, because a fence that oversells
    itself is worse than none. The network is shared and reaches everything
    yours does, so a hostile repository can still send out whatever it can
    read - the same caveat the container carries. /usr and /etc are the
    host's: read-only, but readable. The process runs as you, with your uid
    and your kernel, so the fence is a mount namespace and nothing besides -
    a file it can reach, it reaches as you, and a kernel bug is a kernel
    bug. And everything bound writable is writable: `rm -rf` in the project
    still takes the project, and `rm -rf ~` still takes the agent's own home.
    What is behind the fence is the rest of your disk - the other
    repositories, the ssh keys, the browser profile, the notes - and that is
    what it is for.
    """
    home, here = agent_home(agent.name), os.path.abspath(project)
    mine = real_home()
    args = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent"]
    for path in MUST_BIND:
        args += ["--ro-bind", path, path]
    for path in MAY_BIND:
        args += ["--ro-bind-try", path, path]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # The home first, then the project: see above, and do not swap them.
    args += ["--bind", home, mine, "--setenv", "HOME", mine,
             "--setenv", "DISABLE_AUTOUPDATER", "1",
             "--unsetenv", "SSH_AUTH_SOCK",
             "--bind", here, here, "--chdir", here]
    # Yours last of all, so an argument of yours can widen or overrule
    # anything decided above - the same order the container's `extra` has.
    args += list(extra or [])
    # `--` ends bwrap's arguments and the marker is the word after it, so
    # `split_command` counts the marker and nothing more: `Fence.follows`.
    args += ["--", ENV_PROGRAM, f"{FENCE_ENV}={BWRAP.name}"]
    return " ".join([*(shell_quote(word) for word in args), command])


def under(path: str, root: str) -> bool:
    """Whether `path` is `root` or something inside it."""
    root = root.rstrip("/")
    return not root or path == root or path.startswith(root + "/")


def unreachable(fence: Fence, command: str, project: str,
                extra: list[str] | None = None) -> str:
    """Why the program a tab runs would not be there inside `fence`.

    Empty when it would be, and empty for any fence that brings its own
    programs: a container's answer is `runs_in_image`, which has to start one
    to find out. The namespace fence runs the host's own binary, so the
    question is a question about paths and can be answered here - which is
    worth doing, because the answer is no more often than it looks. An agent
    installed by npm without a prefix, or by Claude's own installer, lives
    under your home; your home is the one thing this fence replaces; and a
    pane whose command is not there dies as it opens and takes its window and
    the sidebar beside it with it.

    The path as it is written, and no symlink following, because inside the
    fence the names are what resolve: /home/you is a directory of ours in
    there, so where a symlink under it pointed on the host is not a question
    the namespace ever asks. The one place following a link would have said
    something true - a launcher in /usr/local/bin whose real file is under
    your home - it would also have said something false twice over, of a
    /srv/bin/agent linked into /usr and of any project reached through a
    link. The names are what exec walks, so the names are what this reads.

    Which leaves two things it cannot see, and both are in the docstring
    rather than in the code because there is no honest way to check them
    from here. It cannot tell whether a program that is reachable can reach
    what *it* needs: a launcher in /usr/local/bin whose node_modules are
    under your home passes here and fails in the pane. And it has no opinion
    at all about a program this machine has never heard of - that is a tab
    that would not have started unfenced either, and not the fence's fault
    to report.

    An argument of yours counts as an answer, and counts first.
    `--sandbox-arg --ro-bind` is exactly how a toolchain outside these roots
    is let in, so every absolute path in `extra` widens what this accepts -
    the same courtesy `runs_in_image` extends by applying `extra` to its
    probe.
    """
    if fence is not BWRAP:
        return ""
    program = (shlex.split(inner_command(command)) or [""])[0]
    found = program if "/" in program else (shutil.which(program) or "")
    if not found:
        return ""
    path = os.path.abspath(found)
    # Yours and the project's first, and before the home is ruled out,
    # because a project under your home - `~/Code/thing`, which is where
    # projects are - is bound back over the top of it and a `./bin/agent`
    # of its own is as much in there as /usr is.
    yours = [word for word in (extra or []) if word.startswith("/")]
    if any(under(path, root)
           for root in (*yours, os.path.abspath(project))):
        return ""
    if under(path, real_home()):
        return (f"{found} is under the home {fence.name} replaces with one "
                f"of its own, so it would not be there: install it where "
                f"/usr can see it, or bind it in with "
                f"{let_in(os.path.dirname(path))}")
    if any(under(path, root) for root in (*MUST_BIND, *MAY_BIND)):
        return ""
    return (f"{found} is in none of the directories {fence.name} binds, so "
            f"it would not be there: bind it in with "
            f"{let_in(os.path.dirname(path))}")


def let_in(where: str) -> str:
    """How to say "and this directory too" on a command line.

    Spelled `--sandbox-arg=` rather than as two words because the value
    starts with a dash, and argparse takes a dashed value only when it is
    joined on - which is a thing to get right in the message rather than in
    the reply to the bug report.
    """
    where = where or "/"
    return (f"--sandbox-arg=--ro-bind-try --sandbox-arg={where} "
            f"--sandbox-arg={where}")


def carry_history(agent: Agent, project: str, session: str,
                  old_home: str, new_home: str) -> tuple[bool, str]:
    """Put a conversation where the agent will look for it after a move.

    Boxing a running tab restarts the agent, and an agent restarted in a
    container looks for its conversation in the container's home - where
    there has never been one. Without this, `--resume` comes back as a new
    conversation wearing the old notes, which is the failure that looks most
    like success.

    It is a copy, not a move: the one left behind is what a tab unboxed
    again would have resumed from anyway, and a copy cannot lose the
    conversation if the container will not start. Newer wins, so carrying it
    back after an hour in a box brings the hour with it.

    Comes back as (whether the tab can be moved, why not). A profile that
    writes nothing down has nothing to carry and moves freely; one whose
    record cannot be named from an id - gemini's glob, codex's database -
    says so rather than moving and quietly losing it.
    """
    named = "" if session.startswith("tab-") else session
    source = conversation_file(agent, project, named, old_home)
    target = conversation_file(agent, project, named, new_home)
    if not source or not target:
        if agent.notice_path or agent.transcript_glob or agent.state_db_glob:
            # It keeps a record, and we cannot point at this tab's.
            return False, (f"this tab's {agent.short} conversation has no id "
                           f"written down, so it cannot be carried")
        return True, ""                 # keeps none: nothing to carry
    try:
        if not os.path.exists(source):
            return True, ""             # a tab that has not said anything yet
        if (os.path.exists(target)
                and os.stat(target).st_mtime >= os.stat(source).st_mtime):
            return True, ""             # what is there already is newer
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        return False, f"the conversation could not be carried over: {exc}"
    return True, ""


def refence(command: str, fence: Fence, project: str, agent: Agent,
            image: str = DEFAULT_IMAGE,
            extra: list[str] | None = None) -> str:
    """`command`, fenced the way `fence` says - whatever it was before.

    Taking the old fence off first is what makes "another tab like this one,
    but on the host" work: a tab opened beside a container inherits the
    container's whole command line, and changing the answer at the prompt
    has to leave the agent's own arguments alone while replacing everything
    in front of them.
    """
    inside = inner_command(command)
    if fence is OFF:
        return inside
    if fence is BWRAP:
        return bwrap_command(inside, project, agent, extra)
    return container_command(inside, project, agent, fence, image,
                             extra)


def make_home(command: str) -> None:
    """Make the directory a fenced command line will mount as the agent's home.

    Before the tab and not while the line is being built, because a line is
    built in places that are only looking at it - and because a tab put back
    by `resume` replays a line nobody built today. Either engine would make
    the directory itself if it were missing, and on Linux docker would make it
    owned by root and rootless podman by a subuid, both of them homes the
    agent cannot write in. Bubblewrap
    """
    home = home_of(command)
    if home:
        try:
            os.makedirs(home, exist_ok=True)
        except OSError:
            pass                        # the engine will say it better


def split_command(command: str) -> tuple[str, str]:
    """A command line as (what fences it, what it runs). Empty first half
    when nothing does.

    Everything that edits a command line goes through here first. Without it
    a conversation id put "at the front, where this agent takes its
    subcommand" lands on `docker run`, and a line asked which conversation it
    names answers with whatever `-e` happened to look like a flag.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return "", command
    for index, word in enumerate(words):
        if word.startswith(f"{FENCE_ENV}="):
            # The marker, and then however many words of its own the fence
            # puts after it: one for the container, the image, and none for
            # the namespace, which has already said `--`. The fence names
            # itself in the marker, so the line says where its own boundary
            # is rather than every caller having to know.
            #
            # A marker naming a fence this version has never heard of is
            # read the way the only fence that ever carried one was - one
            # word, an image - because a line split in the wrong place is a
            # line a conversation id lands in the middle of, and that fails
            # as a tab that will not open rather than as a wrong answer.
            named = fence_named(word.split("=", 1)[1])
            ends = index + 1 + (named.follows if named else DOCKER.follows)
            inner = words[ends:]
            if not inner:
                return "", command
            return (" ".join(shell_quote(w) for w in words[:ends]),
                    " ".join(shell_quote(w) for w in inner))
    return "", command


def inner_command(command: str) -> str:
    """What a command line runs, with any fence taken off the front."""
    return split_command(command)[1]


def rejoin(fence: str, command: str) -> str:
    return f"{fence} {command}" if fence else command


def fenced_by(command: str) -> str:
    """Which fence a command line carries, or empty. The name, as chosen."""
    fence, _ = split_command(command)
    if not fence:
        return OFF.name
    try:
        words = shlex.split(fence)
    except ValueError:
        return ""
    for word in words:
        if word.startswith(f"{FENCE_ENV}="):
            return word.split("=", 1)[1]
    return ""


def home_of(command: str) -> str:
    """The host directory a sandboxed command line uses as the agent's home.

    Empty for a tab that runs on the host, where the agent's home is yours
    and every caller already knows how to find it. This is what lets the
    sidebar read a containerised agent's transcript without knowing that
    there is a container: it is a path on this disk either way.
    """
    fence, _ = split_command(command)
    if not fence:
        return ""
    try:
        words = shlex.split(fence)
    except ValueError:
        return ""
    if f"{FENCE_ENV}={BWRAP.name}" in words:
        # The namespace fence has no `-v` and no fixed target to look for:
        # its home goes on top of yours, wherever that is. So both halves
        # are read off the line - what HOME was set to, and which directory
        # of ours was bound there - which is what keeps a line readable on a
        # machine whose $HOME has changed since it was written, and what
        # lets `real_home` fall back to /home/agent without anything here
        # needing to know that it did.
        return bound_at(words, setting(words, "HOME"))
    for index, word in enumerate(words):
        if word != "-v" or index + 1 >= len(words):
            continue
        # `src:dst` or `src:dst:options`, and the options are what `:z` on a
        # machine with SELinux adds - so the last word is only the target
        # when it is not one of them.
        parts = words[index + 1].split(":")
        if len(parts) > 2 and parts[-1] in ("z", "Z", "ro", "rw"):
            parts.pop()
        if len(parts) >= 2 and parts[-1] == CONTAINER_HOME:
            return ":".join(parts[:-1])
    return ""


def setting(words: list[str], name: str) -> str:
    """What `--setenv name` is set to on a bubblewrap command line."""
    for index, word in enumerate(words[:-2]):
        if word == "--setenv" and words[index + 1] == name:
            return words[index + 2]
    return ""


def bound_at(words: list[str], target: str) -> str:
    """The host directory a bubblewrap command line binds onto `target`.

    The first one, because ours is written before anything of yours and an
    argument of yours that binds over it has not changed where the agent's
    transcript is on this disk - which is the only thing the caller wants to
    know.
    """
    if not target:
        return ""
    for index, word in enumerate(words[:-2]):
        if word == "--bind" and words[index + 2] == target:
            return words[index + 1]
    return ""
