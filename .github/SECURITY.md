# Security

## Reporting a vulnerability

Please report anything you believe to be a security problem privately,
rather than in a public issue.

Use GitHub's private vulnerability reporting: the **Security** tab of
<https://github.com/hrhodin/sticky-chat>, then **Report a vulnerability**.
That opens a private advisory visible only to you and the maintainer.

Please include what you did, what happened, and the versions from
`tmux -V` and `python3 -V`. A proof of concept helps, even a rough one.

This is a small project maintained in spare time, so there is no formal
response time. You will get an acknowledgement as soon as the report is
seen, and an honest answer about whether and when it will be fixed.

Only the current state of `main` is supported. Fixes are not back-ported.

## What the tool does, and what that means

sticky-chat is a local developer tool. It has no network service, sends
no telemetry, and talks to nothing over the network itself. It does,
however, do several things worth knowing about before you run it.

**It generates a tmux config and sources it.** `~/.sticky/tmux.conf` is
rewritten from a template on every `start` and every `reload`, so anything
you put in that file is lost. It binds keys and hooks that run
sticky-chat itself. Your own settings belong in `~/.sticky/user.conf`,
which the generated config sources last — meaning tmux executes whatever
is in that file, with your privileges, exactly as any tmux config would.

**It spawns processes.** A tmux server on its own socket (`tmux -L sticky`
by default, `STICKY_SOCKET` to change it); Claude Code, as `claude` or as
whatever `--claude-cmd` names; a clipboard helper found on the machine
(`pbcopy`, `wl-copy`, `xclip` or `xsel`); and an `awk` loop that pads the
pane before Claude starts. `--claude-cmd` runs what you point it at, so
treat it as you would any other command you type.

**A fork inherits the original tab's flags.** `sticky-chat fork`, `C-g F`
and `F` in the sidebar start the new tab from the command line the source
tab was launched with, plus `--continue --fork-session`. If the original
was started with `--dangerously-skip-permissions`, so is the fork. That is
deliberate — a fork that behaved differently from the tab it came from
would be worse — but it does mean a permission decision made once carries
into tabs you did not type that flag into. sticky-chat neither adds nor
removes such flags on its own.

**Notes are plain files.** They hold the text captured from the pane, so
whatever Claude printed can end up on disk: keys, tokens, contents of
files. They are written as JSON under `~/.sticky/<project>-<hash>/`, or in
`<project>/.sticky/` with `--local`, with the ordinary permissions your
umask gives, and they are not encrypted. `--local` puts them inside the
project, where a careless `git add` could commit them. Deleting `~/.sticky`
removes the lot.

**The tmux server is a tmux server.** Anyone who can already run commands
as your user can attach to it, read the panes and type into the agents cli, e.g. Claude Code. That is
true of every tmux session; sticky-chat does not weaken it and does not
strengthen it.

**Notes leave your machine when you send them.** `C-g s` pastes the quoted
text and your note into Claude's prompt. From there it is Claude Code's
business, under Claude Code's terms.

Out of scope for this project: vulnerabilities in tmux, in Claude Code, or
in your terminal emulator. Report those to the projects concerned. What is
in scope is sticky-chat mishandling any of them.
