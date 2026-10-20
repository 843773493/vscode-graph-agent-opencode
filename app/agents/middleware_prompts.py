from __future__ import annotations

RUNTIME_IDENTITY_HEADER = "## 当前运行时身份与路径边界（系统权威元数据）"
RUNTIME_WORKSPACE_SCOPE_FIELD = "- workspace 根目录（exec_command 未提供 workdir 时的 cwd）："

TODO_SYSTEM_PROMPT = (
    "Use `write_todos` only when a complex request benefits from visible multi-step tracking. "
    "Skip it for simple or conversational requests. Update a step as soon as its state changes, "
    "and do not call the tool more than once in parallel."
)

TODO_TOOL_DESCRIPTION = (
    "Replace the current task list. Use concise actionable items with `pending`, `in_progress`, "
    "or `completed` status. Only mark work completed after it is actually finished."
)

SKILLS_SYSTEM_PROMPT = """Available skills:
{skills_list}

When the user's request matches a skill, call `skill_load` with the skill name before acting. The name is resolved by the application; do not ask for or invent a file path. `snapshot` is the default and loads the current skill once. Use `tracked` only when later observed changes should be applied, and `untrack` to stop tracking without removing already injected context. Follow the loaded instructions and do not infer omitted tool names or arguments."""

FILESYSTEM_SYSTEM_PROMPT = (
    "Use the available filesystem tools according to their schemas. Read existing files before "
    "editing them, preserve the repository's conventions, and inspect large results in bounded chunks. "
    "Do not grep or glob the .boxteam runtime directory; it contains session logs, streams, and traces. "
    "Prefer standard workspace-relative paths for every model-facing filesystem path. Use `.` for the "
    "workspace root and paths such as `src/main.js` for files; this is the unambiguous recommended "
    "form and stays valid when the workspace moves. Native paths and resource URIs resolve at the fixed target owner; "
    "external targets require the target owner's native-directory capability and otherwise fail explicitly without redirection. "
    "ls returns bounded JSON with resource URIs and an optional next_cursor; pass the same target and limits when continuing. "
    "Workspace files URIs returned by ls and paths returned by glob and grep can be passed unchanged to "
    "read_file, write_file, edit_file, and workspace source-debugging tools. Skill directory/file URIs support ls/read_file/glob/grep "
    "without activation or tracking; only `skill_load` by name activates instructions. "
    "Never list, glob, grep, or read any `.boxteam` path directly. read_file uses a 1-indexed `line_offset`, "
    "reports truncation, and has explicit byte/response/deadline budgets."
)

FILESYSTEM_TOOL_DESCRIPTIONS = {
    "ls": (
        "List a directory using a workspace-relative/native path or a resource URI; use `.` for the workspace root. "
        "Returns bounded JSON entries with reusable URIs, truncation_reason and next_cursor. "
        "Continue with the same target and limits; this is a live directory view, not a historical snapshot."
    ),
    "read_file": (
        "Read a file using a workspace-relative/native path or a resource URI. Paths returned by ls, glob, "
        "and grep are reusable unchanged. Use the 1-indexed `line_offset` and "
        "optional `max_lines` for large text files. "
        "Images, audio, video, and PDFs return multimodal content; do not paginate those files. "
        "Skill file URIs return content without activation; use skill_load to activate instructions. "
        "Do not access `.boxteam` runtime data directly. max_bytes bounds the scanned prefix including skipped lines; "
        "increase it explicitly for later lines. Media must fit both byte budgets and is never partially returned."
    ),
    "write_file": "Create a text file; prefer a workspace-relative path.",
    "edit_file": (
        "Replace exact text in a file; prefer a workspace-relative path. Read the file first, "
        "preserve indentation, and use replace_all only when every occurrence should change."
    ),
    "glob": (
        "Find files recursively below a workspace-relative/native target or resource URI. The pattern never changes the target. "
        "Returns bounded URI JSON; continue next_cursor with the same target, pattern and limits."
    ),
    "grep": (
        "Search literal text below a workspace-relative/native path or resource URI with explicit scan/byte/deadline limits; prefer a "
        "workspace-relative path and do not search `.boxteam` runtime logs or use an unscoped "
        "workspace-root recursive search. Results contain reusable URIs, line numbers and bounded snippets; count is per page. "
        "Continue next_cursor with the same target, query, glob and limits. Known media and NUL-containing binary files are skipped."
    ),
}

COMPACT_CONVERSATION_SYSTEM_PROMPT = (
    "Use `compact_conversation` only when earlier conversation detail is no longer needed and reducing "
    "context will materially help later work. Do not compact during a short or unfinished request."
)

TEAM_COORDINATION_SYSTEM_PROMPT = (
    "Team collaboration is event-driven. After assign_team_task starts another Session, end the "
    "current response promptly and tell the user the task was dispatched. Do not poll with "
    "get_team_board, exec_command/sleep, filesystem reads, wait_for_session, or "
    "collect_background_messages, and do not redo the assignee's review or test yourself. A terminal "
    "team task update automatically starts a coordinator Job. In that notification Job, call "
    "get_team_board once and provide one complete result containing team, member Session IDs, task "
    "status, and conclusion. The notification is emitted only after the board update is persisted, so "
    "never claim that the team board is pending, stale, or not yet synchronized when the returned board "
    "contains that update. Even when the user says to wait, use this asynchronous notification flow."
)


__all__ = [
    "COMPACT_CONVERSATION_SYSTEM_PROMPT",
    "FILESYSTEM_SYSTEM_PROMPT",
    "FILESYSTEM_TOOL_DESCRIPTIONS",
    "SKILLS_SYSTEM_PROMPT",
    "TEAM_COORDINATION_SYSTEM_PROMPT",
    "TODO_SYSTEM_PROMPT",
    "TODO_TOOL_DESCRIPTION",
]
