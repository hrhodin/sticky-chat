# Working on sticky-chat

Mechanism and reasoning. `README.md` is the guide for using the tool, and
`.github/CONTRIBUTING.md` is what to do before opening a pull request: how to
set up, what to run, and what a change is expected to come with.
`.github/SECURITY.md` covers reporting a vulnerability - GitHub finds it
there and links it from the repository itself, so the root stays down to
what a reader opens.

## Layout

| path | what it is |
|---|---|
| `src/sticky_chat/` | the program, standard library only; `__init__.py` re-exports so it reads as one namespace |
| `bin/sticky-chat`, `bin/sticky` | launchers that run it straight from a checkout; the same program under both names |
| `tests/` | the suite: `conftest.py` holds the fixtures, the rest split by subject |
| `pyproject.toml` | packaging, the `sticky-chat` and `sticky` console scripts, and the ruff and pytest settings |
| `PLAN.md` | the design |
| `NOTES.md` | the research behind it |
| `tmux/` | a local clone of upstream tmux, kept for reading the source; gitignored, and nothing at runtime looks at it |

At runtime everything sticky-chat owns lives under `~/.sticky`
(`STICKY_HOME`): the generated `tmux.conf`, your own `user.conf`, and one
directory per project, holding one directory per conversation with that
conversation's `notes.json` and `sidebar-rows.json`.

Installed (`pip install .`) the console scripts are the entry point; from a
checkout `bin/sticky-chat` is. Either way `self_path()` records whichever
was used in the generated tmux config, so a session keeps working without
reinstalling. `sticky` is the same program under a shorter name, which is
what makes `sticky codex ~/proj` read the way it does.

The files, in dependency order - each one imports only from those above it:

| file | what is in it |
|---|---|
| `util.py` | colours, text measuring and wrapping, raw key reading, `die` |
| `tmux.py` | the `Tmux` wrapper; nothing else talks to tmux |
| `agents.py` | one profile per coding agent: everything that is Claude's rather than ours, including where it writes its transcript |
| `clipboard.py` | `pbcopy` / `wl-copy` / `xclip` / `xsel` |
| `store.py` | `Store`, which store a pane is showing, and the tab records |
| `placement.py` | which row on screen a note belongs to |
| `sidebar.py` | what the right-hand pane draws, and its keys |
| `config.py` | the generated tmux config, and sizing the window |
| `commands.py` | one function per subcommand |
| `cli.py` | argument handling: what is ours and what is Claude's |

`main` dispatches to `cmd_*` functions through
`build_parser`. `Tmux` wraps `tmux -L <socket> …` and does all the talking;
`Store` is the note file, written atomically through a temporary file and
`os.replace`. Every subcommand that acts on notes takes `--pane` and
`--socket` so a key binding can hand it a context, and `--quiet` where tmux would otherwise
print its stdout into the pane.

## Which notes a pane sees

A note quotes one conversation's output, so it belongs to that conversation.
A note's remembered row is `history_size + offset`, which counts from the
top of one pane's scrollback and means nothing in another - and notes now
outlive panes, since a conversation gets resumed, reloaded and reopened. So
a note records the pane its position was taken in, and `resolve` treats a
position from any other pane as no position at all. Believed, it put notes
below the bottom of a pane they had never been shown in, where they stayed
for ever: five of them in the author's own store, pinned at rows 4357-5321
of a pane whose whole history was 3876 lines.

`session_dir(project, session)` puts it in
`<project store>/<claude session id>/`, and `cmd_start` writes that path onto
both panes as `@sticky_store`; every later command reads it back through
`open_store`, so nothing else has to know how the name is built. A command
with no pane at all — `echo … | sticky-chat add` from a plain shell — has
nothing to read it from, so `open_store` asks `sidebar_showing` which tab is
open on the project and joins that one. The project's own `notes.json` is
where it lands only when no tab is open, because a note in a store nobody
is drawing is a note you can neither see nor send. Three
consequences fall out of the one rule:

- a new tab on a project already open elsewhere starts with an empty
  sidebar, because its id is new;
- `resume` reopens the conversation by id (`--resume <id>`), so the same id
  finds the same directory and the notes are there again, whether or not the
  window record still remembers the path;
- `fork` copies the still-pending notes into the directory named after the
  fork's own new id, so the two tabs diverge from that point.

A bare `--continue` is the gap: Claude picks the id and never tells us, so
the tab gets a `tab-<random>` directory instead and only the window record
leads back to it. A note taken with no tab at all (`sticky-chat add` from
a plain shell) has no conversation to belong to, and falls back to the
project's own `notes.json`.

## The reload loop

`C-g r`, or `sticky-chat reload`, is what to press while changing the
script. It rewrites `~/.sticky/tmux.conf` from `CONFIG_TEMPLATE`,
`source-file`s it into the running server, then kills and reopens every
sidebar so each comes back running the current code, keeping its width,
project and note store. The Claude panes are never touched, so it is safe
mid-conversation.

Edit, `C-g r`, look. Only changes to `cmd_start` itself need a new tab.

## Running the tests

```sh
pytest                        # everything, about a minute
pytest -m "not integration"   # the pure half, about a tenth of a second
```

No attached terminal is needed. The integration half kills and starts its
own tmux server on the `stickytest` socket, points `STICKY_HOME` at a
temporary directory, and for the parts that need a real client attaches to
itself from a second server on `stickyouter`. Without tmux installed those
tests skip rather than fail.

| file | what it covers |
|---|---|
| `test_placement.py` | `place_note` and `resolve`: exact, fuzzy, landmark, the drift guard, prompt-box detection |
| `test_rendering.py` | `build_frame`: the map, the band, the cursor, strike-through, `render_commit` |
| `test_cli.py` | flag splitting, `--help` staying ours, the placeholder regex, the generated config, the agent profiles, the reopen picker |
| `test_integration.py` | capture, placement, commit, uncommit, local stores, the bracketed paste, the live `copy-pipe` binding |
| `test_session.py` | `start`, `fork` and its separate note store, the tall window, a tab on another agent, and reopening a remembered one |

Lint with `ruff check .`. There is no `ruff format`: the source is
hand-wrapped with aligned trailing comments that the formatter flattens.

## Releasing

There is no build script; `pyproject.toml` is the whole definition and the
build backend does the rest.

```sh
python3 -m build        # writes dist/*.whl and dist/*.tar.gz
```

The wheel is pure Python with no dependencies, so one wheel serves every
platform and version the metadata claims. To check a build rather than trust
it, install it somewhere clean:

```sh
python3 -m venv /tmp/check && /tmp/check/bin/pip install dist/*.whl
/tmp/check/bin/sticky-chat --version
/tmp/check/bin/sticky --version         # both console scripts, one program
```

To publish, bump `__version__` in `src/sticky_chat/__init__.py` - hatchling
reads it from there, so nothing else needs editing - then tag it:

```sh
git commit -am "Release 0.2.0" && git tag v0.2.0 && git push --tags
```

The tag triggers `.github/workflows/release.yml`, which builds and uploads
through PyPI trusted publishing: the workflow's OIDC token is the
credential, so there is no token to store or rotate. **This only works once
the PyPI project names this repository and workflow as a trusted
publisher**, which has to be done by hand for the first release.

## How a note stays attached

A note is a JSON record. Besides `id`, `status`, `note` and `quote` it
stores:

| field | what it holds |
|---|---|
| `rows` | the full pane rows the selection sits on, not just the selected characters |
| `sx`, `ex`, `partial` | the column range, and whether the quote is less than the rows |
| `before`, `after` | `CONTEXT_ROWS` (2) rows either side |
| `landmark` | the nearest structural line above, and its offset |
| `abs_line` | where it was last drawn, in absolute pane lines |
| `anchor_abs` | where it was last matched *exactly* |
| `span` | how many rows it covers |

`find_landmark` walks up to `LANDMARK_WINDOW` (40) rows back looking first
for a line matching `GLYPH_LANDMARK` — Claude's tool headers and prompt
markers, `^\s*[>●⏺✦✻*]\s+\S` — and failing that for the nearest line of
three characters or more that starts hard against column zero, since output
is normally indented.

`note_candidates` then offers every plausible row for one note, best first,
in three tiers:

- **exact**: the visible rows equal `rows`, and at least 75% of the
  comparable context rows match as well.
- **approximate**: `SequenceMatcher` over the joined rows scores at least
  `FUZZY_THRESHOLD` (0.8). Only tried when the quote has at least
  `MIN_FUZZY_CHARS` (8) non-space characters, so a short fragment cannot
  drag a note somewhere by accident.
- **landmark**: the landmark line is on screen; the note goes at its
  recorded offset below it.

Within a tier the row nearest to where the note was last seen wins.

`resolve` places every note against the others rather than one at a time.
Exact matches are handed out first and take ownership of the rows they land
on, so a note that only resembles that text is not drawn on top of one that
demonstrably belongs there. Then the rest are placed, skipping owned rows,
and refusing an approximate match that lands more than `FUZZY_DRIFT` (6)
rows from `anchor_abs`. Absolute line numbers do not change as output is
appended — only a repaint moves text, and never far — so a candidate far
from the anchor is similar-looking output somewhere else, not the same
text. Scrolling away from a note's text therefore lists it above the rule
rather than re-attaching it to a lookalike.

A placement pass updates `abs_line`; only an exact match updates
`anchor_abs`. Notes with no candidate are reported `offscreen`, with
`where` set to `above` or `below` so the footer can count them in the right
direction. `sticky-chat place --pane %N` prints a pass as JSON.

`pane_view` reads `#{history_size}`, `#{pane_height}` and
`#{scroll_position}` in one format, captures the visible rows, then reads
the same format again; if the pane moved between the two, the pass is
retried, up to three times. Otherwise a scroll landing between the two
round trips would shift every note by however many rows moved.

## The sidebar loop

`cmd_sidebar` is the program running in the right-hand pane. Nothing in it
is on a clock: it sits in `select` and is told when to look. The one
exception is bounded and rare - a tab whose agent chose its own conversation
id looks for it every `DISCOVER_EVERY` for the first minute of the tab's
life, because no hook can say "the transcript has been written now"; see
[Learning the id an agent chose](#learning-the-id-an-agent-chose).

| what changed | what tells it |
|---|---|
| Claude printed | `pane-activity` sends one `\0` byte to the pane beside it |
| a note was written | `nudge` sends `SIGUSR1` to `@sticky_sidebar_pid` |
| copy mode entered or left | `pane-mode-changed`, the same signal |
| the room changed | `client-resized`, `after-select-window` → `cmd_fit`, which signals every sidebar it resizes |
| Claude ended | `pane-exited` → `cmd_sweep` closes the sidebar outright |
| you pressed a key | the key itself |

`SIDEBAR_IDLE` (30 s) is the timeout on that `select` — not a heartbeat,
nothing is expected to arrive on it, a seatbelt so that a wake which never
comes leaves the pane stale for half a minute rather than for ever.

**Only where tmux can wake it.** `pane-activity` is on master and in no
release, so on tmux 3.7 `install_hooks` fails, `@sticky_wake` is `0`, and
the loop uses `SIDEBAR_TICK` (0.5 s) instead: a poll, as before. Everything
below about being woken describes the tmux this was written on
(`next-3.8`). On a released tmux the sidebar is correct but busier, and
`SIDEBAR_BUSY` (0.5 s, the cap on how often notes are placed) and
`SIDEBAR_FROZEN_TICK` (2 s, while you are scrolled back through the note
history) are what bound the cost.

A wake schedules one more pass `SIDEBAR_SETTLE` (0.3 s) later, with
`last_sig` cleared so it redraws whether the frame changed or not. That
trailing pass is not decoration. A frame is computed from a capture of
Claude's pane, and a capture taken mid-burst is a photograph of a repaint
in progress: the placements in it can be wrong, and the frame it produces
gets cached in `last_sig`, so an identical frame computed later would not
be drawn. The settle pass is what looks again once the screen has stopped
moving. With a heartbeat there was always another pass along shortly; with
nothing on a clock this is the only one.

The pid is published as the `@sticky_sidebar_pid` pane option on both panes
so either side can wake it. Measured: a note edit reaches the screen in
23 ms instead of 540 ms, new output redraws in about 340 ms (the immediate
pass, then the settle), and an idle sidebar uses no measurable CPU at all —
that last one on master tmux; on 3.7 it is the half-second poll's cost.

### What a pass may cost

The heartbeat is cheap and what it triggers is not, so both have a budget.

A tmux call costs the process it starts and almost nothing else — a capture
of fifty rows and a one-word question both take about 4 ms — so questions
travel together: `Tmux.many` puts several commands in one invocation,
`formats` asks several panes at once, and `capture_with` takes the capture
and the scroll check that follows it in the same breath. A pass makes two
invocations rather than five.

A pass costs three things, and profiling says which. On 87 notes against a
real pane: asking tmux for the screen 9.0 ms, placing the notes 1.5-7 ms,
drawing the frame 0.2 ms. Two thirds of it is tmux, and nearly all of that
is the process a call starts rather than the answer - so the loop asks its
three questions in one invocation, `VIEW_FORMAT` included, and hands the
answer to `placements` rather than letting it send a round trip of its own.

Most notes are nowhere near the screen, and the cheapest comparison is the
one not made. A note that has been placed exactly once carries `anchor_abs`,
and `resolve` refuses any *approximate* candidate more than `FUZZY_DRIFT`
from it - so when the whole visible window is further away than that, every
candidate the approximate pass could produce would be thrown out again.
Those notes skip it. Nothing is given up: the exact scan is a list compare
per row and still runs, so text that reappears somewhere else is still
found wherever it is. On a store of 88 notes against a screen made of the
text they quote, placing went from 70 ms to 1.5 ms.

An anchor belongs to a pane, though, so a tab that has just started has
none and every note is searched in full until it has been placed once -
about 7 ms a pass rather than 1.5. That is the price of not believing a row
counted in some other pane, and it buys itself back as the notes are
placed.

The rest of the cost was `note_candidates`, which compares every note against
every row it could sit on. `difflib.SequenceMatcher.ratio` is quadratic,
and with 57 notes on a 50-row screen a single pass took 640 ms — longer
than the tick, so the sidebar ran permanently one pass behind. It now
rejects on `real_quick_ratio` and `quick_ratio` first: both are documented
upper bounds on `ratio`, so a row they put under `FUZZY_THRESHOLD` cannot
reach it and skipping it cannot change the answer. Same placements, 20x
less work; a sidebar showing 60 notes against the text they quote costs
about 1.7% of a core.

There is no poll left where tmux can say so. What replaced it was
`pane-activity` — "run when there is new output in a pane" — which fires for the current window and
hands the right pane in both `#{pane_id}` and `#{hook_pane}`. tmux
coalesces: 250 writes produced 53 firings, one per burst.

The firing has to be free, or a burst of output would cost more than the
heartbeat did — `run-shell -b "kill -USR1 …"` would fork ten times a second
under load. So the hook sends a byte instead, and starts nothing:

```
set-hook -g pane-activity 'if -F "…" { send-keys -t + -H 00 }'
```

`-t +` is the other pane of the two, and it has to be written that way: a
target is not format-expanded, so `-t "#{@sticky_partner}"` addresses a
pane literally called `#{@sticky_partner}`. The guard checks
`#{window_panes}` is 2, since `+` only means "the sidebar" while the tab
has not been split further.

Format subscriptions were tried first and do not work on this tmux, which
is worth knowing before anyone tries again. The idea is right: tmux expands
a format itself, once a second, and tells you only when the value has
changed — the polling happens in the server, in process, and you are woken
only for something real. Two entry points, neither of which fired on
next-3.8:

- `set-hook -B name:what:format` installs a monitor hook. `show-hooks -B`
  lists the subscription afterwards, in every scope and argument form
  tried, and the hook never runs.
- `refresh-client -B name:what:format` is the same for a control-mode
  client, reported as `%subscription-changed`. A control client attaches
  fine and does emit `%output`, `%window-renamed` and the rest, but no
  `%subscription-changed` arrives for a format that demonstrably changed.

So the mechanism is the feature, not the syntax, and the backstop here is
`SIDEBAR_IDLE` instead. If a second opinion on "Claude printed" is ever
wanted, control mode's `%output` does work — at the cost of an extra
attached client, which matters because window size follows the smallest
one.

While the sidebar's own pane is in copy mode it does not redraw at all: a
repaint under a live selection makes the selection jump or vanish.

`draw` wraps each frame in DECSET 2026, so a terminal and a tmux that
support synchronized output (tmux 3.7 and later) never show a half-drawn
sidebar.

Each frame writes `sidebar-rows.json` — the pane id, and for every drawn
note its row, its id, the column of the `x` target and whether it is on
screen. `cmd_click` reads that file to turn a mouse position into a note.
A double click also produces a `MouseUp`, so a double click arrives as
jump, edit, jump; a `.last-edit` guard file drops the trailing jump.

`build_frame` draws in *pane* coordinates and takes `top`, the client's
`#{window_offset_y}`, as the first row the terminal actually shows, so in a
window taller than the client the band still sits at the top of the screen
rather than the top of the window.

`foot_lines` is the mirror of `band_lines`, for the notes `placements`
marked `where: "below"`. It takes its rows off the bottom of the map —
`map_h`, not `body_h`, bounds every aligned note — and never off the band
above, so the two lists cannot squeeze each other out. Its entries go into
`hits` like any other, so a click jumps and the button strikes out.

## Which pane has the keyboard

`open_sidebar` sets `window-active-style` on the sidebar's own pane, and
that is the whole of it: tmux paints a pane's ground itself, empty rows
included, and that option applies exactly while the pane is the active one.
No focus tracking, nothing in the draw loop, and Claude's side is never
touched - the sidebar lights up rather than the transcript dimming.

Three things were tried before that one. `\x1b[K` does not carry the
background here, so painting rows from the frame left the empty ones bare.
Padding each row by hand painted them but overwrote the `[x]` button, which
is placed by the line itself. And a program in the pane *can* watch its own
focus - DECSET 1004, `\x1b[I` and `\x1b[O` arrive on stdin and `read_key`
can name them - but it needs its starting state from tmux, because focus is
reported when it changes and no change is coming for focus a pane never
had. All of that is unnecessary for a background; keep it in mind if some
element of the frame ever needs to change with focus, which the style
cannot do.

Note that `select-pane -P` sets a pane style too, and also *selects* the
pane, which is not what a sidebar reporting on itself wants.

## Where a prompt lives

All three prompts - the note text, the project for a new tab, and the list
of tabs `C-g o` offers to reopen - run in a **floating pane**, made with
`new-pane`, not in a `display-popup`. They look the same, but a popup is not
a pane, and that difference is the whole reason:

`popup_key_cb` in tmux's `popup.c` is handed every key and mouse event while
a popup is up, and for a mouse event outside the popup's rectangle it
returns 0 - consumed. No binding runs, no hook fires, and the popup's own
program is never told. Clicking away from a popup is invisible to
everything. I checked this three ways before believing it: a
`MouseDown1Pane` binding logging zero firings, a program in a popup with
mouse reporting on seeing clicks inside and nothing outside, and finally the
source.

A floating pane is a pane. Clicking off it is an ordinary change of active
pane, `window-pane-changed` fires, and `cmd_sweep` closes any pane marked
`@sticky_role=note` that is not the active one. Closing it sends SIGHUP to
the prompt, and `prompt_line`'s `on_hangup` saves whatever had been typed -
so "click outside to finish" is three lines of hook and a role option.

`window-pane-changed` fires on every click that moves focus, and starting
sticky to find nothing costs 37 ms, so the hook is guarded on
`#{!=:#{window_panes},2}`: a sticky window is two panes, and three while a
prompt is up. The click that closes a prompt is also on its way to
`cmd_click`, which is why `dismissing` exists - one click should do one
thing, and reaching for the nearest place to click is not the same as
asking to strike a note out. It asks twice because the answer comes from
two directions: the prompt may still be there, or the sweep may have got to
it first and left the time behind in `@sticky_dismissed`.

`C-s` and `Alt-Enter` in the prompt save the note and run `commit`, which
is `s` in the sidebar without the trip through it. Those two keys because
those two are the ones that arrive: raw mode makes `C-s` a byte rather than
flow control, and `Alt-Enter` comes through as an escape and a return.
`Shift-Enter` and `C-Enter` are both plain `\r` at the pane, whatever
`extended-keys` says - measured, not assumed.

Two things follow from it being a pane rather than an overlay:

- `new-pane` does not block, where `display-popup -E` did. Whatever used to
  happen after the popup returned - telling the sidebar, handing focus to it
  - happens in `cmd_note` now, which is the thing that knows when it is done.
- the window has three panes while a note is being typed, and the
  `pane-activity` wake is guarded on a window of two, so the sidebar holds
  still until the prompt closes. That is the freeze `holding` used to do,
  for free and without a freeze.

Floating panes are tmux 3.7 and later, which is why that is the version the
README asks for. tmux master has `new-pane -O -C`, a modal pane that closes
itself when clicked outside - the same behaviour with none of our hook - but
it was merged three days before this was written and no release has it yet.

## Knowing that Claude is still talking

Scrolled back, the view is anchored to the text, not to the bottom: the line
you are reading stays under your eyes while output piles up behind it. That
is the right behaviour and it hides something - `#{scroll_position}` does
*not* grow as those lines arrive (measured: 6, still 6, while the history
went 20 to 58), so nothing tmux reports says a reply has been printing.

The sidebar is the only thing that can work it out. It reads
`#{pane_in_mode}` for the Claude pane in the invocation it was already
making, remembers the history size at the moment reading began, and puts the
difference in the `@sticky_new` window option, which `status-right` reads
back. The status line rather than the sidebar's own footer, because that is
the bottom of the window you are looking at rather than the foot of the pane
beside it - and it is drawn already, so the count costs no room. tmux redraws
the status on its own slow clock, so `refresh-client -S` asks it to look;
both calls happen only when the number changes.

The wording grows and shrinks with the room: `#{client_width}` less twelve
columns a tab, and above ninety-five it says "new output rows below" rather
than "below". Note that `#{>:100,90}` is *false* - those comparisons are on
text - so the width test is `#{e|>:}`, the arithmetic one. And when the tabs
will not fit, tmux truncates the window list rather than `status-right`, so
the count never disappears; shrinking it is a courtesy to the tab list, not
a way of saving the count. Note that `capture-pane` shows a pane's *screen*
rather than the copy-mode view, so it is no use for checking any of this -
capture the attached client instead, which renders what you are looking at.

The count is written with colour, which is what made `truncate` measure
lines by their bytes rather than their width and cut the footer in half.
It now skips escape sequences when counting and never cuts one in two.

## Typing out of a scroll

Reading Claude's output means scrolling, and scrolling means copy mode,
where every letter is a motion - so the first thing you type after reading
goes nowhere at all until you know to press `q`. `typing_keys` generates a
binding for every printable character that leaves the mode and delivers the
key: `send -X cancel ; send-keys`, where `send-keys` with no argument
resends whatever was pressed, so one shape does for all of them.

Three things about it are not obvious. Both `copy-mode` and `copy-mode-vi`
get every binding: the generated config sets `mode-keys vi`, but a
`user.conf` can put it back to emacs, and only one of the two tables is
live at a time. The `Any` key does not work here - the manual says
it runs for keys with no binding of their own, but nothing fires in a mode
table, so the keys have to be named. And they are single-quoted, because
tmux expands a leading `~` inside double quotes: `bind -T copy-mode "~"`
binds your home directory, and reports it only when the file is sourced, by
which point the rest of the file is lost.

`RESERVED_IN_COPY_MODE` is what stays behind: `N` takes a note, `y` copies,
`q` leaves, `v` starts a selection. Everything that is not a printable
character is untouched, so the arrows, the page keys, the wheel and the
control keys that search all still work.

## The generated tmux config

`write_config` substitutes five placeholders into `CONFIG_TEMPLATE` and
writes `~/.sticky/tmux.conf`: `@BIN@` the absolute path to the script,
`@HOME@` the state directory, `@CLIP@` the clipboard helper found on this
machine. The file is regenerated on every `start` and every `reload`, which
is why user settings belong in `~/.sticky/user.conf`; the template sources
that last, so anything in it wins.

The wiring between panes is carried in pane options rather than in any
state of sticky's own, so every subcommand can rediscover its context from
a pane id alone:

| option | meaning |
|---|---|
| `@sticky_role` | `claude`, `sidebar`, or `note` for a prompt |
| `@sticky_partner` | the other pane's id; for a prompt, the Claude pane |
| `@sticky_project` | the project directory |
| `@sticky_width` | the width the sidebar was opened at |
| `@sticky_store` | the note directory: this conversation's, not the project's |
| `@sticky_session` | the conversation id, or a `tab-` key for a bare `--continue` |
| `@sticky_agent_cmd` | the command line the tab was started with |
| `@sticky_agent` | which agent profile the tab was started on |
| `@sticky_sidebar_pid` | the sidebar process, for `SIGUSR1` |
| `@sticky_virtual_rows` | window option: the height asked for |
| `@sticky_new` | window option: rows printed while you read, for the status line |
| `@sticky_wake` | global: whether this tmux can say when Claude printed |
| `@sticky_dismissed` | global: when a click last closed a prompt |
| `@sticky_focus_style` | global, yours to set: the ground under a focused sidebar |

Bindings:

- `N` in both copy-mode tables: `copy-pipe-no-clear` into
  `sticky-chat add`, then `stop-selection`. No-clear keeps copy mode and
  the highlight, so a note taken while scrolled back leaves you where you
  were reading; `stop-selection` finishes the selection off so the mouse
  stops dragging it about. `cmd_add` drops out of copy mode itself, but
  only when the pane was at the bottom.
- `MouseDragEnd1Pane` in both copy-mode tables: the same, guarded by
  `#{==:#{@sticky_role},claude}`; over any other pane it is a plain
  `copy-pipe-no-clear`, which fills tmux's buffer and leaves the selection
  standing.
- `y` in both copy-mode tables: `copy-pipe-no-clear` into `@CLIP@`.
- `MouseUp1Pane` and `DoubleClick1Pane` in the root table: `sticky-chat
  click` when the pane is the sidebar. These replace tmux's defaults and
  have no else branch, so a double click over Claude no longer selects a
  word.
- `WheelUpPane` / `WheelDownPane` in the root table: send `C-y` / `C-e` to
  the sidebar so it pages its own list; anywhere else, tmux's own default
  behaviour, reproduced verbatim.
- Prefix keys: `s`/`S` commit, `u`/`U` uncommit, `r`/`R` reload, `F` fork,
  `L` list, `Q` quit, `c`/`t`/`C` a new tab, `o` reopen a remembered one.
  The ones that would otherwise
  print pass `--quiet`, because tmux shows a `run-shell` command's stdout
  inside the pane and anything printed lands on top of Claude — `L` is the
  exception, since printing into the pane is the whole of what it does.
- The new-tab and reopen keys go through `run-shell`, which is the thing
  that expands the `#{}` in what it runs. Name the prompt in the config
  instead and sticky is handed the literal string `#{socket_path}`.
  `cmd_start --ask` opens the prompt itself, from Python, where the values
  are real; inside it `--ask-here` is what actually asks. `cmd_reopen --ask`
  is the same two steps with no second flag: the prompt runs a plain
  `reopen`, because picking a tab is the only thing that subcommand does and
  a marker saying "really ask this time" would mark nothing.

Hooks:

- `client-resized`, `client-attached`, `after-select-window` run
  `sticky-chat fit`.
- `pane-mode-changed` sends `SIGUSR1` to `@sticky_sidebar_pid` and runs
  `fit` as well, so the viewport pin is reconsidered the moment copy mode
  is entered or left.
- `pane-activity` sends the sidebar its wake byte. This is the only thing
  that tells it Claude has printed, so it is the one hook the sidebar
  cannot do without.
- `window-pane-changed` runs `sweep` too, which is what closes a note
  prompt you have clicked away from.
- `pane-exited` runs `sticky-chat sweep`, which closes any sidebar whose
  partner has gone: a tab is the two panes together, so Claude ending ends
  the tab. Note that `#{pane_id}` inside a hook is whatever is active, not
  what died — the dead pane is `#{hook_pane}` — and it is destroyed by the
  time the hook runs, so `cmd_sweep` asks the sidebars which of them lost a
  partner rather than asking the partner about itself. The sidebar's own
  check on the heartbeat stays as the backstop, since it rides along in a
  call the loop makes anyway.

`cmd_fit` resizes every sidebar back to its `@sticky_width`, skipping
zoomed windows, then calls `fit_windows` and `pin_clients`.

`set -as terminal-overrides ",*:RGB"` is not cosmetic: Claude assumes
24-bit colour whenever `$TMUX` is set, and without the override tmux
quantises it to 256.

That override has a cost from tmux 3.8 on, which draws its own chrome — the
selection, popups, borders, the message line, two dozen default styles in
all — in theme colours that resolve to 24-bit values. A terminal that cannot
draw them reads the sequence as a faint attribute and loses the colour, so a
selection goes grey instead of yellow. `set -sq theme terminal` puts that
chrome back on the ANSI palette every terminal has, which is the user's own
besides; `-q` because tmux before 3.8 has no such option and would otherwise
say so on every start.

3.8 also changed what happens when you ask about a target that is not there:
`display-message -p -t %17 '#{pane_id}'` used to fail for a pane that had
gone, and now succeeds and prints nothing. Anything that read only the exit
status therefore answered "still there" for ever - which is why
`Tmux.pane_exists` compares the id that comes back rather than trusting
the status, and why `resolve_project` treats an empty
`#{pane_current_path}` as no answer. `has-session` still fails properly, so `server_running` is fine
as it stands.

`cmd_add --auto` suppresses a note when the selection reaches into the
prompt box (`in_prompt_box`, `PROMPT_ROWS` = 2 rows above the cursor, for
the box's top border and the gap above it), so a drag over your own
half-written message is taken as a plain copy. Both `MouseDragEnd1Pane`
bindings pass it; the keyboard's `N` does not, so the keyboard always makes
a note.

## Why the keys are what they are

The prefix is `C-g` because Claude Code uses `C-b` itself, and a prefix
that Claude also wants would be unusable. This applies only inside the
sticky server.

`s`, `u`, `r`, `c` and `t` are lower case because they are pressed often.
They cost tmux's own `choose-tree`, `refresh-client`, `new-window` and
`clock-mode` on those letters, which is a fair trade inside a server that
only ever runs this one layout; `S`, `U` and `R` stay as shifted aliases
for anyone whose fingers expect them.

`o` for open costs tmux's `select-pane -t :.+`, which cycles the panes of a
window. Cheap here: a sticky window is two panes, and `C-g ;` and the arrow
keys both still move between them - and the mouse does, which is how anyone
in a two-pane layout actually switches.

Two keys go the other way. `C-g F` forks, so that `C-g f` keeps tmux's
find-window, and `C-g L` lists notes, so that `C-g l` keeps last-window.
`F` keeps its case inside the sidebar too, so the key is the same wherever
you press it.

`N` in copy mode costs vi's "previous search match".

The sidebar runs in cbreak with `ISIG` still on, so `C-c` would raise
`SIGINT` and end the pane — far too easy to hit by accident in there. It is
ignored outright, and `q` is the close key.

The status bar sets `bg=default,fg=default` rather than tmux's green, so it
borrows the terminal's own colours and stays readable in a light or a dark
scheme; key names are told apart by weight instead. tmux 3.6 and later can
tell whether the terminal is light or dark (`#{client_theme}`, plus the
`client-light-theme` and `client-dark-theme` hooks), so a conditional style
is possible in `user.conf` there.

## A window taller than the terminal

Claude's default renderer repaints the bottom region of its frame in place.
When that region is taller than the terminal, the cursor-up cannot reach
above row 0, so the repaint runs off the top and eats what was printed
before — which is why a long table appears to swallow the lines above it.
It cannot be fixed from outside, because a VT cursor only addresses visible
rows. More rows is the mitigation.

So a window can be made taller than the terminal — `DEFAULT_VIRTUAL_ROWS`
(200) when the height is not spelled out: `window-size manual`, then
`resize-window -y` to the larger of the requested height and the client's
area. tmux pans a viewport over it, Claude's pty reports the taller size,
and its repaint has room. The
requested height is remembered as `@sticky_virtual_rows`, and the status
line shows `[+N rows]` through `#{?window_bigger,…}`.

`prime_rows` prepends an `awk` loop printing that many blank lines before
`exec`ing Claude, because a pane draws from its top row down: without it
the first screenful would sit above the viewport and you would be looking
at empty rows.

`fit_windows` re-applies the height on every `fit`. It is a floor rather
than a fixed size — a window shorter than the client is drawn in the corner
with dead space around it — so the height used is
`max(@sticky_virtual_rows, client_area)`.

`pin_clients` holds the viewport at the bottom. Left alone, tmux pans to
keep the active pane's cursor in view and drops to the top of the window
whenever that cursor is hidden, which is exactly what Claude does while it
repaints, so the text jumps away under you. `refresh-client -D 1000` pushes
the viewport to the bottom and keeps it there. Copy mode is the one time
the cursor is worth following, because that is you moving it, so for a
client in copy mode on a Claude pane the pin comes off with
`refresh-client -c`, which returns to tracking the cursor.

`window-size manual` freezes the width as well as the height, so
`fit_windows` holds both against the client; without that a window keeps
whatever width it was born with and the terminal draws dead space beside it.

Only a window bigger than its client is panned, which is why an overlay
misbehaved here and nowhere else: opening one clears the one before it, and
`refresh-client` redraws the client underneath. There used to be a freeze
for that — `holding()`, `frozen()` and an `@sticky_freeze` pane option —
and it is gone. The prompt is a floating pane now rather than a popup, so
there is no overlay to lose: see *Where a prompt lives*. If a
`--virtual-rows` tab ever paints over one, the history is in the commits,
not in the source.

None of this happens unless it is asked for. `default_virtual_rows()`
returns 0 unless `STICKY_VIRTUAL_ROWS` says otherwise, so a tab is tall
only with `--virtual-rows N` or the `C-g C` binding that passes it — a
panned window still costs a pane that paints over popups and a viewport
that moves under a repaint, and that trade is the user's to make, per tab.
A fork inherits the source tab's height, or its notes would sit at a
different offset from the ones they were copied from; so does a tab that
`resume` puts back, from the `virtual_rows` in its record.

## The clipboard

Inside tmux the mouse belongs to tmux, so a drag makes a *tmux* selection:
the highlight is drawn by tmux into the pane, and the terminal has no
selection of its own for its copy shortcut to act on.

tmux's own bridge is OSC 52, an escape sequence asking the terminal to set
the system clipboard, governed by `set-clipboard` (default `external`) and
gated on an `Ms` terminfo entry for the outer terminal. iTerm2, Ghostty,
kitty and WezTerm honour the sequence; macOS Terminal.app does not. Test a
terminal with:

```sh
printf 'hello-osc52' | base64 | { read -r b; printf '\033]52;c;%s\a' "$b"; }
pbpaste        # or wl-paste / xclip -o
```

If the clipboard changed, `set -g set-clipboard on` in `~/.sticky/user.conf`
also lets applications inside tmux set tmux's buffers. If the escape
sequence printed as visible text, the terminal ignores it.

sticky-chat does not depend on any of that. It sets no `set-clipboard`
value, and the two paths it does control go through a local helper instead:
`clipboard_command` picks `pbcopy` on macOS, `wl-copy` when
`WAYLAND_DISPLAY` is set, then `xclip`, then `xsel`. That command is baked
into the config as `@CLIP@` for the `y` binding, and called directly by
`to_clipboard` for the popup's `Ctrl-C`. With none of them installed
`@CLIP@` becomes `true` and `y` fills only tmux's paste buffer.

Terminals also let you hold a modifier while dragging to bypass mouse
reporting and make the terminal's own selection, which its copy shortcut
can then act on. iTerm2 uses Option; Terminal.app has no reliable
equivalent.

Trying another terminal costs nothing: the tmux server is a daemon, so
running `sticky-chat` from the new one attaches to the session that is
already there — same Claude, same notes, nothing restarted.

## Claude Code's two renderers

`/tui default` is the classic main-screen renderer. Finished messages are
printed once into normal scrollback; the streaming message and the prompt
box are a dynamic region repainted in place. This is the one sticky-chat
is built for: tmux's history then holds exactly what the terminal's
scrollback would.

`/tui fullscreen` keeps its transcript in its own alternate screen with
virtualised scrollback. `capture-pane` without `-a` reads the live screen,
so the matching loop works there unchanged, but the sidebar can only see
what is on screen and absolute line numbers mean nothing. Notes still work;
they are still listed when out of Claude's own viewport.

## Which agent is in the pane

Nothing in the placing, the sidebar or the store cares which agent is
running - but four things do, and they all live in `agents.py` as one frozen
`Agent` record per agent:

| field | what it settles |
|---|---|
| `command`, `label`, `short`, `exit_hint` | what to run, and how the sidebar names it |
| `no_menu_args` | arguments added unless `--menus`; Claude's `--disallowedTools AskUserQuestion` |
| `env` | what the pane is told about the tmux around it |
| `session_flag`, `resume_flags`, `continue_flags`, `fork_flags` | how a tab is given a conversation, asked for it again, and branched |
| `words_first` | where those words go on the line: last, or straight after the program name |
| `transcript_glob`, `session_keys` | where the agent writes down what it called the conversation, for the ids it never lets us choose |
| `glyph_landmark`, `paste_placeholder`, `prompt_rows`, `prompt_box_rows` | the shape of the output the placer reads |
| `tested` | whether anybody has actually run it |

The four session fields are separate because each is used on its own, and
`can_resume`, `can_fork` and `names_sessions` are read off them. An agent
with none of them still works - notes are placed, sent and drawn the same way
- it just cannot promise a tab comes back as the same conversation, so a
feature that needs one **declines rather than guesses**: `C-g F` on an agent
with no branch command says so and opens nothing, and `resume` puts the tab
and its notes back on a fresh conversation and prints which of the two you
are getting. Nothing ever writes a flag the profile does not have.

`can_fork` asks only whether the conversation can be branched. Naming what
the branch made is a separate question: an agent that picks its own id gets a
fork all the same, remembered under a `tab-` key of ours with the carried
notes under it and `continued` set in the record, which is exactly how a tab
started with a bare `--continue` is handled. What is remembered for such a
fork is the tab *without* the branch words, because `resume` re-runs what it
finds and `codex fork --last` at every restart would cut a fresh copy each
time; Claude's line is kept whole, since `resume_command` drops the branch
words from it and asks for the id instead.

`words_first` is where the session words go. Claude spells them as flags and
does not mind where they sit, so they go last, after whatever else the tab
was started with; codex spells the same jobs as subcommands (`codex resume
--last`, `codex fork --last`), which stop being subcommands the moment
anything else is in front of them. `with_words` in `store.py` is the one
place that settles it, and all three composers - `cmd_start`'s session flag,
`cmd_fork`, `resume_command` - go through it, so claude's lines come out
byte for byte as before.

`--agent NAME` on `start` and `new` picks one, `claude` is the default and is
exactly what the program did before profiles existed, and the choice is
recorded on the pane as `@sticky_agent` and in the window record, so fork,
resume and the sidebar read back the profile the tab was started on. A tab
opened from a running one inherits it. The command to run is `--agent-cmd`,
one spelling only - `--claude-cmd` is gone, and the pane option is
`@sticky_agent_cmd` - because the line may not be starting Claude at all.
`placement.py` keeps `GLYPH_LANDMARK`, `PROMPT_ROWS`, `PROMPT_BOX_ROWS` and
`PASTE_PLACEHOLDER` as module names - they are the default profile's values -
and `find_landmark` and `in_prompt_box` take a profile when the caller has
one.

`sticky <agent> [dir]` is the short spelling: when the first word is a known
agent name **and is not a path that exists**, it is read as `start --agent
<name>`. The path is looked at first, so a directory really called `claude`
goes on meaning what it meant before. Everything else on the line is
unchanged, including `--` for the agent's own arguments.

Four profiles today. `claude` is the default. `generic` is the honest
fallback for anything that prints a transcript: you name the command with
`--agent-cmd`, and it claims no session flags, no environment, no default
arguments and no paste folding - a second paste into an agent that never
folded would send the block twice - while keeping the conservative output
heuristics, which are a guess about a terminal frame rather than about
Claude. **`gemini` and `agy` were tried end to end and pass; `codex` is
marked `tested=False`**, which `sticky-chat` says when you select it and in
`--help`, with the `caveat` field carrying what exactly is unproven.

Gemini CLI 0.46.0, 2026-09-08: the tab started, the transcript was on disk
before the first prompt was answered, `discover_session` had the id within
seconds, `commit` put the note in Gemini's own input box, and after a
`kill-server` `resume` relaunched it as `gemini --resume <uuid>` and the
earlier exchange was back on screen. Resuming writes a *second* transcript
file carrying the same id, which is why discovery keys on the id in the
header rather than on one file per conversation. Its answers are marked
`✦` and its finished tools `✓`, so the profile carries a glyph set of its
own; the info banners are deliberately not in it.

Antigravity CLI 1.1.27, 2026-09-09, the same five steps and the same
result - it updated itself to 1.1.28 between the first tab and the resume
and carried on regardless. Where it writes a conversation is the one
surprise: `~/.gemini/antigravity-cli/conversation_summaries.db` has an id
column *and* a workspace column, which would have made it the best source
of the three, but on a Google AI Pro account it stays empty - the summaries
live server-side. What is on disk is one sqlite file per conversation,
`conversations/<id>.db`, so the id is the file's name and nothing inside is
ever opened. It marks a tool `●` and a thought `▸`. There is no fork
flag, though `parent_conversation_id` in that empty table says it can, so
one may yet appear.

Both of them can reopen the conversation this project was last having -
`gemini --resume` with nothing after it, `codex resume --last`, whose
listing the documented `--all` flag ("beyond current working directory
filter") says is directory-filtered by default - and that is `continue_flags`.
Both can also be asked for one conversation by id (`gemini --resume <uuid>`,
`codex resume <uuid|name>`), which is `resume_flags`, and neither can be
*told* an id when a session starts - so the id is theirs to choose and sticky
has to learn it afterwards. That is what the next section is about. Only
codex documents a branch command, so only codex forks.

## Learning the id an agent chose

Claude Code takes `--session-id <uuid>`, so sticky picks the id, and knows it
before the pane exists. Gemini and codex have id-based resume but no way to
be handed an id at the start: the id is theirs to choose, and until something
reads it back, a tab on either resumed as a fresh conversation wearing the
old tab's notes.

All three write the conversation as a JSONL file whose **first line is a
header carrying the id**, which is the whole mechanism. One rule per profile:
the newest file matching `transcript_glob` under `$HOME` that was modified
after this tab launched, and the id in that file's first line - tried as
`session_keys` (`sessionId`, `session_id`, `id`, plus one level into a
wrapper), and failing that as the first UUID-shaped run in the file name,
which is how Claude Code names its files. An empty glob means the profile
cannot be asked, and nothing goes looking.

**The first line, never the file.** A Gemini transcript on the machine this
was written on had reached 1.6 GB. `json.load` on that is not a slow sidebar,
it is a dead one - so `session_id_of` opens the file, reads one line with a
64 KB cap (a file with no newline in it at all must not arrive in memory
whole) and closes it. A test writes invalid JSON as the second line, so
anything that read the file whole would fail it.

The launch time is what keeps yesterday out. The glob has no idea which
project it is looking at - gemini's directories are named for the project on
this machine though its documentation says a hash, and codex's are not - so
"the newest one" is only the right answer because this tab is what has just
been writing. `mark_launch` records the moment on the agent's pane as
`@sticky_launched`, taken before the pane is created and only for a profile
that has something to learn; a file older than that is somebody else's
conversation and is never adopted.

**Where it runs.** In the sidebar, because it is the only thing still alive
after the tab opens and it already knows the pane. Three gates, all read once
at startup: the profile has a glob and does not name its own sessions
(`can_discover`, which keeps every Claude tab out of this path entirely),
`@sticky_agent_session` is not already set, and the tab is younger than
`DISCOVER_WINDOW` (60 s). Then at most one glob and one first-line read every
`DISCOVER_EVERY` (5 s), after the frame is on screen rather than before it,
until the id is found or the tab is past that same minute. While it is
hunting, the interval also caps the `select` timeout - nothing in tmux can
say "the agent has written its transcript now", so this is the one thing in
the loop genuinely on a clock, and it is on one for at most a minute.
Afterwards `hunting` is False, the timeout goes back to `SIDEBAR_IDLE`, and
the whole mechanism costs one comparison a pass. Measured with a stand-in
agent that never writes anything: 0.12 s of CPU over the first minute, and
not a measurable tick more in the thirty seconds after it gave up. An agent
that never writes a transcript is a normal outcome, not an error, and
nothing is printed about it.

**What the id is for, and what it is not.** It is recorded as
`agent_session`, a field of its own in the window record, and it reopens the
conversation. It is *not* the store key: the notes were written to the
`tab-<hex>` directory, keyed as they are, and moving the store would strand
every note already on screen. The two ids stay apart in `resume_command` for
the same reason - `session` is the key the notes are filed under, and only
`agent_session` is a name any agent has ever heard of.

Codex and antigravity keep no transcripts as files, which is the whole reason
`state_db_glob` exists beside `transcript_glob`. Every guide describes codex
JSONL rollouts under `~/.codex/sessions/`; 0.153.4 installed on 2026-09-08 has
no such directory, and keeps sqlite instead - `~/.codex/state_<n>.sqlite`,
whose `threads` table has `id`, `created_at`, `created_at_ms` and `cwd`, with
`rollout_migration_state` beside it as the receipt for the change. Antigravity
keeps the same *shape* under different words:
`~/.gemini/antigravity-cli/conversation_summaries.db`, table
`conversation_summaries`, with `conversation_id`, `last_modified_time` and
`workspace_uris` among its twenty columns, read off the live schema on this
machine rather than off any documentation. Either row is a better answer than
any file was: it carries the project, so the conversation is matched to *here*
instead of to whichever file was touched last.

So the words are on the profile - `state_db_table`, `state_db_id_column`,
`state_db_time_columns`, `state_db_project_column`, beside the glob - and
`conversation_in_db` writes the one sentence they go into:

```
select <id>, <times...> from <table>
where <project> like '%' || ? || '%'
order by <times... desc> limit 1
```

which for codex is `select id, created_at, created_at_ms from threads where
cwd like … order by created_at desc, created_at_ms desc limit 1`, exactly
what it asked before, and for antigravity:

```
select conversation_id, last_modified_time from conversation_summaries
where workspace_uris like '%' || ? || '%'
order by last_modified_time desc limit 1
```

A profile naming a glob but not all four of the rest is never asked at all.

**Two formats are unknown, and are allowed for rather than guessed.** The
antigravity table has zero rows on the machine this was written on - nobody
has held a conversation in it yet - so neither has ever been seen written.
`workspace_uris` is plural and may hold a bare path, a `file://` URI or a
JSON list of either, so the project is matched with `like '%' || ? || '%'`
against its realpath, which reads all three alike; a realpath is long and
specific enough that a substring is not loose in practice, and a row
belonging to another project still cannot match one - which is its own test.
`last_modified_time` is declared `datetime`, which sqlite does not enforce,
so it can arrive as an integer, a float or an ISO-8601 string; `unix_time`
reads all of those - and codex's seconds and milliseconds, whose column names
do not settle the scale - and answers 0.0 for anything it cannot read, which
the caller takes as a row with no time on it rather than as a row too old to
want. That last part is a deliberate trade: a row for this exact project,
looked at within a minute of the tab starting, is that tab's conversation,
and refusing it would lose the feature outright over a column nobody has yet
seen written.

The database belongs to another program, so `conversation_in_db` opens it
read-only with a short timeout and treats a missing table, a locked database
and a moved column alike - the profile learns nothing, and the tab comes back
on `resume --last` or `--continue` as it did before. Codex's flags were read
back off `codex resume --help` on the same version and match what the profile
carries.

## Remembering tabs

`~/.sticky/windows/<session-id>.json` holds one record per tab: the project,
the command line it was launched with, the note store, the sidebar width,
the virtual-row height, and when it was last seen. One file per tab means no
central list to lock and no ordering problem between tabs; a tab that never
returns simply leaves its file for `resume` to offer or forget.

The session id is the key, and sticky chooses it where it can: `cmd_start`
adds `--session-id <uuid>` unless the command line already names a session
(`--resume`, `--continue`, `-c`, `-r`, `--session-id`) - or the profile has
no such flag, which reads here exactly as a bare `--continue` does, and the
key becomes a `tab-` name of ours. Claude honours a supplied id for a fresh
session and alongside `--continue --fork-session`, so a fork gets an id of
its own by the same route rather than needing to be discovered afterwards; a
fork on an agent that names its own conversations gets a `tab-` key instead.
`resume_command` turns a stored line back into a resuming one: it drops
`--session-id`, `--continue` and `--fork-session` - the fork already happened
- and adds `--resume <id>`.

Which id, and what happens without one, is the whole of the rest of it.
`agent_session` - the id learned from the transcript - wins where there is
one; otherwise the key, unless it is a `tab-` name of ours, which no agent
has heard of. With no id at all the line gets `continue_flags` instead, so a
codex tab comes back on `codex resume --last` rather than on a bare `codex`,
which would be a new conversation wearing the old notes. A line that already
names a conversation is left exactly as it is, and an agent with neither kind
of flag is handed nothing. So, for the same tab, discovered and not:

| profile | id learned | id never learned |
|---|---|---|
| `claude` | `claude --resume <uuid>` (never discovered: it was ours) | `claude --continue`, as it was started |
| `gemini` | `gemini --resume <uuid>` | `gemini --resume` |
| `codex` | `codex resume <uuid> -m gpt` | `codex resume --last -m gpt` |

`cmd_resume` says "the conversation starts fresh" only when that is what will
happen - when the line it is about to run names no conversation at all, which
is now `generic` and nothing else.

The sidebar refreshes its own record once a minute, so `resume` can order
tabs by when they were last really in use rather than when they were made.

`open_tab` waits briefly after creating the window: a session id Claude will
not accept dies about then, and the empty result sends `reopen_tab` down the
fallback path, which reopens the same project on a new conversation. That
function is where a record becomes a window: `resume_command` for the line,
`open_tab` for the window, and the retry without the id when the
conversation has gone. Both ways in go through it - `resume` from a shell
and `C-g o` from inside the session - so the two cannot come to disagree
about what reopening a tab means.

## Picking one tab to reopen

`resume` is every record in turn, `[y/n/a/q]`, in a terminal. `C-g o` is the
other half of the same question: the tab you want *now*, from inside the
session, without a shell to run anything in. It opens the same floating pane
`C-g c` does, running `sticky-chat reopen`, which prints the remembered tabs
newest first and reads a line with `prompt_line`:

```
  1  sticky-tmux-for-claude  claude  just now  same conversation
  2  notes-app               codex     3d ago  its last conversation
  3  scratch                 gemini    5h ago  same conversation
  4  aider-thing             generic   20 Feb  a new conversation
```

Four columns because there are four things you choose by: which project,
which agent, how long since it was last open, and what reopening it actually
brings back. That last one is read off the line `resume_command` produces
rather than worked out again — an id on it means that very conversation,
`--continue` or `codex resume --last` means the last one in that project,
and a line saying nothing about conversations means the tab and its notes
around a fresh one. A row cannot promise what the command will not do.

Enter with nothing typed takes the first, since the tab you want back is
nearly always the one that went away last. A number picks that row. A number
that is not a row is refused with what you typed still on the line to
correct, exactly as a path that does not exist is at the new-tab prompt —
the alternative is a pane that closes having opened nothing, and no way to
tell which of the two happened. Esc closes it and takes nothing with it, and
so does clicking away, which is the sweep sending SIGHUP.

Only what fits in the pane is listed, and only what is listed can be picked:
choosing by a number you cannot see is not choosing, and `resume` still
offers the whole list.

**A tab that is already open is not offered.** Reopening one would put a
second tab on a conversation that is still running, which the agent either
refuses or - worse - takes. `last_seen` cannot answer that question, because
the sidebar of a running tab is exactly what keeps its record warm, so
`tabs_showing` asks the panes instead: the agent's pane carries
`@sticky_session` and the sidebar beside it `@sticky_store`, and a record
matching either is a tab you are already looking at. A record whose project
directory has gone is left out too, since `open_tab` would refuse it.

## Quitting

`cmd_quit` saves nothing new: records are written as tabs open and refreshed
by their sidebars while they run. What it adds is the shape of the moment -
`open_at_quit` is set on the records of tabs that were open and cleared on
the rest - so `resume --last` can offer that set back rather than everything
ever opened. Then it kills the server. An agent mid-answer loses that turn,
since only completed exchanges reach the transcript. The question it asks
first names the agents actually in the tabs, read off `@sticky_agent`, so
"stop Claude?" is not asked beside a codex tab.

`--last` implies taking them all: having just asked for the set you closed,
being asked about each one again would be a poor joke.

## Further reading

`PLAN.md` is the design. `NOTES.md` is the research behind it, including
what was checked in the tmux and iTerm2 sources and in the Claude Code
binary.
