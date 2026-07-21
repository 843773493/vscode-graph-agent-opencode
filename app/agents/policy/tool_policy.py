from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


TOOL_POLICY_ALL = "all"
TOOL_POLICY_EXTENSIONS = "extensions"
TOOL_POLICY_SELECTORS = frozenset({TOOL_POLICY_ALL, TOOL_POLICY_EXTENSIONS})

# 这里集中声明默认工具和 DeepAgents middleware 暴露给模型的工具。配置解析、
# Agent 列表和运行时必须使用同一全集，不能各自维护一套“可用工具”解释。
DEFAULT_AGENT_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "python_exec",
        "emit_system_time_messages",
        "monitor_session_agent_end",
        "collect_background_messages",
        "persistent_terminal",
        "send_message_to_session",
        "task",
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "attach_team_session",
        "assign_team_task",
        "update_team_task",
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "compact_conversation",
    }
)

_SESSION_DELEGATION_TOOLS = frozenset({"task", "create_team_member"})
_SESSION_COMMUNICATION_TOOL = "send_message_to_session"


@dataclass(frozen=True, slots=True)
class ResolvedToolPolicy:
    """一次完整解析后的工具策略；集合字段均为最终权威结果。"""

    universe_names: frozenset[str]
    extension_names: frozenset[str]
    allowlist: frozenset[str]
    denylist: frozenset[str]
    enabled_names: frozenset[str]
    disabled_names: frozenset[str]

    @property
    def enabled_extension_names(self) -> frozenset[str]:
        return self.extension_names & self.enabled_names


def build_agent_tool_universe(
    *,
    extension_names: Iterable[str] = (),
    include_test_tools: bool = False,
) -> frozenset[str]:
    resolved_extensions = _normalize_names(extension_names, field_name="extension_names")
    reserved_extensions = resolved_extensions & TOOL_POLICY_SELECTORS
    if reserved_extensions:
        raise ValueError(
            "扩展工具名称不能使用策略保留值: "
            + ", ".join(sorted(reserved_extensions))
        )
    universe = set(DEFAULT_AGENT_TOOL_NAMES)
    universe.update(resolved_extensions)
    if include_test_tools:
        universe.add("test_tool")
    return frozenset(universe)


def resolve_tool_policy(
    *,
    universe_names: Iterable[str],
    extension_names: Iterable[str] = (),
    allowlist: Iterable[str] = (),
    denylist: Iterable[str] = (),
    context: str = "工具策略",
) -> ResolvedToolPolicy:
    """解析 allowlist/denylist，并校验最终启用工具的依赖。

    集合规则很简单：先把 denylist 展开为禁用集合 D，再把 allowlist
    展开为恢复集合 A；最终禁用 D-A，最终可用 U-(D-A)。因此默认 U
    全部可用，allowlist 不是独立白名单，而是对 denylist 的定点撤销。
    """

    universe = _normalize_names(universe_names, field_name="universe_names")
    extensions = _normalize_names(extension_names, field_name="extension_names")
    if not extensions <= universe:
        missing = extensions - universe
        raise ValueError(
            f"{context} 的扩展工具不在工具全集中: {', '.join(sorted(missing))}"
        )
    raw_allowlist = _normalize_names(allowlist, field_name="allowlist")
    raw_denylist = _normalize_names(denylist, field_name="denylist")
    _validate_selectors(
        raw_allowlist | raw_denylist,
        universe=universe,
        context=context,
    )

    expanded_denied = _expand_selectors(
        raw_denylist,
        universe=universe,
        extensions=extensions,
    )
    expanded_allowed = _expand_selectors(
        raw_allowlist,
        universe=universe,
        extensions=extensions,
    )
    disabled = expanded_denied - expanded_allowed
    enabled = universe - disabled
    validate_tool_dependencies(enabled, context=context)
    return ResolvedToolPolicy(
        universe_names=universe,
        extension_names=extensions,
        allowlist=raw_allowlist,
        denylist=raw_denylist,
        enabled_names=frozenset(enabled),
        disabled_names=frozenset(disabled),
    )


def resolve_tool_selectors(
    *,
    selectors: Iterable[str],
    universe_names: Iterable[str],
    extension_names: Iterable[str] = (),
    context: str = "工具选择器",
) -> frozenset[str]:
    """校验并展开 all/extensions/具体工具名选择器。"""

    universe = _normalize_names(universe_names, field_name="universe_names")
    extensions = _normalize_names(extension_names, field_name="extension_names")
    raw_selectors = _normalize_names(selectors, field_name="selectors")
    _validate_selectors(raw_selectors, universe=universe, context=context)
    return _expand_selectors(
        raw_selectors,
        universe=universe,
        extensions=extensions,
    )


def validate_tool_dependencies(
    enabled_tool_names: Iterable[str],
    *,
    context: str = "工具策略",
) -> None:
    """校验跨工具依赖，供配置加载、开关事务和运行时共同调用。"""

    enabled = _normalize_names(enabled_tool_names, field_name="enabled_tool_names")
    enabled_delegation_tools = _SESSION_DELEGATION_TOOLS & enabled
    if enabled_delegation_tools and _SESSION_COMMUNICATION_TOOL not in enabled:
        raise ValueError(
            f"{context} 存在工具依赖冲突："
            f"{', '.join(sorted(enabled_delegation_tools))} "
            f"依赖 {_SESSION_COMMUNICATION_TOOL}；"
            "请恢复通信工具，或同时禁用这些委派工具"
        )


def _normalize_names(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} 必须是字符串数组，不能是单个字符串")
    items = tuple(values)
    if not all(isinstance(item, str) and item for item in items):
        raise TypeError(f"{field_name} 必须只包含非空字符串")
    return frozenset(items)


def _validate_selectors(
    selectors: frozenset[str],
    *,
    universe: frozenset[str],
    context: str,
) -> None:
    unknown = selectors - universe - TOOL_POLICY_SELECTORS
    if unknown:
        raise ValueError(
            f"{context} 引用了不存在的工具: {', '.join(sorted(unknown))}"
        )


def _expand_selectors(
    selectors: frozenset[str],
    *,
    universe: frozenset[str],
    extensions: frozenset[str],
) -> frozenset[str]:
    expanded = set(selectors - TOOL_POLICY_SELECTORS)
    if TOOL_POLICY_ALL in selectors:
        expanded.update(universe)
    if TOOL_POLICY_EXTENSIONS in selectors:
        expanded.update(extensions)
    return frozenset(expanded)
