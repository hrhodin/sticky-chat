"""Which agent is in the pane, and everything that depends on knowing.

Sticky-chat is written against Claude Code and none of the placing, the
sidebar or the store cares which agent is in the pane - but five things do:
the command to run, the flags that give a tab a conversation of its own, the
environment the pane is handed, the shape of the output the placer reads, and
where the agent writes down what it called the conversation. Those five live
here, as one record per agent, so that supporting another one is a row in
`AGENTS` rather than a branch in six files.

Four profiles. `claude` is the default and is exactly what the program did
before this file existed. `generic` is the honest fallback for anything that
prints a transcript: it runs the command you name and claims nothing else, so
the features that need a conversation id say so rather than build a command
line the agent would refuse. `gemini` and `codex` carry what their own
documentation says, plus - for gemini - what a transcript on this machine
actually looked like, and both are marked untested, because nobody has run
sticky-chat on them.

Nothing here talks to tmux beyond borrowing the session name for an
environment variable, so it sits above the store and the placer and can be
imported by either.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass

from .tmux import SESSION_NAME
from .util import die

# ----------------------------------------------------------------- defaults

# Lines that make good landmarks: an agent's tool headers and prompt markers.
# Permissive on purpose - it is a first guess, and `find_landmark` falls back
# to the nearest line hard against column zero when none of these match - so
# the same regex serves an agent nobody has looked at yet.
GLYPH_LANDMARK = re.compile(r"^\s*[>●⏺✦✻*]\s+\S")

# Claude folds a paste of more than three lines into a placeholder and says
# to paste again to expand it. Detected rather than guessed, so it keeps
# working if the threshold moves; an agent with no such folding leaves this
# empty and is never pasted into twice.
CLAUDE_PASTE = re.compile(r"\[Pasted text #\d+(?: \+\d+ lines)?\]")

# Rows above the cursor that still belong to the input box - its top border
# and the gap above it - and the rows at the bottom that are the box rather
# than transcript, when looking for a folded paste.
PROMPT_ROWS = 2
PROMPT_BOX_ROWS = 10

PREFIX = "C-g"              # what CONFIG_TEMPLATE binds, told to the agent
                            # so that it leaves that key alone

# ------------------------------------------------- what it calls itself

# Every agent here writes a conversation as a JSONL file whose first line is a
# header, and the header carries the id. Which key it uses is the only
# difference between them, so a profile carries a list of candidates rather
# than a parser of its own.
SESSION_KEYS = ("sessionId", "session_id", "id")

# A conversation id is a UUID in all three, in the header and in the file name
# where the name has one. Insisting on the shape is what makes `id` safe to
# try: a header field of that name holding something else is not something we
# could hand back to the agent, and a guess is worse than nothing here.
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# The most of a first line worth reading. A transcript is one line per turn,
# so this is generous for a header; what it really guards against is a file
# with no newline in it at all, which must not be pulled into memory whole.
HEADER_BYTES = 64 * 1024

# ------------------------------------------------------------------ profile


@dataclass(frozen=True)
class Agent:
    """One agent, as the rest of the program sees it.

    The session fields are the whole of what makes a tab resumable, and they
    are separate because each is used on its own: `session_flag` names a
    conversation we chose, `resume_flags` reopen one by that name,
    `continue_flags` ask for a conversation without naming it, and
    `fork_flags` branch the one that is running. An agent that has none of
    them still works - notes are placed, sent and drawn the same way - it
    just cannot promise that a tab comes back as the same conversation.
    """

    name: str                   # what --agent takes, and what the pane records
    label: str                  # the product, as it is written in prose
    short: str                  # how it is named in a sentence
    command: str                # the program to run; empty means you say
    aliases: tuple[str, ...] = ()  # other names the same agent answers to
    tested: bool = True         # whether anybody has run sticky-chat on it
    caveat: str = ""            # what exactly is unproven, when some of it is
    exit_hint: str = "ending the agent closes the tab"
    no_menu_args: tuple[str, ...] = ()    # dropped by --menus
    env: tuple[tuple[str, str], ...] = ()
    session_flag: str = ""      # gives a new conversation a name we picked
    resume_flags: tuple[str, ...] = ()    # reopen one by that name
    continue_flags: tuple[str, ...] = ()  # ask for one without naming it
    continue_line: tuple[str, ...] = ()   # ...and how to write that, if the
                                          # flags above are alternative
                                          # spellings rather than one line
    fork_flags: tuple[str, ...] = ()      # branch the running one
    # Where the words above go on the line. Claude takes them last, after
    # whatever else the tab was started with; codex puts the same job in a
    # subcommand - `codex resume --last` - which has to sit straight after
    # the program name or it is not a subcommand at all. Named for what it
    # settles rather than for "subcommand", because it moves the whole run
    # of words we add and only the first of them is ever one.
    words_first: bool = False
    # Where the agent writes the transcript of a conversation, as a glob
    # under $HOME, and which keys in that transcript's first line hold the
    # id it chose. This is the only way to learn an id we were not allowed
    # to pick: an empty glob means the profile cannot be asked what it
    # called the conversation, and nothing goes looking.
    transcript_glob: str = ""
    session_keys: tuple[str, ...] = SESSION_KEYS
    # ...or a sqlite state file, where it keeps one - and then the four
    # names needed to ask it. Codex and Antigravity keep the same shape
    # under different words: one row per conversation, carrying the id,
    # when it was last touched, and the project it belongs to. So the
    # profile spells the words and `conversation_in_db` writes the one
    # sentence they go into; a profile missing any of the four is never
    # asked, exactly as one with no database at all.
    state_db_glob: str = ""
    state_db_table: str = ""
    state_db_id_column: str = ""
    state_db_time_columns: tuple[str, ...] = ()
    state_db_project_column: str = ""
    glyph_landmark: re.Pattern = GLYPH_LANDMARK
    paste_placeholder: re.Pattern | None = None
    prompt_rows: int = PROMPT_ROWS
    prompt_box_rows: int = PROMPT_BOX_ROWS

    @property
    def id_flags(self) -> tuple[str, ...]:
        """Flags whose next word is a conversation id, not another flag."""
        head = (self.session_flag,) if self.session_flag else ()
        return head + self.resume_flags

    @property
    def session_flags(self) -> tuple[str, ...]:
        """Every flag that says which conversation to open.

        A command line carrying one of these has already been told which
        conversation it wants, and sticky adds none of its own.
        """
        return self.id_flags + self.continue_flags

    @property
    def bare_session_flags(self) -> tuple[str, ...]:
        """The ones that take no value, so stripping them takes one word."""
        extra = tuple(f for f in self.fork_flags
                      if f not in self.continue_flags)
        return self.continue_flags + extra

    @property
    def continue_words(self) -> tuple[str, ...]:
        """What to put on a command line to ask for the last conversation.

        `continue_flags` is the recognising set - every spelling that means
        "you have already said which conversation", so a line the user wrote
        is never second-guessed. Writing one is a different question: claude's
        `--continue` and `-c` are the same flag twice and only one belongs on
        a line, while codex's `resume --last` is two words that go together.
        Profiles of the second kind say so; the rest take the first spelling.
        """
        return self.continue_line or self.continue_flags[:1]

    @property
    def names_sessions(self) -> bool:
        """Whether a tab can be given a conversation of its own."""
        return bool(self.session_flag)

    @property
    def can_resume(self) -> bool:
        return bool(self.resume_flags)

    @property
    def can_discover(self) -> bool:
        """Whether the id the agent chose can be read back off its transcript.

        Only interesting where the id was never ours. An agent we hand an id
        to has already told us the answer, so reading a file to be told it
        again is work for nothing - and it keeps the default profile out of
        this code path entirely.
        """
        return (bool(self.transcript_glob or self.state_db_glob)
                and not self.names_sessions)

    @property
    def can_fork(self) -> bool:
        """Whether the conversation can be branched at all.

        Naming what the branch made is a separate question, and not one that
        has to be answered here: a fork whose id the agent keeps to itself is
        remembered under a key of our own, exactly as a tab that was told to
        continue without being told which conversation.
        """
        return bool(self.fork_flags)


# ----------------------------------------------------------------- profiles


CLAUDE = Agent(
    name="claude",
    label="Claude Code",
    short="Claude",
    command="claude",
    exit_hint="/exit in Claude closes the tab",
    # Claude answers in prose instead of opening a question menu, which a
    # sidebar full of notes has no way to answer.
    no_menu_args=("--disallowedTools", "AskUserQuestion"),
    env=(("CLAUDE_CODE_TMUX_SESSION", SESSION_NAME),
         ("CLAUDE_CODE_TMUX_PREFIX", PREFIX)),
    session_flag="--session-id",
    resume_flags=("--resume", "-r"),
    continue_flags=("--continue", "-c"),
    fork_flags=("--continue", "--fork-session"),
    paste_placeholder=CLAUDE_PASTE,
    # No `transcript_glob`, though Claude writes one it would fit
    # (~/.claude/projects/<escaped path>/<uuid>.jsonl, the id in the name).
    # We name the conversation ourselves here, so there is nothing on disk
    # to learn - and an empty glob is the second lock, next to
    # `names_sessions`, keeping the default profile out of the discovery
    # path altogether.
)


GENERIC = Agent(
    name="generic",
    label="your agent",
    short="the agent",
    command="",                 # nothing to default to: --agent-cmd says
    tested=True,                # it promises nothing, and that much is tested
)


# Untested: nobody has run sticky-chat on it. Everything below was read off
# Gemini's own documentation, and everything that was not is left empty.
#
# `--resume` with nothing after it reopens the conversation this project was
# last having - sessions live under ~/.gemini/tmp/<project hash>/chats/, so
# "the last one" means the last one *here* - which is exactly what
# `continue_flags` is for.
#
# `--resume <uuid>` reopens one conversation by name, which is `resume_flags`.
# A new session still cannot be *told* which id to use - that half is as it
# was - so the id is Gemini's to choose and sticky reads it back off the
# transcript instead; `transcript_glob` is that, and until something has read
# it the same `--resume`, bare, is the fallback. The one flag doing both jobs
# is why it appears twice.
#
# The glob was checked on this machine rather than read off the
# documentation, and the two disagree: the directory under tmp/ was a
# readable project name, not the documented hash. So the glob asks about
# neither, and the first line of the newest file settles it -
# {"sessionId": "...", "projectHash": ..., "startTime": ...}.
#
# No branch command is documented, so `fork_flags` is empty and `C-g F`
# declines rather than opening a tab wearing someone else's notes.
# Tried end to end against Gemini CLI 0.46.0 on 2026-09-08: the tab started,
# the transcript was on disk before the first prompt was answered, the id came
# out of it within seconds, a note landed in Gemini's own input box, and
# `--resume <uuid>` brought the exchange back on screen.
GEMINI = Agent(
    name="gemini",
    label="Gemini CLI",
    short="Gemini",
    command="gemini",
    # Gemini answers under `✦` and marks a finished tool with `✓`; the rest
    # of the set is shared, and the fallback to column zero covers what is
    # not here. Its info banners are left out on purpose - an update
    # notice is not a turn, and a landmark on one drags notes onto it.
    glyph_landmark=re.compile(r"^\s*[>●⏺✦✻✓✗✕*]\s+\S"),
    exit_hint="ending Gemini closes the tab",
    resume_flags=("--resume",),
    continue_flags=("--resume",),
    transcript_glob=".gemini/tmp/*/chats/*.jsonl",
)


# Untested, and read off Codex's own documentation the same way. Codex puts
# both jobs in subcommands - `codex resume` and `codex fork` - hence
# `words_first`; bare, each opens a picker of recent sessions, and `--last`
# takes the most recent without asking.
#
# `--last` rather than the picker because sticky is reopening one particular
# tab: the user answered "which conversation" by pressing the key, and a
# picker would ask it again. Which sessions `--last` is choosing from is
# inferred rather than seen: `codex resume` documents an `--all` flag as
# showing sessions "beyond current working directory filter", so the default
# listing is filtered to the directory and `--last` is very likely this
# project's most recent conversation, the same shape as gemini's bare
# `--resume`. Inferred from that flag, not from running codex - like the rest
# of this profile.
#
# `codex resume <id>` takes a session UUID or a session name, which is
# `resume_flags` - the subcommand alone, with the id put after it by
# `with_words`. A new codex session still cannot be told its id, so as with
# gemini the id is codex's to choose and is read back off the transcript
# afterwards; a tab whose id nothing ever learned falls back to `resume
# --last`, and a fork is remembered under a key of sticky's own either way.
#
# The glob is the least verified thing here: codex is not installed on this
# machine, and all that is documented is that sessions are JSONL under
# ~/.codex/sessions/. So it is deliberately permissive - `**` matches the
# date-shaped subdirectories it is said to use and a flat directory equally -
# and both ways of finding the id are left open, the header keys and a UUID
# in the file name. If codex turns out to write something else entirely, this
# profile learns nothing and behaves exactly as it did before: the tab comes
# back on `resume --last`.
CODEX = Agent(
    name="codex",
    label="Codex CLI",
    short="Codex",
    command="codex",
    tested=False,
    exit_hint="ending Codex closes the tab",
    resume_flags=("resume",),
    continue_flags=("resume", "--last"),
    continue_line=("resume", "--last"),   # two words, not two spellings
    fork_flags=("fork", "--last"),
    words_first=True,
    state_db_glob=".codex/state_*.sqlite",
    state_db_table="threads",
    state_db_id_column="id",
    state_db_time_columns=("created_at", "created_at_ms"),
    state_db_project_column="cwd",
    caveat=("its flags were read back off `codex resume --help` on Codex CLI "
            "0.153.4 and match, and finding a conversation is proven against "
            "a real state database - but no tab has gone through sticky end "
            "to end: no note into its prompt, and no resume on the id"),
)


# Google's replacement for Gemini CLI, and where individual Google accounts
# are being sent: homebrew's `gemini-cli` is deprecated and disabled from
# 2026-12-18 in favour of this. The flags are read off `agy --help` on 1.1.27
# and are the friendliest of the four - `--conversation <id>` reopens one by
# name and `--continue` takes the last - but there is still no way to *give*
# a conversation an id at the start, so the id is Antigravity's to choose and
# is read back afterwards.
#
# It keeps no transcripts as files: like codex, it keeps sqlite, at
# ~/.gemini/antigravity-cli/conversation_summaries.db, whose
# `conversation_summaries` table is one row per conversation with
# `conversation_id`, `last_modified_time` and `workspace_uris` among its
# twenty columns. Those names are read off the live schema on this machine
# rather than off any documentation, so they are facts - but the table has
# zero rows, because nobody has held a conversation in it yet, and the two
# formats that matter are therefore unknown. Both are allowed for rather
# than guessed: `workspace_uris` is matched as a substring, so a bare path,
# a `file://` URI and a JSON list of either all work, and
# `last_modified_time`, declared `datetime` and so stored as whatever wrote
# it, goes through `unix_time`, which reads an integer, a float or an
# ISO-8601 string and says so when it can read none of them.
# Google's replacement for Gemini CLI, and where individual Google accounts
# are being sent: homebrew's `gemini-cli` is deprecated and disabled from
# 2026-12-18 in favour of this. Tried end to end on 1.1.27 (which updated
# itself to 1.1.28 mid-test and carried on): the tab started, the id was read
# off the file name within seconds, a note landed in its input box, and
# `--conversation <id>` brought the whole exchange back on screen. There is
# still no way to *give* a conversation an id at the start, so the id has to
# be learned, and no fork - though `parent_conversation_id` in its database
# says it can, so a flag may yet appear.
ANTIGRAVITY = Agent(
    name="agy",
    label="Antigravity CLI",
    short="Antigravity",
    command="agy",
    aliases=("antigravity",),   # what the cask and the product are called
    exit_hint="/exit in Antigravity closes the tab",
    resume_flags=("--conversation",),
    continue_flags=("--continue", "-c"),
    # Antigravity marks a tool call with the same `●` Claude does and a
    # thought with `▸`; the rest of the set is shared.
    glyph_landmark=re.compile(r"^\s*[>●⏺✦✻▸*]\s+\S"),
    # One sqlite file per conversation, named for its id - so the name is the
    # answer and nothing inside has to be opened. There is a
    # `conversation_summaries.db` beside it with an id and a workspace column,
    # which would have been the better source because it knows the project,
    # but on a Google AI Pro account it stays empty: the summaries live
    # server-side. The file appears as the conversation starts.
    transcript_glob=".gemini/antigravity-cli/conversations/*.db",
)


AGENTS = {agent.name: agent
          for agent in (CLAUDE, GENERIC, GEMINI, CODEX, ANTIGRAVITY)}

DEFAULT_AGENT = CLAUDE.name


def known_name(name: str) -> str:
    """The registry name a word asks for, alias or not. Empty if it is not one.

    Every profile is named after the command it runs, because that is the
    word already in the user's fingers - so `agy`, not `antigravity`, even
    though the product and the homebrew cask are called the second. The
    product name is kept as an alias rather than dropped: somebody who
    installed Antigravity will type Antigravity.
    """
    word = (name or "").strip().lower()
    if word in AGENTS:
        return word
    for agent in AGENTS.values():
        if word in agent.aliases:
            return agent.name
    return ""


def agent_named(name: str | None) -> Agent:
    """The profile a name asks for; the default when it asks for nothing.

    Forgiving on purpose. This is what reads a name back off a pane option or
    a window record, which some other version of sticky-chat may have
    written, and a sidebar that refused to draw because it did not recognise
    a name is a worse answer than drawing it as the default.
    """
    return AGENTS.get(known_name(name or ""), AGENTS[DEFAULT_AGENT])


def require_agent(name: str | None) -> Agent:
    """The profile the user asked for by hand, or a clear death.

    Typed rather than read back, so a name that is not there is a mistake
    worth stopping for: quietly starting Claude because "gemeni" was
    misspelt is the one outcome nobody wants.
    """
    if not name:
        return AGENTS[DEFAULT_AGENT]
    found = AGENTS.get(known_name(name))
    if found is None:
        die(f"no such agent: {name}. Known: {', '.join(AGENTS)}")
    return found


def agent_choices() -> str:
    """The names for --help, so the flag cannot drift from the registry."""
    return ", ".join(f"{name} (default)" if name == DEFAULT_AGENT
                     else f"{name} (untested)" if not agent.tested
                     else name
                     for name, agent in AGENTS.items())


# ------------------------------------------------- reading an id back off disk


def session_id_of(path: str, keys: tuple[str, ...] = SESSION_KEYS) -> str:
    """The conversation id a transcript names, read from its first line only.

    One line, never the file. A Gemini transcript on this machine had grown
    to 1.6 GB, and `json.load` on that is not a slow sidebar but a dead one;
    every agent writes its header first, so the first line is both the
    cheapest read and the only one with the answer in it.

    The file name is the fallback, because Claude Code names the file after
    the id and codex is thought to put one in the name too - and a header
    that will not parse is exactly the moment the name is worth trying.
    """
    try:
        with open(path, "rb") as fh:
            line = fh.readline(HEADER_BYTES)
    except OSError:
        return ""
    try:
        header = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        header = None
    if isinstance(header, dict):
        # The header itself, then one level into it. Codex's layout is
        # unverified here and a wrapper - {"type": ..., "payload": {...}} -
        # is one of the shapes it is reported to have, so a dict one deep is
        # looked in rather than being told this is not a header.
        nested = [v for v in header.values() if isinstance(v, dict)]
        for source in [header, *nested]:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and UUID_RE.fullmatch(value.strip()):
                    return value.strip()
    found = UUID_RE.search(os.path.basename(path))
    return found.group(0) if found else ""


# A time written down as text, in the shapes ISO-8601 is written in: a `T`
# or a space between the date and the clock, any number of fractional digits
# or none, and `Z`, an offset, or nothing at all. Read here rather than by
# `datetime.fromisoformat`, which on the oldest Python this supports takes
# neither a `Z` nor nine fractional digits - and nine is what a program
# written in Go tends to print.
ISO_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
                      r"(?:\.(\d+))?\s*(Z|z|[+-]\d{2}:?\d{2})?")


def unix_time(value: object) -> float:
    """A state database's time column as a unix timestamp. 0.0 for nonsense.

    Sqlite does not enforce the type a column was declared with, so what
    comes back is whatever the agent wrote. Codex writes integers - seconds
    in one column and milliseconds in another, and the names do not settle
    which - while antigravity declares `last_modified_time` a `datetime`,
    which can arrive as an integer, a float or an ISO-8601 string depending
    on what wrote it. One question, so one helper answers it.

    0.0 means "no idea", not "the epoch": the caller reads it as a row with
    no time on it rather than as a row too old to want.
    """
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return 0.0
    when = 0.0
    if isinstance(value, str):
        try:
            when = float(value.strip())     # an epoch written out as text
        except ValueError:
            found = ISO_TIME.match(value.strip())
            if not found:
                return 0.0
            year, mon, day, hour, minute, sec, frac, zone = found.groups()
            try:
                stamp = datetime.datetime(
                    int(year), int(mon), int(day), int(hour), int(minute),
                    int(sec), int((frac or "").ljust(6, "0")[:6]))
                if zone and zone not in ("Z", "z"):
                    away = datetime.timedelta(hours=int(zone[1:3]),
                                              minutes=int(zone[-2:]))
                    stamp = stamp.replace(tzinfo=datetime.timezone(
                        -away if zone[0] == "-" else away))
                elif zone:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                # A string with no zone on it is read as local time, which
                # is the forgiving way round: a row of this project's that
                # reads a few hours into the future is still taken, where
                # one read into the past would be thrown away as yesterday.
                return stamp.timestamp()
            except (ValueError, OverflowError, OSError):
                return 0.0                  # a date that is not a date
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        when = float(value)
    while when > 1e12:              # milliseconds, or finer: neither the
        when /= 1000                # column name nor the type says which,
    return when if when > 0 else 0.0    # so anything too big to be a date
                                        # this century is scaled until it is


def conversation_in_db(agent: Agent, path: str, project: str,
                       since: float) -> str:
    """The newest conversation a state database has for a project.

    Codex kept JSONL rollouts once and now keeps sqlite - the migration is
    still visible in the table names - and antigravity keeps sqlite too.
    Either row is a better answer than any file ever was: it carries the
    project, so the conversation is matched to *here* rather than to
    whichever file happened to be touched last. The two tables are the same
    shape under different words, so the profile spells the words and this
    writes the one sentence they go into:

        select <id>, <times...> from <table>
        where <project> like '%' || ? || '%'
        order by <times... desc> limit 1

    Read-only, and forgiving of everything: the database belongs to another
    program, which may be mid-write, may have moved the schema on, or may not
    have created the row yet. Any of that means "not yet", never an error.
    """
    times = agent.state_db_time_columns
    if not (times and agent.state_db_table and agent.state_db_id_column
            and agent.state_db_project_column):
        return ""                       # not a profile with a database to ask
    # `like` rather than `=`, because only codex's column is a bare path.
    # Antigravity's is `workspace_uris`, plural, and with no row ever written
    # nobody knows whether it holds a path, a `file://` URI or a JSON list of
    # either - a substring reads all three alike. The project is a realpath,
    # long and specific enough that this is not loose in practice, and a row
    # belonging to another project still cannot match.
    query = (f"select {agent.state_db_id_column}, {', '.join(times)} "
             f"from {agent.state_db_table} "
             f"where {agent.state_db_project_column} like '%' || ? || '%' "
             f"order by {', '.join(c + ' desc' for c in times)} limit 1")
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    except sqlite3.Error:
        return ""
    try:
        rows = db.execute(query, (project,)).fetchall()
    except sqlite3.Error:
        return ""                       # no such table, or a schema we do not
    finally:                            # know: the profile simply learns
        db.close()                      # nothing, as it did before
    for ident, *stamps in rows:
        when = next((t for t in map(unix_time, stamps) if t), 0.0)
        # A time in a shape nobody here can read is not a reason to refuse
        # the row, and the trade is deliberate: the row is for this exact
        # project and is being looked at within a minute of the tab starting
        # - the sidebar stops asking after `DISCOVER_WINDOW` - so the newest
        # conversation here is this tab's. Refusing would lose the feature
        # outright on a column whose format is still unknown.
        if ident and (not when or when >= since):
            return str(ident)
    return ""


def discover_session(agent: Agent, since: float, home: str = "",
                     project: str = "") -> str:
    """What the agent called the conversation it opened in a tab. Or nothing.

    Gemini, codex and antigravity pick their own id and print it nowhere, so
    the one place it is written down is whatever the agent keeps on disk: a
    transcript for gemini, a sqlite state database for the other two. The
    rule for a transcript is the same for every profile that has a glob: the
    newest file matching it that was touched after the tab launched, and the
    id in that file's first line.

    `since` is what keeps yesterday out. The glob has no idea which project
    it is looking at - gemini's directories are named for the project but
    codex's are not, and neither is worth depending on - so "the newest one"
    is only the right answer because this tab is what has just been writing.
    A file older than the launch is somebody else's conversation and is never
    adopted.

    Costs one glob and one first line. Nothing here retries, waits or
    remembers: the caller decides how often to ask and when to stop.
    """
    if not (agent.transcript_glob or agent.state_db_glob):
        return ""                       # this profile cannot be asked
    # Escaped, because only the profile's half of this is a pattern: a home
    # directory with a `[` in it would otherwise match nothing at all.
    root = glob.escape(home or os.path.expanduser("~"))
    if agent.state_db_glob:
        # A database is asked about this project by name, so the newest one
        # of several - codex numbers them by schema version, antigravity has
        # just the one - is the only choice left to make.
        here = os.path.realpath(project) if project else ""
        for path in sorted(glob.iglob(os.path.join(root,
                                                   agent.state_db_glob)),
                           reverse=True):
            found = (conversation_in_db(agent, path, here, since)
                     if here else "")
            if found:
                return found
        return ""
    newest, seen = "", since
    try:
        for path in glob.iglob(os.path.join(root, agent.transcript_glob),
                               recursive=True):
            try:
                when = os.stat(path).st_mtime
            except OSError:
                continue                # gone between the glob and the ask
            if when > seen:
                newest, seen = path, when
    except OSError:
        return ""
    return session_id_of(newest, agent.session_keys) if newest else ""
