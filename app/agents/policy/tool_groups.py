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
DEBUGGING_TOOL_GROUP = ToolGroupDefinition(
    group_id="debugging",
    group_name="源码调试",
    kind="debugging",
)

DEBUGGING_TOOL_NAMES = frozenset(
    {
        "list_debug_configurations",
        "create_debug_configuration",
        "activate_debug_configuration",
        "delete_debug_configuration",
        "start_debugging",
        "stop_debugging",
        "step_over",
        "step_into",
        "step_out",
        "continue_execution",
        "pause_execution",
        "restart_debugging",
        "add_breakpoint",
        "add_logpoint",
        "remove_breakpoint",
        "clear_all_breakpoints",
        "list_breakpoints",
        "list_variable_names",
        "get_variables_values",
        "evaluate_expression",
    }
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
        "read_context",
        "search_context",
    }
)
AGENT_COLLABORATION_TOOL_NAMES = (
    DIRECT_AGENT_COLLABORATION_TOOL_NAMES
    | AGENT_COLLABORATION_EXTENSION_TOOL_NAMES
)


def catalog_group_for_tool(tool_name: str) -> ToolGroupDefinition:
    if tool_name in DEBUGGING_TOOL_NAMES:
        return DEBUGGING_TOOL_GROUP
    if tool_name in AGENT_COLLABORATION_TOOL_NAMES:
        return AGENT_COLLABORATION_TOOL_GROUP
    return DEFAULT_TOOL_GROUP
