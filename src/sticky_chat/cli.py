"""The command line: what is ours, what belongs to the agent."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .agents import agent_choices, known_name
from .commands import (
    ask,
    cmd_add,
    cmd_attach,
    cmd_clear,
    cmd_click,
    cmd_commit,
    cmd_fit,
    cmd_fork,
    cmd_list,
    cmd_note,
    cmd_place,
    cmd_quit,
    cmd_reload,
    cmd_reopen,
    cmd_restore,
    cmd_resume,
    cmd_rm,
    cmd_start,
    cmd_sweep,
    cmd_uncommit,
)
from .config import DEFAULT_SIDEBAR_WIDTH, default_virtual_rows
from .sidebar import cmd_sidebar
from .store import known_windows
from .tmux import Tmux
from .util import die

# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sticky-chat", description=__doc__,
                                     allow_abbrev=False)
    parser.add_argument("--socket", dest="top_socket",
                        help="tmux socket name or path")
    parser.add_argument("--version", action="version",
                        version=f"sticky-chat {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--pane")
        p.add_argument("--project")
        p.add_argument("--socket")
        p.add_argument("--store", help="note directory, for a forked tab")
        return p

    for name, help_text in (("start", "open an agent + sidebar window"),
                            ("new", "add another window for another project")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("dir", nargs="?")
        p.add_argument("--sidebar-width", type=int,
                       default=DEFAULT_SIDEBAR_WIDTH)
        p.add_argument("--virtual-rows", type=int,
                       default=default_virtual_rows(),
                       help="give the agent a window this many rows tall, "
                            "taller than the terminal; off unless you ask")
        p.add_argument("--no-virtual-rows", dest="virtual_rows",
                       action="store_const", const=0,
                       default=default_virtual_rows(),
                       help="give the agent a window the size of the "
                            "terminal")
        p.add_argument("--agent", help="which agent profile to start on: "
                                       f"{agent_choices()}")
        p.add_argument("--agent-cmd", metavar="CMD",
                       help="the command to run, instead of the profile's")
        p.add_argument("--detach", action="store_true")
        p.add_argument("--local", action="store_true",
                       help="keep notes in <dir>/.sticky instead of ~/.sticky")
        p.add_argument("--socket")
        p.add_argument("--client", help="client to switch to the new window")
        p.add_argument("--store", help="note directory, for a forked tab")
        p.add_argument("--ask", action="store_true",
                       help="ask for the project in a prompt, Tab completes")
        p.add_argument("--ask-here", action="store_true",
                       help="ask on this terminal: what the prompt runs")
        p.add_argument("--menus", action="store_true",
                       help="let the agent open multiple-choice question "
                            "menus")
        p.add_argument("--sticky-dir",
                       help="project directory, when the positional one is "
                            "not usable")
        p.add_argument("--from", dest="source_pane",
                       help="pane whose agent arguments the new tab copies")
        p.set_defaults(func=cmd_start)

    p = common(sub.add_parser(
        "fork", help="branch the conversation into a new tab with the notes"))
    p.add_argument("--client")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_fork)

    p = common(sub.add_parser("click", help="handle a sidebar click (internal)"))
    p.add_argument("--y", type=int, default=-1)
    p.add_argument("--x", type=int, default=0)
    p.add_argument("--client")
    p.add_argument("--edit", action="store_true",
                   help="a double click: open the note for editing")
    p.add_argument("--id", help="act on this note rather than a screen row")
    p.set_defaults(func=cmd_click)

    p = sub.add_parser(
        "quit", help="close every window and stop the agents")
    p.add_argument("--yes", action="store_true", help="do not ask first")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--socket")
    p.set_defaults(func=cmd_quit)

    p = sub.add_parser("resume", help="reopen the tabs you had open before")
    p.add_argument("--all", action="store_true",
                   help="take every remembered tab without asking")
    p.add_argument("--last", action="store_true",
                   help="only the tabs that were open at the last quit")
    p.add_argument("--detach", action="store_true")
    p.add_argument("--client")
    p.add_argument("--socket")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "reopen", help="pick one remembered tab and open it again")
    p.add_argument("--ask", action="store_true",
                   help="pick in a floating pane: what the key binding does")
    p.add_argument("--detach", action="store_true")
    p.add_argument("--client")
    p.add_argument("--socket")
    p.add_argument("--from", dest="source_pane",
                   help="pane the picker opens beside")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser(
        "reload", help="re-apply the config and restart the sidebars")
    p.add_argument("--socket")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_reload)

    p = common(sub.add_parser("restore", help="reopen a closed sidebar"))
    p.add_argument("--sidebar-width", type=int, default=DEFAULT_SIDEBAR_WIDTH)
    p.add_argument("--quiet", action="store_true",
                   help="say nothing: key bindings show stdout in the pane")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser(
        "sweep", help="close sidebars whose agent has ended (internal)")
    p.add_argument("--socket")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("fit", help="restore the sidebar width (internal)")
    p.add_argument("--socket")
    p.set_defaults(func=cmd_fit)

    p = sub.add_parser("attach", help="re-attach to the sticky session")
    p.add_argument("--socket")
    p.set_defaults(func=cmd_attach)

    p = common(sub.add_parser("add", help="record a selection (stdin) as a note"))
    p.add_argument("--client")
    p.add_argument("--text")
    p.add_argument("--note")
    p.add_argument("--sy", type=int, default=0)
    p.add_argument("--sx", type=int, default=0)
    p.add_argument("--ey", type=int, default=0)
    p.add_argument("--ex", type=int, default=0)
    p.add_argument("--hsize", type=int,
                   help="pane history size when the selection was made")
    p.add_argument("--auto", action="store_true",
                   help="skip the note when the selection is your own input")
    p.add_argument("--scroll", type=int,
                   help="how far the pane was scrolled back at the time")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("note", help="prompt for a note's text")
    p.add_argument("id")
    p.add_argument("--edit", action="store_true",
                   help="start from the note's current text")
    p.add_argument("--store", help="note directory, for a forked tab")
    p.add_argument("--project")
    p.add_argument("--socket")
    p.add_argument("--pane", help="the agent pane the note belongs to")
    p.set_defaults(func=cmd_note)

    p = common(sub.add_parser("list", help="show notes"))
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_list)

    p = common(sub.add_parser("commit", help="render pending notes and paste"))
    p.add_argument("--no-paste", action="store_true")
    p.add_argument("--send", action="store_true",
                   help="press Enter too, instead of leaving it to you")
    p.add_argument("--quiet", action="store_true",
                   help="say nothing: key bindings show stdout in the pane")
    p.set_defaults(func=cmd_commit)

    p = common(sub.add_parser(
        "uncommit",
        help="mark sent notes pending again; nothing is taken back from "
             "the agent"))
    p.add_argument("id", nargs="*", help="note ids (default: the last send)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="say nothing: key bindings show stdout in the pane")
    p.set_defaults(func=cmd_uncommit)

    p = common(sub.add_parser("place", help="placement pass as JSON"))
    p.set_defaults(func=cmd_place)

    p = common(sub.add_parser("sidebar", help="the sidebar pane program"))
    p.set_defaults(func=cmd_sidebar)

    p = common(sub.add_parser("rm", help="delete a note"))
    p.add_argument("id")
    p.set_defaults(func=cmd_rm)

    p = common(sub.add_parser("clear", help="delete notes"))
    p.add_argument("--committed", action="store_true")
    p.set_defaults(func=cmd_clear)
    # Everything sticky owns; anything else on the command line is the agent's.
    parser.sticky_commands = frozenset(sub.choices)
    return parser


# Our own flags, and how many values each takes, so they can be lifted out of
# a command line that is otherwise passed straight to the agent.
OUR_FLAGS = {"--sidebar-width": 1, "--agent-cmd": 1,
             "--agent": 1, "--socket": 1,
             "--sticky-dir": 1, "--local": 0, "--detach": 0, "--menus": 0,
             "--virtual-rows": 1, "--no-virtual-rows": 0}


# Options that may appear before a subcommand.
GLOBAL_FLAGS = {"--socket": 1}


def split_our_flags(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate sticky's own flags from everything meant for the agent."""
    ours: list[str] = []
    theirs: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        name = token.split("=", 1)[0]
        arity = OUR_FLAGS.get(name)
        if arity is None:
            theirs.append(token)
            index += 1
            continue
        if "=" in token or arity == 0:
            ours.append(token)
            index += 1
            continue
        ours.extend(argv[index:index + 1 + arity])
        index += 1 + arity
    return ours, theirs


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv[1:]:
        cut = argv.index("--", 1)
        passthrough = argv[cut + 1:]
        argv = argv[:cut]
    rest = argv[1:]
    parser = build_parser()

    # A subcommand may follow global options, as in "--socket foo list".
    cursor = 0
    while cursor < len(rest):
        name = rest[cursor].split("=", 1)[0]
        if name not in GLOBAL_FLAGS:
            break
        cursor += 1 if "=" in rest[cursor] else 1 + GLOBAL_FLAGS[name]

    # Ours, not the agent's: without this, "sticky-chat --help" would be
    # read as "start the agent and pass it --help", and open a window to
    # say so.
    if any(token in ("-h", "--help") for token in rest[:cursor + 1]):
        parser.print_help()
        return 0
    if "--version" in rest[:cursor + 1]:
        print(f"sticky-chat {__version__}")
        return 0

    # `sticky codex .` - the agent first, which is how the line reads out
    # loud. The path is looked at before the name: a directory really called
    # `codex` is something the user can see in front of them, and an agent
    # name is a word we happen to know, so the directory wins and the line
    # goes on meaning what a bare `sticky-chat codex` has always meant.
    if (cursor < len(rest) and known_name(rest[cursor])
            and not os.path.exists(rest[cursor])):
        rest = [*rest[:cursor], "start", "--agent", *rest[cursor:]]

    if cursor < len(rest) and rest[cursor] in parser.sticky_commands:
        if rest[cursor] in ("start", "new"):
            args, unknown = parser.parse_known_args(rest)
            args.claude_args = unknown + passthrough
        else:
            args = parser.parse_args(rest)
            args.claude_args = passthrough
    elif cursor >= len(rest) and not passthrough:
        # Nothing but global options: join what is already running, or start
        # here if nothing is.
        args = parser.parse_args(["start", *rest])
        args.claude_args = []
        socket = args.socket or args.top_socket
        if Tmux(socket).server_running():
            return cmd_attach(argparse.Namespace(socket=socket))
        if known_windows() and sys.stdin.isatty():
            answer = ask("sticky: nothing running. Reopen your tabs?", "y/n")
            if answer == "y":
                return cmd_resume(argparse.Namespace(
                    socket=socket, all=False, detach=False, client=None))
    else:
        # No subcommand: this is an agent in the current directory, and
        # every argument that is not ours belongs to it.
        ours, theirs = split_our_flags(rest)
        args = parser.parse_args(["start", *ours])
        args.claude_args = theirs + passthrough

    if getattr(args, "socket", None) is None:
        args.socket = args.top_socket
    if getattr(args, "sticky_dir", None):
        args.dir = args.sticky_dir
    try:
        return args.func(args)
    except RuntimeError as exc:
        die(str(exc))
    return 0
