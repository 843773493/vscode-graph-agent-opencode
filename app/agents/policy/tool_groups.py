from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolGroupDefinition:
    group_id: str
    group_name: str
    kind: str

    def as_catalog_fields(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "group_name": self.group_name,
            "kind": self.kind,
        }


DEFAULT_TOOL_GROUP = ToolGroupDefinition(
    group_id="default",
    group_name="默认工具",
    kind="default",
)
AGENT_COLLABORATION_TOOL_GROUP = ToolGroupDefinition(
    group_id="agent-collaboration",
    group_name="默认工具 · Agent Collaboration",
    kind="collaboration",
)

DIRECT_AGENT_COLLABORATION_TOOL_NAMES = frozenset(
    {
        "monitor_session_agent_end",
        "send_message_to_session",
        "task",
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "attach_team_session",
        "assign_team_task",
        "update_team_task",
    }
)
AGENT_COLLABORATION_EXTENSION_TOOL_NAMES = frozenset(
    {
        "read_session_recent_text_messages",
        "grep_session_context_jsonl",
        "read_session_context_jsonl",
    }
)
AGENT_COLLABORATION_TOOL_NAMES = (
    DIRECT_AGENT_COLLABORATION_TOOL_NAMES
    | AGENT_COLLABORATION_EXTENSION_TOOL_NAMES
)


def catalog_group_for_tool(tool_name: str) -> ToolGroupDefinition:
    if tool_name in AGENT_COLLABORATION_TOOL_NAMES:
        return AGENT_COLLABORATION_TOOL_GROUP
    return DEFAULT_TOOL_GROUP
