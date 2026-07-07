from __future__ import annotations

from typing import Any


def extract_agent_tools_by_name(agent: Any) -> dict[str, Any]:
    """从 DeepAgents 图中提取工具映射，集中隔离第三方私有结构访问。"""
    graph_view = agent.get_graph()
    nodes = getattr(graph_view, "nodes", {}) or {}
    tool_map: dict[str, Any] = {}
    for node in nodes.values():
        candidate = getattr(node, "data", node)
        tools_by_name = getattr(candidate, "tools_by_name", None)
        if isinstance(tools_by_name, dict):
            tool_map.update(tools_by_name)
    return tool_map
