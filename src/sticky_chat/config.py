"""The tmux config sticky generates, and the server it runs."""

from __future__ import annotations

import os

from .clipboard import clipboard_command
from .store import STATE_HOME, private_dir
from .tmux import Tmux
from .util import self_path, shell_quote

DEFAULT_SIDEBAR_WIDTH = 34


# Rows a tall window gets whatever the terminal has, when one is asked for.
# It is never asked for on your behalf: a window bigger than its client is
# still drawn wrong often enough - popups painted over by the pane beneath
# them, panes that jump under a repaint - that it has to be opted into, tab
# by tab, by someone who wants what it gives.
DEFAULT_VIRTUAL_ROWS = 200




# ------------------------------------------------------------------ startup


# --------------------------------------------------------- the status rows

# Agent tabs on one row and everything else on another, because they are two
# kinds of thing and reading one list for both means reading it twice. What
# tells them apart is the mark: sticky gives every agent tab one, `$` is the
# shell's, and a window sticky never opened has none at all - so "an agent"
# is "marked, and not the shell".
AGENT_TAB = "#{&&:#{@sticky_mark},#{!=:#{@sticky_mark},$}}"

# tmux's own window list, near enough. The ranges are what make a tab
# clickable and the styles are what make the current one, a bell and an
# alert look like they always did; all of it is copied from the stock
# `status-format[0]` rather than invented, because a list that behaved
# almost like tmux's would be worse than one that behaved like nothing.
_ALERT = ("#{?#{&&:#{window_last_flag},"
          "#{!=:#{E:window-status-last-style},default}},"
          " #{E:window-status-last-style},}"
          "#{?#{&&:#{window_bell_flag},"
          "#{!=:#{E:window-status-bell-style},default}},"
          " #{E:window-status-bell-style},"
          "#{?#{&&:#{||:#{window_activity_flag},#{window_silence_flag}},"
          "#{!=:#{E:window-status-activity-style},default}},"
          " #{E:window-status-activity-style},}}")

_OTHER_TAB = ("#[range=window|#{window_index} #{E:window-status-style}"
              + _ALERT + "]#[push-default]#{T:window-status-format}"
              "#[pop-default]#[norange default]")

_THIS_TAB = ("#[range=window|#{window_index} list=focus "
             "#{?#{!=:#{E:window-status-current-style},default},"
             "#{E:window-status-current-style},#{E:window-status-style}}"
             + _ALERT + "]#[push-default]#{T:window-status-current-format}"
             "#[pop-default]#[norange list=on default]")


def _tabs(wanted: bool) -> str:
    """The window list, with half the windows left out of it.

    The separator goes inside the test rather than after it. Left where tmux
    puts it, every skipped tab would still lay down the space between two
    tabs, and the row would read `1:beta  3:gamma` with a hole where the
    shell used to be - which is worse than either list on its own.
    """
    keep = AGENT_TAB if wanted else f"#{{?{AGENT_TAB},0,1}}"
    gap = "#{E:window-status-separator}"
    return (f"#{{W:#{{?{keep},{_OTHER_TAB}{gap},}},"
            f"#{{?{keep},{_THIS_TAB}{gap},}}}}")


def _row(left: str, tabs: str, right: str) -> str:
    return ("#[align=left range=left #{E:status-left-style}]#[push-default]"
            f"{left}"
            "#[pop-default]#[norange default]#[list=on "
            "align=#{status-justify}]#[list=left-marker]<"
            "#[list=right-marker]>#[list=on]"
            f"{tabs}"
            "#[nolist align=right range=right #{E:status-right-style}]"
            f"#[push-default]{right}#[pop-default]#[norange default]")


# The badge at the left of each row, and the reason it is a constant: the
# two rows have to be the same width or the two tab lists start at
# different columns, and reading them as one list off a ragged left edge is
# most of what having two rows was meant to fix.
STICKY_LABEL = " sticky "

OTHER_LABEL = " other ".ljust(len(STICKY_LABEL))

AGENT_ROW = _row("#{T;=/#{status-left-length}:status-left}", _tabs(True),
                 "#{T;=/#{status-right-length}:status-right}")

# The second row earns its own label rather than repeating "sticky", and
# nothing on the right: what is over there is about sending notes, which is
# not what a tab with no agent in it is for. `other` rather than `shell`,
# because what lands here is everything that is not an agent tab - the
# shells sticky opened, and any window somebody made in this session by
# hand, which sticky knows nothing about and should not mislabel.
OTHER_ROW = _row(f"#[bold]{OTHER_LABEL}#[default] ", _tabs(False), "")

# Two rows only while there is a second list to put on one. A row that says
# "shell" and holds nothing is a line of the terminal spent on saying that
# there are no shells, which is not news.
HAS_OTHERS = f"#{{W:#{{?{AGENT_TAB},,x}},#{{?{AGENT_TAB},,x}}}}"

# Asked again whenever the answer could have changed. It is a format and
# nothing else - no process is started to decide how tall the status line
# is, which matters because one of the hooks it hangs off fires every time
# you change tab.
# `on` and not `1`: the option is a choice, and one row is spelt `on`.
# `set -g status 1` is refused as an unknown value, which a hook has no way
# of telling you - it simply leaves the row count wherever it was.
ROWS_HOOK = f'if -F "{HAS_OTHERS}" "set -g status 2" "set -g status on"'


CONFIG_TEMPLATE = """# generated by sticky - do not edit, edit tmux.conf in the repo
set -g prefix C-g
unbind C-b
bind C-g send-prefix

set -g default-terminal "tmux-256color"
set -as terminal-overrides ",*:RGB"
# From 3.8 tmux draws its own chrome - the selection, popups, borders, the
# message line - in theme colours that resolve to 24-bit values, which the
# line above promises whether or not the terminal can draw them. ANSI is the
# palette every terminal has, and it is the user's own besides. Older tmux
# has no such option, so the set is quiet.
set -sq theme terminal
set -s escape-time 0
set -g history-limit 200000
set -g mouse on
set -s extended-keys on
set -g focus-events on
set -g base-index 1
setw -g mode-keys vi
# The status bar borrows the terminal's own background and foreground rather
# than tmux's green, so it stays readable in any colour scheme; keys are told
# apart by weight, not by colour.
set -g status-style "bg=default,fg=default"
set -g window-status-current-style "bold"
# Which agent is in each tab, one character after its name. The mark is a
# window option rather than a pane one: a format in the status line is
# resolved against whichever pane is active, and half of a sticky window is
# the sidebar - a pane-level mark would come and go as focus moved. A tab
# without one, which is any window sticky did not open, looks as it always
# did.
# Hard against the name, with no space: a mark floating between two tabs
# belongs to neither of them to look at.
# The mark is highlighted while a tab is waiting to be read: the agent
# printed and then went quiet, which is the closest thing to "done" that
# works for every agent, including one that rings no bell. Only the mark,
# not the whole tab - tmux's own bell flag already reverses everything, and
# two kinds of shouting in one status line is one too many.
# A tab waiting for a limit to reset shows a clock instead of its agent's
# mark: while that is what it is doing, it is the more useful of the two
# things one character can say.
set -g window-status-format "#I:#W#{?@sticky_waiting,⧗,#{?@sticky_mark,#{?@sticky_done,#[reverse]#{@sticky_mark}#[noreverse],#{@sticky_mark}},}}#F"
set -g window-status-current-format "#I:#W#{?@sticky_waiting,⧗,#{?@sticky_mark,#{?@sticky_done,#[reverse]#{@sticky_mark}#[noreverse],#{@sticky_mark}},}}#F"

set -g window-status-style "dim"
# Two lists, on two rows: agent tabs above, everything else below. The
# second row is there only while something is on it - see `rows_needed`.
set -g status-format[0] "@AGENTROW@"
set -g status-format[1] "@OTHERROW@"
set -g status-left "#[bold]@LABEL@#[default] "
set -g status-left-length 20
set -g status-right-length 200
set -g status-right "#{?@sticky_new,#[reverse] \u2193 #{@sticky_new}#{?#{e|>:#{e|-:#{client_width},#{e|*:#{session_windows},12}},95}, new output rows below, below} #[default]  ,}#[bold]C-g s#[default] send  #[bold]C-g c#[default] new tab  #[bold]C-g n#[default] next  #[bold]C-g w#[default] tabs  #[bold]C-g d#[default] detach  #{?window_bigger,[+#{window_offset_y} rows] ,}"

# selection -> note. no-clear keeps copy mode and the highlight, so a note
# taken while scrolled back leaves you where you were reading;
# stop-selection finishes the selection off so the mouse stops dragging it
# about; `sticky add` drops out of copy mode when you were at the bottom.
bind -T copy-mode-vi N { send -X copy-pipe-no-clear "@BIN@ add --socket '#{socket_path}' --client '#{client_name}' --pane '#{pane_id}' --sy '#{selection_start_y}' --sx '#{selection_start_x}' --ey '#{selection_end_y}' --ex '#{selection_end_x}' --hsize '#{history_size}' --scroll '#{scroll_position}'" ; send -X stop-selection }
bind -T copy-mode    N { send -X copy-pipe-no-clear "@BIN@ add --socket '#{socket_path}' --client '#{client_name}' --pane '#{pane_id}' --sy '#{selection_start_y}' --sx '#{selection_start_x}' --ey '#{selection_end_y}' --ex '#{selection_end_x}' --hsize '#{history_size}' --scroll '#{scroll_position}'" ; send -X stop-selection }

# y puts the live selection on the system clipboard and keeps it up. The
# the prompt's C-c only covers the selection that opened it; this is for
# afterwards, for the sidebar, and for selections that never became notes.
# `v` begins a selection, which is what the docs and the sidebar's own help
# have always said and what a vi user expects. tmux's own copy-mode-vi gives
# `v` to rectangle-toggle and puts begin-selection on Space - and Space is a
# key you type with, which the table below hands back to Claude. Binding it
# here means the promise is kept and the space bar stays a space bar.
bind -T copy-mode-vi v send -X begin-selection
bind -T copy-mode    v send -X begin-selection
bind -T copy-mode-vi y send -X copy-pipe-no-clear "@CLIP@"
bind -T copy-mode    y send -X copy-pipe-no-clear "@CLIP@"

# Dragging in the sidebar has no other purpose than copying, so it goes
# straight to the system clipboard; over Claude a drag might become a note,
# which is why that side does not touch the clipboard on its own.
# Mouse drag release in the Claude pane opens the note prompt. --auto is what
# makes the gesture forgiving: a drag that reaches into the prompt box is
# taken as a plain copy, since that is your own half-written message. N above
# has no --auto, so the keyboard always makes a note.
bind -T copy-mode-vi MouseDragEnd1Pane if -F '#{==:#{@sticky_role},claude}' { send -X copy-pipe-no-clear "@BIN@ add --socket '#{socket_path}' --client '#{client_name}' --pane '#{pane_id}' --sy '#{selection_start_y}' --sx '#{selection_start_x}' --ey '#{selection_end_y}' --ex '#{selection_end_x}' --hsize '#{history_size}' --scroll '#{scroll_position}' --auto" ; send -X stop-selection } { send -X copy-pipe-no-clear "@CLIP@" ; send -X stop-selection }
bind -T copy-mode    MouseDragEnd1Pane if -F '#{==:#{@sticky_role},claude}' { send -X copy-pipe-no-clear "@BIN@ add --socket '#{socket_path}' --client '#{client_name}' --pane '#{pane_id}' --sy '#{selection_start_y}' --sx '#{selection_start_x}' --ey '#{selection_end_y}' --ex '#{selection_end_x}' --hsize '#{history_size}' --scroll '#{scroll_position}' --auto" ; send -X stop-selection } { send -X copy-pipe-no-clear "@CLIP@" ; send -X stop-selection }
bind s run-shell -b '@BIN@ commit --quiet --socket "#{socket_path}" --pane "#{pane_id}"'
bind S run-shell -b '@BIN@ commit --send --quiet --socket "#{socket_path}" --pane "#{pane_id}"'
# The project is asked for in a prompt of sticky's own rather than tmux's
# command-prompt, which cannot complete a path: there Tab completes directory
# names, and a path that does not exist is said so with what you typed still
# on the line. It is opened from Python rather than named here, because
# run-shell expands the #{} in what it runs and the alternatives do not.
bind t run-shell -b '@BIN@ new --ask --socket "#{socket_path}" --client "#{client_name}" --from "#{pane_id}"'
bind c run-shell -b '@BIN@ new --ask --socket "#{socket_path}" --client "#{client_name}" --from "#{pane_id}"'
# The same tab, but with a window taller than the terminal, for when a long
# table keeps eating the lines above it. Off by default because a panned
# window is drawn wrong in other ways; this is how you take that trade.
bind C run-shell -b '@BIN@ new --ask --virtual-rows @ROWS@ --socket "#{socket_path}" --client "#{client_name}" --from "#{pane_id}"'
# A tab with no agent in it: a shell, for reading a log, a test run or a
# diff. `c` inherits the agent of the tab you are in, which is right nearly
# always and never when what you want is a prompt of your own.
# o for open: the same prompt again, listing the tabs sticky remembers
# instead of asking for a project - the way back into a conversation you
# closed, without leaving the session to run `resume` in a shell. A tab that
# is already on screen is not offered, since opening it again would be a
# second tab on a conversation that is still running.
bind o run-shell -b '@BIN@ reopen --ask --socket "#{socket_path}" --client "#{client_name}" --from "#{pane_id}"'
# `pane-activity`, which is how the sidebar hears that Claude has printed, is
# set by `install_hooks` rather than written here: it exists only in tmux
# after 3.7c, and an unknown hook name makes the whole file fail to source.

# `pane-exited`, which is how a tab notices the agent has gone, is set by
# `install_hooks` and not written here: a server somebody else started never
# sources this file, and a sidebar left alone in a window is exactly the case
# that must not depend on it.
# Clicking off a note prompt is an ordinary change of active pane, which is
# the whole reason the prompt is a floating pane and not a popup: a popup is
# handed the click and throws it away, and nothing downstream ever hears.
# Guarded on the pane count because this fires on every click that moves
# focus, and starting sticky to find nothing costs 37ms each time: a sticky
# window is two panes, and three while a prompt is open.
set-hook -g window-pane-changed 'if -F "#{!=:#{window_panes},2}" { run-shell -b "@BIN@ sweep --quiet --socket \\"#{socket_path}\\"" }'
set-hook -g client-resized 'run-shell -b "@BIN@ fit --socket \\"#{socket_path}\\""'
set-hook -g client-attached 'run-shell -b "@BIN@ fit --socket \\"#{socket_path}\\""'
# `after-select-window` and `pane-focus-in` are set by `install_hooks`, not
# here: arriving at a tab is what clears its "done" mark, and a server that
# was already running when sticky arrived never sources this file.

# `sticky` at the left of the status line is a way into the sidebar for a
# hand already on the mouse: clicking it crosses to the other pane, and
# clicking it again crosses back. `{next}` wraps in a two-pane window,
# which is what makes it a toggle rather than a one-way trip - and a target
# cannot be a format, so the partner cannot simply be named.
bind -T root MouseDown1StatusLeft select-pane -t "{next}"

# the wheel over the sidebar pages through the note history; everywhere else
# this is tmux's own default binding
# single click shows where a note came from, double click edits it, and the
# [x] button at the right edge strikes it out
bind -T root MouseUp1Pane if -F '#{==:#{@sticky_role},sidebar}' 'run-shell -b "@BIN@ click --socket \\"#{socket_path}\\" --client \\"#{client_name}\\" --pane \\"#{pane_id}\\" --y \\"#{mouse_y}\\" --x \\"#{mouse_x}\\""'
bind -T root DoubleClick1Pane if -F '#{==:#{@sticky_role},sidebar}' 'run-shell -b "@BIN@ click --edit --socket \\"#{socket_path}\\" --client \\"#{client_name}\\" --pane \\"#{pane_id}\\" --y \\"#{mouse_y}\\" --x \\"#{mouse_x}\\""'

bind -T root WheelUpPane if -F '#{==:#{@sticky_role},sidebar}' 'send-keys -t "#{pane_id}" C-y' 'if -F "#{||:#{alternate_on},#{pane_in_mode},#{mouse_any_flag}}" "send-keys -M" "copy-mode -e"'
bind -T root WheelDownPane if -F '#{==:#{@sticky_role},sidebar}' 'send-keys -t "#{pane_id}" C-e' 'send-keys -M'

# entering or leaving copy mode is exactly when the view stops moving: wake the
# sidebar so it re-places its notes against the settled screen, and let fit
# decide whether the viewport is still pinned to the bottom of a tall window
set-hook -g pane-mode-changed { if -F "#{@sticky_sidebar_pid}" { run-shell -b "kill -USR1 #{@sticky_sidebar_pid} 2>/dev/null || true" } ; run-shell -b '@BIN@ fit --socket "#{socket_path}"' }

bind u run-shell -b '@BIN@ uncommit --quiet --socket "#{socket_path}" --pane "#{pane_id}"'
bind U run-shell -b '@BIN@ uncommit --quiet --socket "#{socket_path}" --pane "#{pane_id}"'
bind F run-shell -b '@BIN@ fork --quiet --socket "#{socket_path}" --pane "#{pane_id}" --client "#{client_name}"'
bind r run-shell -b '@BIN@ reload --quiet --socket "#{socket_path}"'
bind Q run-shell -b '@BIN@ quit --socket "#{socket_path}"'
bind R run-shell -b '@BIN@ reload --quiet --socket "#{socket_path}"'
bind L run-shell -b '@BIN@ list --socket "#{socket_path}" --pane "#{pane_id}"'

# Reading puts the pane in copy mode, where an ordinary key is a motion and
# what you type next goes nowhere until you know to press q. These say that
# typing means typing: leave the mode and let the key through. Everything
# that is not a printable character is left alone, and so are sticky's own
# four - N takes a note, y copies, q leaves, v starts a selection.
@TYPING@

# Your own overrides, if you want them; this file is never regenerated.
source-file -q "@HOME@/user.conf"
"""


ACTIVITY_HOOK = (
    'if -F "#{&&:#{==:#{@sticky_role},claude},#{==:#{window_panes},2}}"'
    ' { send-keys -t + -H 00 }'
)


EXIT_HOOK = ('run-shell -b "{binary} sweep --quiet '
             '--socket \\"#{{socket_path}}\\""')

# Arriving at a tab answers "has it finished", so the mark goes. Both
# actions in one value rather than appended: `install_hooks` runs on every
# start, and appending would grow the list a copy at a time.
ARRIVE_HOOK = ('run-shell -b "{binary} fit --socket \\"#{{socket_path}}\\"" ; '
               'set-option -w -u @sticky_done')
FOCUS_HOOK = "set-option -w -u @sticky_done"


def count_rows(tm) -> None:
    """Ask again whether the status line needs its second row.

    The hooks cover a window opening and closing, but not the moment that
    actually decides it: a tab is a window first and an agent tab a beat
    later, when its mark is set, and until then it counts as one of the
    others. So this is called once more when a tab is finished, which is
    the only time the answer is known.
    """
    tm.ok("set-option", "-g", "status",
          "2" if tm.fmt("", HAS_OTHERS).strip() else "on")


def install_hooks(tm) -> bool:
    """Set the hooks the config file cannot carry, and say whether it took.

    New output in Claude's pane is what tells the sidebar to look again, and
    the whole notification is one byte: `send-keys -t +` is the other pane of
    a two-pane window - a target cannot be a format, so `#{@sticky_partner}`
    would arrive as those very characters - and no process is started for it.

    `pane-activity` only exists in tmux after 3.7c, though, and an unknown
    hook name does not fail quietly: `source-file` gives up on the whole file,
    which would take `sticky-chat reload` with it. So it is set from here,
    where a refusal is just a False, and recorded in `@sticky_wake` so the
    sidebar knows whether it can wait to be told or has to keep looking.
    """
    # A tab is the agent and its sidebar together, so when the agent ends the
    # tab ends. The pane that exited is gone by the time this runs and cannot
    # be asked anything - `#{pane_id}` in a hook is whatever happens to be
    # active, not what died - so sticky asks the sidebars instead which of
    # them lost its partner. Set from here rather than from the config file
    # because a server that was already running when sticky arrived never
    # read one, and a sidebar with nothing left to annotate would then sit
    # there until it next happened to look, which on tmux 3.8 is half a
    # minute away.
    tm.ok("set-hook", "-g", "pane-exited",
          EXIT_HOOK.format(binary=self_path()))
    tm.ok("set-hook", "-g", "after-select-window",
          ARRIVE_HOOK.format(binary=self_path()))
    tm.ok("set-hook", "-g", "pane-focus-in", FOCUS_HOOK)
    # Whether there is a second row to draw. Hung off every hook that can
    # change the answer: a window opening or closing, and arriving at a
    # session that a previous client left set the other way.
    for when in ("after-new-window", "window-unlinked", "window-linked",
                 "client-attached", "after-select-window"):
        tm.ok("set-hook", "-ga", when, ROWS_HOOK)
    tm.ok("set-option", "-g", "status",
          "2" if tm.fmt("", HAS_OTHERS).strip() else "on")
    woken = tm.ok("set-hook", "-g", "pane-activity", ACTIVITY_HOOK)
    tm.ok("set-option", "-g", "@sticky_wake", "1" if woken else "0")
    count_rows(tm)
    return woken


FOCUS_STYLE = "bg=colour236"    # the ground under the sidebar you are in


RESERVED_IN_COPY_MODE = ("N", "y", "q", "v")


def typing_keys() -> str:
    """Bindings that let any ordinary key end a scroll and reach Claude.

    Scrolling back puts the pane in copy mode, where every letter is a
    motion, so the first thing you type after reading goes nowhere - you
    have to know to press q first. Here a printable key leaves copy mode and
    arrives at Claude, which is what pressing it meant.

    `send-keys` with no argument resends whatever was pressed, so one shape
    does for every key. Both tables get it because `mode-keys` follows your
    EDITOR, and the four that stay are sticky's own: N makes a note, y
    copies, q leaves, v starts a selection. Everything that is not a
    printable character - arrows, page keys, the wheel, and the control keys
    that search - is untouched.
    """
    lines = []
    for code in range(0x20, 0x7F):
        key = chr(code)
        if key in RESERVED_IN_COPY_MODE:
            continue
        # Single quotes, because tmux expands a leading ~ inside double ones
        # and would bind the home directory instead of the key. The one key
        # single quotes cannot hold is the apostrophe itself.
        quoted = f'"{key}"' if key == "'" else f"'{key}'"
        for table in ("copy-mode", "copy-mode-vi"):
            lines.append(f"bind -T {table} {quoted} "
                         "{ send -X cancel ; send-keys }")
    return "\n".join(lines)


def write_config(bin_path: str) -> str:
    private_dir(STATE_HOME)
    path = os.path.join(STATE_HOME, "tmux.conf")
    with open(path, "w") as fh:
        clip = clipboard_command() or ["true"]
        fh.write(CONFIG_TEMPLATE.replace("@BIN@", bin_path)
                 .replace("@HOME@", STATE_HOME)
                 .replace("@ROWS@", str(DEFAULT_VIRTUAL_ROWS))
                 .replace("@TYPING@", typing_keys())
                 .replace("@LABEL@", STICKY_LABEL)
                 .replace("@AGENTROW@", AGENT_ROW)
                 .replace("@OTHERROW@", OTHER_ROW)
                 .replace("@CLIP@", " ".join(shell_quote(c) for c in clip)))
    return path


def default_virtual_rows() -> int:
    """How tall to make the window when nobody asks: the terminal's height.

    A tall window is off unless it is asked for - per tab with
    `--virtual-rows N` or `C-g C`, or for every tab with
    STICKY_VIRTUAL_ROWS.
    """
    try:
        return max(0, int(os.environ["STICKY_VIRTUAL_ROWS"]))
    except (KeyError, ValueError):
        return 0


def client_areas(tm: Tmux) -> dict[str, tuple[int, int]]:
    """The rows and columns each window's own clients give it, by window id.

    A client sizes the window it is looking at and no other. Taking the
    largest of every attached client instead would let two terminals sitting
    on two different tabs pull each other's windows about.
    """
    try:
        listing = tm.run("list-clients", "-F",
                         "#{window_id}\t#{client_height}\t#{client_width}"
                         "\t#{status}")
    except RuntimeError:
        return {}
    out: dict[str, tuple[int, int]] = {}
    for line in listing.splitlines():
        window, height, width, bar = (line.split("\t") + [""] * 4)[:4]
        if not (window and height.isdigit() and width.isdigit()):
            continue
        rows, cols = out.get(window, (0, 0))
        out[window] = (max(rows, int(height) - status_height(bar)),
                       max(cols, int(width)))
    return out


def status_height(said: str) -> int:
    """How many rows of the terminal the status line is taking.

    Not a constant since the tab lists went to two rows. The option is a
    choice and says so in its own words: `off` is none, a number is that
    many, and `on` - or anything unexpected - is the one row it has always
    been. Getting this wrong is invisible until a tall window, where it
    shows up as the bottom line of the agent's output hidden under the
    status bar for ever.
    """
    said = said.strip()
    if said == "off":
        return 0
    return int(said) if said.isdigit() else 1


def client_area(tm: Tmux, target: str | None = None) -> int:
    """Rows the clients on a window have for the window itself.

    With a target - a window, or a pane in one - only the clients looking at
    that window count. With none, the tallest client anywhere stands in.
    """
    areas = client_areas(tm)
    if target is None:
        return max((rows for rows, _ in areas.values()), default=0)
    try:
        window = tm.fmt(target, "#{window_id}")
    except RuntimeError:
        return 0
    return areas.get(window, (0, 0))[0]


def prime_rows(command: str, rows: int) -> str:
    """Scroll the pane before the command runs, so it starts at the bottom.

    A pane draws from its top row down, so in a window taller than the
    terminal the first screenful of Claude would sit above the viewport and
    you would be looking at blank rows. Filling the window with newlines
    first puts the prompt where it would be in an ordinary terminal.
    """
    return f"awk 'BEGIN{{while(i++<{rows})print\"\"}}'; exec {command}"


def virtual_window(tm: Tmux, window: str, rows: int):
    """Make the window taller than the terminal and remember by how much.

    tmux then pans a viewport over it, so Claude's repaint region has room
    above the last line instead of running off the top of the screen.
    """
    tm.ok("set-window-option", "-t", window, "window-size", "manual")
    tm.ok("set-window-option", "-t", window, "@sticky_virtual_rows", str(rows))
    tm.ok("resize-window", "-t", window, "-y",
          str(max(rows, client_area(tm, window))))



def fit_windows(tm: Tmux):
    """Hold each tall window at its height, or its own terminal's if more.

    A window shorter than the client is drawn in the corner with dead space
    around it, so the height asked for is a floor rather than a fixed size.
    The floor is the client looking at that window, not the tallest client
    anywhere; a window nobody is watching keeps the height it asked for until
    somebody is.
    """
    try:
        listing = tm.run("list-windows", "-a", "-F",
                         "#{window_id}\t#{@sticky_virtual_rows}\t"
                         "#{window_height}\t#{window_width}")
    except RuntimeError:
        return
    areas = None
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[1].isdigit():
            continue
        if areas is None:
            areas = client_areas(tm)
        rows, cols = areas.get(parts[0], (0, 0))
        want = max(int(parts[1]), rows)
        if parts[2].isdigit() and int(parts[2]) != want:
            tm.ok("resize-window", "-t", parts[0], "-y", str(want))
        # Manual sizing freezes the width as well, and a window narrower
        # than the terminal is drawn in the corner with dead space beside it.
        if cols and parts[3].isdigit() and int(parts[3]) != cols:
            tm.ok("resize-window", "-t", parts[0], "-x", str(cols))


def pin_clients(tm: Tmux):
    """Hold the viewport at the bottom of a window that is taller than it.

    Left alone, tmux pans to keep the active pane's cursor in view and drops
    to the top of the window whenever that cursor is hidden - which is
    exactly what Claude does while it repaints, so the text jumps away under
    you. Copy mode is the one time the cursor is worth following, because
    that is you moving it, so the pin comes off while you read back.
    """
    try:
        listing = tm.run("list-clients", "-F",
                         "#{client_name}\t#{window_bigger}\t"
                         "#{pane_in_mode}\t#{@sticky_role}")
    except RuntimeError:
        return
    for line in listing.splitlines():
        name, bigger, in_mode, role = (line.split("\t") + [""] * 4)[:4]
        if bigger != "1":
            continue
        if in_mode == "1" and role == "claude":
            tm.ok("refresh-client", "-t", name, "-c")
        else:
            tm.ok("refresh-client", "-t", name, "-D", "1000")
