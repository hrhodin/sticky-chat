"""sticky-chat - sticky notes for AI coding agents, in tmux.

Annotate what the agent printed and hand the quoted lines back with your
notes attached. Written against Claude Code, and everything that depends on
*which* agent is in the pane lives in `agents` as one profile per agent; the
placing, the sidebar and the store take a profile and ask it.

Select any part of a pane's output with the cursor, attach a short note, see
quote and note in a sidebar pane aligned with the original text, then commit:
every pending note is rendered as the quoted text plus your note and pasted
into the left pane's prompt.

The pieces, in dependency order: `tmux` talks to tmux and nothing else does;
`agents` is one profile per coding agent; `store` is the note file and which
one a pane is showing; `placement` decides which row a note belongs to;
`sidebar` draws the right-hand pane; `config` generates the tmux config and
sizes the window; `commands` is one function per subcommand; `cli` is the
argument handling. Everything public is
re-exported here, so `import sticky_chat` still sees one namespace.
"""

from __future__ import annotations

# This file exists to re-export: `import sticky_chat` should still see one
# namespace, and the tests reach for names without caring which file they
# ended up in.
# ruff: noqa: F401

__version__ = "0.1.0"

from .agents import (
    AGENTS,
    ANTIGRAVITY,
    CLAUDE,
    CODEX,
    DEFAULT_AGENT,
    GEMINI,
    GENERIC,
    HEADER_BYTES,
    SESSION_KEYS,
    Agent,
    agent_choices,
    agent_named,
    conversation_in_db,
    discover_session,
    known_name,
    require_agent,
    session_id_of,
    unix_time,
)
from .cli import GLOBAL_FLAGS, OUR_FLAGS, build_parser, main, split_our_flags
from .clipboard import clipboard_command, to_clipboard
from .commands import (
    DISMISS_GRACE,
    YEAR,
    ago,
    ask,
    ask_project,
    attach,
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
    cmd_reload,
    cmd_reopen,
    cmd_restore,
    cmd_resume,
    cmd_rm,
    cmd_start,
    cmd_sweep,
    cmd_uncommit,
    column,
    comes_back,
    dismissing,
    expand_paste,
    jump_to_note,
    leave_copy_mode,
    mark_launch,
    nudge,
    open_project_prompt,
    open_reopen_prompt,
    open_sidebar,
    open_tab,
    pane_env,
    pick_tab,
    render_commit,
    reopen_tab,
    reopenable,
    resume_command,
    sidebar_pid,
    tab_name,
    tab_row,
    tell,
)
from .config import (
    ACTIVITY_HOOK,
    CONFIG_TEMPLATE,
    DEFAULT_SIDEBAR_WIDTH,
    DEFAULT_VIRTUAL_ROWS,
    EXIT_HOOK,
    FOCUS_STYLE,
    RESERVED_IN_COPY_MODE,
    client_area,
    client_areas,
    default_virtual_rows,
    fit_windows,
    install_hooks,
    pin_clients,
    prime_rows,
    typing_keys,
    virtual_window,
    write_config,
)
from .placement import (
    CONTEXT_ROWS,
    FUZZY_DRIFT,
    FUZZY_THRESHOLD,
    GLYPH_LANDMARK,
    LANDMARK_WINDOW,
    MIN_FUZZY_CHARS,
    PASTE_PLACEHOLDER,
    PROMPT_BOX_ROWS,
    PROMPT_ROWS,
    VIEW_FORMAT,
    context_score,
    find_landmark,
    in_prompt_box,
    note_candidates,
    pane_view,
    parse_view,
    place_note,
    placements,
    resolve,
)
from .sidebar import (
    BAND_SHARE,
    CLOSE,
    DISCOVER_EVERY,
    DISCOVER_WINDOW,
    HELP_SECTIONS,
    SIDEBAR_BUSY,
    SIDEBAR_FROZEN_TICK,
    SIDEBAR_IDLE,
    SIDEBAR_SETTLE,
    SIDEBAR_TICK,
    band_lines,
    band_window,
    build_frame,
    build_help,
    close_column,
    cmd_sidebar,
    draw,
    foot_lines,
    footer_text,
    help_sections,
    note_bullet,
    note_style,
    with_close,
)
from .store import (
    SESSION_FLAGS,
    STATE_HOME,
    Store,
    agent_of,
    claude_pane,
    known_windows,
    launched_as,
    names_a_session,
    open_store,
    private_dir,
    project_slug,
    record_window,
    resolve_project,
    session_dir,
    session_named,
    sidebar_showing,
    sidebars_showing,
    tabs_showing,
    windows_dir,
    with_words,
    without_session,
)
from .tmux import DEFAULT_SOCKET, SESSION_NAME, Tmux
from .util import (
    ANSI,
    ARROWS,
    BOLD,
    COL_COMMITTED,
    COL_FOOTER,
    COL_FUZZY,
    COL_PENDING,
    DIM,
    RESET,
    REVERSE,
    STATUS_COLOR,
    STRIKE,
    cell_width,
    complete_path,
    die,
    dwidth,
    lay_out,
    prompt_line,
    read_key,
    row_step,
    self_path,
    shell_quote,
    terminal_size,
    terminal_width,
    truncate,
    visible,
    wrap,
)

__all__ = [name for name in dir() if not name.startswith("_")]


if __name__ == "__main__":
    import sys
    sys.exit(main())
