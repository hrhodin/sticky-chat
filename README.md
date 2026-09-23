# Sticky-Chat

[![agents](https://img.shields.io/badge/agents-claude%20%C2%B7%20codex%20%C2%B7%20gemini%20%C2%B7%20antigravity-blue.svg)](#another-agent)
[![PyPI](https://img.shields.io/pypi/v/sticky-chat.svg)](https://pypi.org/project/sticky-chat/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![tmux](https://img.shields.io/badge/tmux-3.7%2B-blue.svg)](https://github.com/tmux/tmux)
[![CI](https://github.com/hrhodin/sticky-chat/actions/workflows/ci.yml/badge.svg)](https://github.com/hrhodin/sticky-chat/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Keep your thoughts linked: Attach sticky notes to chat output, then hand the quoted
text plus your notes back to the agent in one paste.

<img src="https://raw.githubusercontent.com/hrhodin/sticky-chat/main/docs/demo-claude.gif" width="640"
     alt="Three notes taken on what Claude Code drew, handed back in one paste">

Select any part of the output with the cursor: a few characters, a line, or
a block of lines. Then type a short note. The note is logged in a sidebar pane,
aligned with the output row it belongs to.
You then decide when to send the batch of pending notes to the agent's prompt, continuing the discussion in a single thread, or to fork into a new tab window.

It is a few thousand lines of Python with no dependencies, driving the tmux
you already have. Sticky sends nothing to the outside and neither the agent nor tmux is patched or replaced: 
sticky-chat runs on its own local socket (`tmux -L sticky`) with a config it
generates into `~/.sticky/tmux.conf`, so your sessions and key bindings
keep working alongside it.

Start it with `sticky-chat` (or shorthands `sticky claude`)  and possible arguments you would otherwise give to the agent.

## Requirements

- **tmux 3.7 or newer** (`tmux -V`). 3.7 is where floating panes arrived,
  which is what the note prompt is, and it also brings redraws inside a synchronized
  update, so the sidebar never tears. Until `pane-activity` reaches a tmux
  release, the sidebar checks the pane twice a second rather than being woken
  when the agent prints — cheap, but not free.
- **Python 3.9 or newer**. The program itself uses only the standard
  library, so it pulls in nothing else.
- **An agent.** Claude Code on your `PATH` as `claude` is the default;
  Gemini CLI and Codex CLI have profiles of their own, and `--agent-cmd`
  runs anything else that prints a transcript.
- macOS or Linux; Windows only inside WSL. No admin rights are needed.

If tmux is missing: `brew install tmux` on macOS, `apt install tmux` or
`dnf install tmux` on Linux. Copying to the system clipboard uses `pbcopy`,
`wl-copy`, `xclip` or `xsel`, whichever is present; without one the copy
keys fill tmux's own paste buffer instead.

## Install

```sh
uv tool install sticky-chat      # or: pipx install sticky-chat
pip install sticky-chat          # works too; see below
```

That puts `sticky-chat` on your `PATH`, and `sticky` as a shorter name
for the same command.

There are no dependencies to resolve - the program is standard library
only - so `pip` is as good as the others at fetching it. What the first two
add is a virtualenv of their own: a command-line tool installed with `pip`
lands in whichever environment happens to be active, which is a nuisance
when that environment changes or goes away, and on a system Python it is
refused outright.

If you would rather run the source you can inspect and edit:

```sh
git clone https://github.com/hrhodin/sticky-chat ~/Code/sticky-chat
mkdir -p ~/.local/bin
ln -s ~/Code/sticky-chat/bin/sticky-chat ~/.local/bin/sticky-chat
ln -s ~/Code/sticky-chat/bin/sticky      ~/.local/bin/sticky
```

`bin/sticky-chat` runs the checkout directly, so `git pull` is all an
update takes. If `~/.local/bin` is not on your `PATH` yet:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
```

Check it with `sticky-chat --version`. To uninstall, remove the command
and `~/.sticky`, which holds the generated config and your notes.

## Quick start

```sh
sticky-chat start ~/my-project
```

That opens one tmux window: the chat pane on the left, the notes sidebar
on the right, 34 columns wide. Drag over something the agent printed, type
a note, press `C-g s` to send every pending note back to the agent.

```sh
sticky-chat start ~/my-project -- --dangerously-skip-permissions
sticky-chat new ~/other-project    # a second tab, a second project
sticky-chat                        # re-attach later
```

Arguments after `--` reach the agent unchanged, so `--continue` and
`--resume` work through `sticky-chat start`. Notes belong to the conversation, not to
the directory: a second tab on the same project opens an empty sidebar, and
resuming a tab brings its own notes back. `C-g z` zooms the chat pane to
full width; the sidebar keeps running behind it.

`C-g c` asks for the project in a prompt, starting from the directory the
pane is in. **Tab** completes directory names - once if there is only one,
listing the choices if there are several - and a path that does not exist
is said so with what you typed still on the line to correct. Esc cancels.

### Another agent

```sh
sticky claude ~/my-project         # the same as sticky-chat start, shorter
sticky gemini ~/my-project         # Gemini CLI instead
sticky codex  ~/my-project         # Codex CLI instead
sticky agy    ~/my-project         # Google's Antigravity CLI
sticky shell  ~/my-project         # no agent at all, just a shell
sticky-chat start ~/p --agent generic --agent-cmd aider   # anything else
```

An agent's name in that position picks its profile, unless a directory of
that name is sitting there, which wins. `shell` is the one that is not an
agent: a tab with a shell in it, for reading a log or a test run or a diff.
Notes are taken there the same way, and then go to whichever tab should
see them - press its number in the sidebar, the one tmux shows in the
status line.

An agent that runs out of turns says so and stops. Sticky reads the time it
gave, waits until then plus a few minutes, and sends one word - `continue` -
so a night's work is not lost to a limit that lifted at half past midnight.
Three attempts at most: if three have not got past it, something is wrong
that waiting will not fix. Ask the tab something yourself and the three
start over - a tab that ran out last night should not spend the rest of
its life one strike from the end. The tab shows `⧗` while it waits, the
sidebar says `⧗ sending "continue" in 40 min` on a row of its own with the
hour in the footer beneath it. Click that row to call it off - it stays
there struck out, so you can see what it was going to do and click again to
put it back - or `t` then `cancel` to be rid of it.

The tab list at the bottom says which agent is in which tab, one character
in front of the name: `✦` Claude Code, `◆` Codex, `✧` Gemini, `▲` Antigravity,
`$` a shell, `•` anything else. The mark is highlighted while a tab is
waiting to be read - it printed something and then went quiet, which is
the only definition of "finished" that holds for every agent - and looking
at the tab clears it.

If a tab marks itself and you cannot see why, `sticky log <file>` records
what each sidebar decided and the rows that changed to make it decide that.
It takes effect at once, needs no reload, and `sticky log --off` stops it.

Tabs with no agent in them - shells, and any window you made in this session
yourself - get a row of their own beneath, labelled `other`. It is there
only while something is on it, so a session of nothing but agent tabs has
the one row it always had.

Nothing else you touch changes with the agent - the same keys, the same
sidebar, the same send. What changes is only what the agent itself can be
asked for:

| agent | reopens the same conversation | `C-g F` forks it |
|---|---|---|
| Claude Code | yes - sticky names the conversation when it starts it | yes |
| Gemini CLI | yes, once sticky has read the id out of its transcript | no, Gemini has no branch command |
| Codex CLI | yes, once sticky has read the id out of its transcript | yes |
| Antigravity CLI (`agy`) | yes, once sticky has read the id off its own file | no, no branch command yet |
| anything else | no; the tab comes back, the conversation starts fresh | no |

## Taking a note

- **Mouse**: drag across any output in the left pane. Releasing opens a
  prompt; type the note and press Enter.
- **Keyboard**: scroll up with the wheel or `C-g [`, press `v` to start a
  selection, move, then press `N`.
- **Shell**: `echo "some text" | sticky-chat add --note "why is this here"`

Scrolled back, the status line counts what has arrived while you were
reading — `↓ 37 new output rows below`, highlighted, clearing when you
return to the bottom.

Scrolled back, any ordinary key gets you out again: typing a letter leaves
the scroll and the letter arrives at the agent, which is what pressing it
meant.

In the prompt, **Enter** saves and **Ctrl-U** clears the line. **Ctrl-S** or **Alt-Enter** saves the note
and sends the whole batch to the agent without going via the sidebar; press
Enter in the chat pane as you would after `C-g s`. **Ctrl-C** copies the selected text to the system clipboard and
closes without making a note. **Esc** on an empty note cancels it; Esc
after typing keeps what you wrote, struck through in the sidebar — click
its `x` to bring it back. Esc or Ctrl-C while editing an existing note
leaves it as it was.

**Clicking anywhere else** finishes the note too: what you had typed is
saved, and an untouched prompt is taken back rather than left as a blank
row.

## Sending notes

Saving a note hands you to the sidebar, so `s` sends it and `S` sends it
without waiting — the keys are under your fingers already. Sending pastes
every pending note into the agent's prompt as one block, leaves copy mode if
you were reading back, and puts you at the agent's prompt where the answer is
about to appear, so Enter is the next key. From the chat pane the same two are `C-g s` and
`C-g S`. A single-line quote becomes

```
cannot find module - is this the vendored copy or the npm one?
```

and a multi-line quote becomes

```
> error: cannot find module
>     at line 42
- which of these two fails first?
```

Notes go out in the order they appear in the pane, separated by blank
lines; struck-out notes and notes with no text are left out. Sent notes
stay in the sidebar, grey and marked `✓`, so you can see what you have
already asked about.

If the paste never made it — you cleared the prompt instead of pressing
Enter — press `C-g u`, or `u` in the sidebar, to put the last batch back to
pending. Sending never deletes anything; it only changes status.
`sticky-chat commit --send` presses Enter for you.

## Keys

The prefix is `C-g`. Your own
tmux keeps whatever prefix you configured.

| key | what it does |
|---|---|
| `C-g s` | paste every pending note into the prompt; you press Enter |
| `C-g S` | the same, and press Enter for you |
| `C-g u` | unmark the last batch: pending again, not taken back (`C-g U`) |
| `C-g c` | new tab: asks for the project, then which agent (`C-g t`) |
| `C-g C` | the same, but the tab gets a window taller than the terminal |
| `C-g F` | fork this conversation into a new tab |
| `C-g o` | reopen a closed tab: lists what is remembered, a number picks |
| `1`-`9` in the sidebar | send the pending notes to that tab instead of this one |
| `C-g r` | reload: rewrite the config, restart the sidebars (`C-g R`) |
| `C-g L` | print the pending notes into the pane |
| `C-g n` `C-g w` | next tab, choose a tab |
| `C-g z` `C-g d` | zoom the pane, detach (stop looking; nothing stops) |
| `C-g Q` | quit: close every window and stop the agent sessions |
| `C-g [` | scroll back through the output |

Over the chat pane, drag with the mouse to note a selection, or press `N`
in copy mode. **Selecting copies**, in either pane and whether or not the
selection becomes a note, the way it does in an X11 terminal. `y` in copy
mode copies the live selection again, wherever you are, and leaves it
highlighted, and Ctrl-C in the note prompt copies and closes without
saving anything.

Scrolled back, the lines you have already annotated are lit. Copy mode is
the one place tmux will paint over a pane's own output — it colours the
matches of a search — so entering it sets the search to what your newest
notes quote. Quiet on purpose: it is there to be recognised out of the
corner of an eye rather than read. Only the eight most recent, and only
their first forty characters, because that search runs inside the tmux
server and every pane waits while it does.

Inside the sidebar the letters work on their own, without the prefix:

| key | what it does |
|---|---|
| `s` `u` `F` | send, unmark, fork — as above |
| `t` | send later: asks when, filled in with the reset time the agent printed; `cancel` calls one off |
| `↑` `↓` or `k` `j` | move the cursor from note to note |
| `PgUp` `PgDn` `Home` `End` | a screenful, the first note, the last |
| `Enter` | edit the note under the cursor |
| `space` | scroll the chat pane to the text that note came from |
| `x` | strike the note out; again brings it back — the cursored note's `[x]` is drawn bold |
| `Esc` `g` `G` | drop the cursor, back to the live map |
| `Tab` `>` `<` | next tab, next tab, previous tab |
| wheel | page back through every note in this conversation |
| click `sticky` | bottom left of the status line: cross to the sidebar, and click again to cross back |
| `r` | re-read the notes file |
| `?` or `h` | the help panel; any key goes back |
| `q` | close the sidebar |
| any other letter | goes to the chat pane, which takes the focus with it — start typing a message with your eye on the notes |

The list scrolls to follow the cursor; the wheel pans it freely and the
next arrow key snaps the cursor back. Striking out is reversible, and
`sticky-chat rm <id>` is the only thing that deletes a note.

With the mouse: **single click** a note and the chat pane scrolls to the
text it came from, centered, unless it is already on screen. **Double
click** to edit it — the prompt starts from its current text, and changing a
note that was already sent puts it back to pending. Clicking the `[x]` at
its right edge strikes it out; clicking again brings it back. Dragging inside
the sidebar selects text and leaves the selection standing until `Esc` or
`q`.

## The sidebar

The lower part is a map of the chat pane: each note sits on the row its
text is on, with a marker spanning the rows it covers, and reads
`- your note` under a dimmed copy of the quoted line, or `✓ your note` once
sent. Yellow means pending, grey means sent, orange means the text has
changed enough that the position shown is a guess.

Above that, over a rule, is a dense list of the notes *above* what you can
see — whatever has scrolled off the top, nearest one at the bottom. It says `↑ N more` on its top row
when the list is truncated.
Below the map, over a rule of its own, is the same list for the notes
*below* what you can see — the ones the pane has not reached yet, nearest
one at the top.

The bottom row is the footer: what is pending, `↑` how many notes are above
the screen, `↓` how many are below, `u unmark` once anything has been sent,
and `? help`. While anything is pending, the row above reads
`s submits · C-g s from the chat pane`. The status line at the very bottom of the
window carries `s`, `c`, `n`, `w` and `d`, and the sidebar shows the full
help panel until you take your first note.

Scroll the wheel over the sidebar and the upper list pages back through
every note you have taken in this conversation, no longer tied to anything
on screen. `g` or `Esc` returns to the live map. While you are scrolled
back the map is frozen, so nothing moves under you.

## Commands

| command | effect |
|---|---|
| `sticky-chat start [dir] [-- agent args]` | open an agent + sidebar window |
| `sticky-chat new [dir]` | another window for another project |
| `sticky-chat` / `sticky-chat attach` | re-attach to what is running |
| `sticky-chat list [--all]` | show pending notes, or all of them |
| `sticky-chat commit [--no-paste] [--send] [--quiet]` | render, paste, mark sent |
| `sticky-chat commit --at WHEN` | send later: `4h`, `90m`, `00:32`, `2026-09-16 00:32`, or `cancel` |
| `sticky-chat uncommit [id…] [--all]` | mark sent notes pending again; default is the last batch |
| `sticky-chat fork` | branch the conversation into a new tab |
| `sticky-chat restore` | reopen a sidebar you closed |
| `sticky-chat reload` | rewrite the config and restart every sidebar |
| `sticky-chat quit` | close every window and stop the agents in them |
| `sticky-chat resume [--all] [--yes]` | reopen the tabs that were open last time; `--all` goes further back |
| `sticky-chat add --note "…"` | make a note from text on stdin |
| `sticky-chat rm <id>` | delete one note |
| `sticky-chat clear [--committed]` | delete every note, or only the sent ones |
| `sticky-chat log [file]` / `--off` | record what the sidebars decide, and why; `log` alone says where it is going |

`note`, `sidebar`, `click`, `place`, `fit`, `marks` and `trace` also exist,
driven by the key bindings and tmux hooks rather than typed by hand. With no subcommand,
`sticky-chat` joins the session already running, or starts one in the
current directory.

Flags on `start` and `new`:

| flag | effect |
|---|---|
| `--sidebar-width N` | columns for the sidebar (default 34) |
| `--agent NAME` | which agent profile: `claude`, `gemini`, `codex`, `generic` |
| `--agent-cmd CMD` | launch something other than the profile's command |
| `--local` | keep notes in the project, not in `~/.sticky` |
| `--detach` | create the window without attaching to it |
| `--menus` | let Claude open multiple-choice question menus |
| `--virtual-rows N` | give the agent a window N rows tall, taller than the terminal |

Claude profile includes `claude --disallowedTools AskUserQuestion`, so Claude answers in prose
instead of opening a menu.

## Forking a conversation

`C-g F`, `F` in the sidebar, or `sticky-chat fork` opens a second tab on
the same project, i.e., running `claude --continue --fork-session` with the first
tab's arguments repeated. The agent replays the conversation into a new
session id, so you get two lines of enquiry from the same context, side by
side.

The fork gets its own copy of the notes that were still pending, under its
own session id. From then on the two tabs are independent:
asking a question in the fork does not mark it answered in the original,
and notes taken in one do not appear in the other. Carried notes keep their
text and their quotes, so `C-g s` sends them with full context; whether
they line up with a row depends on the replayed transcript still holding
the text they came from, and where it does not they are listed above the
rule instead.

`--continue` reads the transcript the agent writes to disk as each exchange
completes, so a fork carries every finished turn but not one still being
written. If the agent is mid-answer, wait for it to land before forking.

## Detaching, and stopping

**`C-g d` detaches.** That means you stop looking at sticky, not that
anything stops. The tmux server keeps running in the background with every
tab in it: the agent carries on, mid-task included, and your notes stay
where they are. You are simply returned to the shell you started from.
Closing the terminal window does exactly the same thing — a session cannot
be lost by shutting a window on it.

Come back to all of it with:

```sh
sticky-chat
```

Detaching is the whole session, not one tab. There is no way to detach a
single tab, because a tab is not a thing you are attached to — it is a
window inside the session you are attached to.

To actually stop — close all the windows and end the agent sessions:

```sh
sticky-chat quit          # or C-g Q
```

It names the projects it is about to close and waits for a yes. Nothing is
lost: your notes are files, and every tab is recorded, so

```sh
sticky-chat resume
```

brings back exactly the set that was open when you quit — and a reboot, a
crash or a lid closed on a Friday comes back the same way, because none of
this depends on having quit properly.

## Coming back after a restart

A reboot takes the tmux server with it, but not your work. Sticky records session ids and remembers what it would take to reopen it: the project,
the command line, the note store, the sidebar width. Afterwards:

```sh
sticky-chat resume
```

What comes back is the last set that was open, not every tab you have ever
had. Nobody records the moment a machine reboots, so it is worked out from
what is already written down: a sidebar stamps its record every minute
while it runs, so the tabs that stopped together are the ones stamped
within a heartbeat of each other, and one you closed at lunchtime is hours
behind them. A clean `sticky-chat quit` is the same question with the same
answer.

The set is named and taken as a set, the way it went away:

```
sticky: 3 tabs were open 7h ago: sticky-chat, notes-api and 1 more. Reopen
them, or p to go through them one at a time? [y/n/p]
```

`p` asks about each one in turn, and says what its letters do before it
starts — `y` reopens it, `n` skips it, `a` reopens all the rest, `q` stops.
`--yes` asks nothing, which is what a login item would use. Running
`sticky-chat` with nothing running offers the same set in one line.

To reach further back there is `--all`, which goes through every tab ever
opened, one at a time, and `C-g o` (`sticky-chat reopen`), which lists them
newest first and puts one back.

A tab is remembered until you say otherwise. If some have gone untouched
for a year, `resume --all` offers once to forget them. Should a
conversation itself be gone, the tab still opens on the same project with a
fresh one, and says so.

## What survives what

| event | tmux session | the agent's conversation | your notes |
|---|---|---|---|
| closing the terminal window | survives — the tmux server is a daemon, so `sticky-chat` re-attaches | keeps running, mid-task included | kept |
| logging out or rebooting | gone; the server dies with the machine | `sticky-chat resume` brings back the set that was up | kept |
| `q` in the sidebar | window stays, `C-g r` reopens the pane | untouched | kept |

Notes are files, so they are the one thing that always survives.

## Where notes live

`~/.sticky/<project>-<hash>/<session-id>/notes.json` by default: one
directory per conversation, under the project it belongs to. That is what
makes a new tab start clean and a resumed tab find its notes again - the
agent's session id names the directory, so the same conversation always
comes back to the same notes.

Pass `--local` to keep them in `<project>/.sticky/<session-id>/notes.json`
instead; every command detects that directory on its own, so nothing else
needs a flag. A note taken outside a sticky tab (`sticky-chat add` from a
plain shell) joins whichever tab is open on that project; with two open it
asks which, and with none it lands in the project's own `notes.json` beside
those directories.

| variable | effect |
|---|---|
| `STICKY_HOME` | move the whole state directory (default `~/.sticky`) |
| `STICKY_LOCAL` | keep notes in the project, as `--local` does |
| `STICKY_SOCKET` | change the tmux socket name (default `sticky`) |
| `STICKY_VIRTUAL_ROWS` | give every tab a window this many rows tall |

## Settings

`~/.sticky/tmux.conf` is regenerated on every start and every reload, so
put your own tmux settings in `~/.sticky/user.conf`. It is sourced last, so
anything there wins:

```sh
# ~/.sticky/user.conf
set -g status-style "bg=colour236,fg=colour250"
set -g status-right "#[fg=cyan]C-g s#[default] send  #{session_name} "
```

The status bar sets no colours of its own — it takes the terminal's
background and foreground and tells key names apart by weight — so it stays
readable in a light or a dark scheme.

## Troubleshooting

| symptom | cause |
|---|---|
| `sticky-chat: command not found` | the install directory is not on your `PATH` |
| `the command exited immediately` | the agent is not on your `PATH`; pass `--agent-cmd /full/path/to/claude` |
| no prompt after a drag | tmux older than 3.7, which has no floating panes — check `tmux -V` |
| a note turns orange | the text under it changed; the position shown is a guess |
| notes stuck off-screen | scroll back to the text, or `sticky-chat list` to see them all |
| `C-b` does nothing | inside sticky-chat the prefix is `C-g` |
| the sidebar is gone | `C-g r`, or `sticky-chat restore` |

`docs/DEV.md` covers the internals: how a note stays attached to its text, the
generated tmux config, the tall window, and how to run the tests.
