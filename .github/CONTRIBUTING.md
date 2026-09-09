# Contributing

sticky-chat is a small project: one program, no runtime dependencies, and
a test suite that drives a real tmux server. Bug reports, patches and ideas
are all welcome. What follows is how to get set up and what makes a change
easy to accept.

## What you need

- **Python 3.9 or newer.**
- **tmux 3.7 or newer** for the integration tests, since the note prompt is
  a floating pane. Check with `tmux -V`; `brew install tmux` on macOS,
  `apt install tmux` or `dnf install tmux` on Linux — note that the
  distributions are usually behind (Ubuntu 24.04 ships 3.4), in which case
  the integration tests skip themselves rather than fail.
- **pytest** and **ruff**, which the `dev` extra installs.
- **Claude Code** on your `PATH` as `claude` if you want to run the thing
  by hand. The tests do not need it.

## Getting set up

```sh
git clone https://github.com/hrhodin/sticky-chat
cd sticky-chat
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"   # the sticky-chat console script, plus pytest and ruff
```

The editable install is a convenience, not a requirement: the program is
standard library only and runs straight from a checkout. `README.md`
describes the symlink install for day-to-day use.

## Running the tests

```sh
pytest                        # everything
pytest -m "not integration"   # the fast pure-function tests only
pytest -m integration         # the ones that drive a real tmux server
```

The integration tests start and kill their own tmux server on a socket of
their own and point `STICKY_HOME` at a temporary directory, so they leave
your own tmux sessions and your own notes alone. No attached terminal is
needed. They are the slow half, so `-m "not integration"` is the one worth
running on every edit; run the whole suite before you open a pull request.

Without tmux installed the integration tests skip rather than fail, so if
the slow half quietly vanishes, check `tmux -V`.

## Lint

```sh
ruff check .
```

Configured in `pyproject.toml`; please do not add per-file ignores to work
around a finding without saying why in the pull request.

There is deliberately no `ruff format`. The source is hand-wrapped to 79
columns, with trailing comments aligned into a second column so that a
comment spanning several lines reads as one block. The formatter flattens
those into left-aligned comments that appear to belong to the following
line, which is worse than the inconsistency it removes. Match the
surrounding style by hand.

## Working on the program

`C-g r`, or `sticky-chat reload`, rewrites the generated tmux config and
restarts every sidebar with the code as it now stands, without touching the
Claude panes. So the loop is edit, `C-g r`, look, and it is safe in the
middle of a conversation. Only changes to `cmd_start` itself need a new
tab.

`docs/DEV.md` covers the internals worth reading before changing them: how a
note stays attached to its text as output moves, what goes into the
generated tmux config and the pane options that carry the wiring, the
sidebar's redraw loop, and why the window is taller than the terminal.

## What a change should look like

- **A test for anything behavioural.** If it changes what the program does,
  it needs a test. Prefer a fast test against the pure functions where the
  behaviour can be reached that way; reach for an integration test when it
  genuinely needs tmux.
- **No new runtime dependencies.** The program is standard library only and
  that is deliberate: it means it can be dropped onto any machine with
  Python and tmux and run, with no install step and nothing to keep up to
  date. Test-only and development tools are a different matter.
- **Python 3.9 still has to work.** No syntax or standard library that
  arrived later.
- **Comments explain why, not what.** The existing comments record the
  reasoning that is not visible in the code — why a pin is dropped in copy
  mode, why an approximate match is refused far from its anchor. Keep to
  that; do not narrate the lines below.
- **Docs where they belong.** `README.md` is for people using the tool,
  `docs/DEV.md` for how it works inside. A new flag or key belongs in both, if
  it is user-visible.
- **One change per pull request.** A refactor mixed into a fix is hard to
  review and harder to revert.

## Before you open a pull request

Run `pytest` and `ruff check .` locally, and say in the pull request what
you ran and what you checked by hand in tmux, if anything. Behaviour in a
real terminal is often the only way to see that something is right.

## Reporting bugs

Use the issue forms. The bug form asks for `tmux -V`, your Python version,
your terminal emulator and the output of a `list-panes` command; that last
one has diagnosed several real bugs on its own, so please paste it in even
when it looks irrelevant.
