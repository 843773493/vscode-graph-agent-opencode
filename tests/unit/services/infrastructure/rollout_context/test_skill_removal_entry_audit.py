"""静态审计:除 skill_load 与受信 Session 控制入口外，不存在 Skill 即时移除入口。

OpenSpec add-context-injection-lifecycle 1.3/3.5:本 change 不提供 Skill 即时移除
工具;untrack 只停止后续追踪,不删除、改写或重排已注入内容。该合同以 AST 静态
断言锁死,而不是文档声明:

1. "tracking_status" 的赋值只允许出现在 ContextSourceManager 域内;
2. 生产代码调用 "load_skill(...)" 只允许出现在白名单文件
   (模型 skill_load 工具 + 受信 Session 控制 API);
3. 不存在其它以 Skill 移除/卸载语义命名的生产函数或第二个 "skill_*" 工具名。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path.cwd()
APP_ROOT = PROJECT_ROOT / "app"

# 受信 Skill 状态入口白名单:模型 skill_load 工具。受信 Session 控制 API
# (OpenSpec 3.5,复用同一 CSM mutation)落地后必须显式加入。
LOAD_SKILL_CALLER_WHITELIST = {
    "app/agents/tools/skill_loading.py",
    # C7-B1 受信 Session 控制 API：untrack 与 skill_load 工具复用同一
    # CSM load_skill mutation，不存在第二状态机或移除入口。
    "app/services/business/session_skill_tracking_service.py",
}
# tracking 状态的唯一域 owner。
TRACKING_OWNER_FILES = {
    "app/services/infrastructure/rollout_context/runtime/context_sources/context_source_manager.py",
}
# 生产代码中合法的 skill_load 工具名(LangChain @tool 首参)。
ALLOWED_SKILL_TOOL_NAMES = {"skill_load"}

_REMOVAL_NAME_PATTERN = re.compile(
    r"(remove|delete|drop|clear|purge|unregister)[a-z_]*skill"
    r"|skill[a-z_]*(remove|delete|drop|clear|purge|unregister)",
    re.IGNORECASE,
)


def _iter_app_trees() -> list[tuple[str, ast.Module]]:
    trees: list[tuple[str, ast.Module]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        trees.append((rel, tree))
    return trees


def _is_call_to(node: ast.AST, attr_names: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in attr_names
    if isinstance(func, ast.Name):
        return func.id in attr_names
    return False


def test_tracking_status_mutations_only_in_csm_owner() -> None:
    """Skill 追踪状态只能由 ContextSourceManager 域 owner 改写。"""
    violations: list[str] = []
    for rel, tree in _iter_app_trees():
        if rel in TRACKING_OWNER_FILES:
            continue
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "tracking_status":
                    violations.append(f"{rel}:{node.lineno}")
    assert violations == [], (
        "tracking_status 被域外改写，构成 Skill 即时移除第二入口:", violations
    )


def test_load_skill_caller_whitelist_entries_exist() -> None:
    # 白名单条目必须指向真实存在的生产文件，防止入口删除后残留空条目。
    for rel in LOAD_SKILL_CALLER_WHITELIST:
        assert (PROJECT_ROOT / rel).is_file(), rel


def test_load_skill_calls_only_from_whitelisted_entries() -> None:
    """生产代码只允许白名单入口调用 CSM 的 load_skill。"""
    violations: list[str] = []
    for rel, tree in _iter_app_trees():
        if rel in LOAD_SKILL_CALLER_WHITELIST:
            continue
        for node in ast.walk(tree):
            if _is_call_to(node, {"load_skill"}):
                violations.append(f"{rel}:{node.lineno}")
    assert violations == [], (
        "load_skill 出现在白名单之外(只允许 skill_load 工具与受信 Session 控制 API):",
        violations,
    )


def test_no_skill_removal_named_entries_outside_owner() -> None:
    """不存在 remove/delete/unregister Skill 语义的生产入口命名。"""
    violations: list[str] = []
    for rel, tree in _iter_app_trees():
        if rel in TRACKING_OWNER_FILES:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                _REMOVAL_NAME_PATTERN.search(node.name)
            ):
                violations.append(f"{rel}:{node.lineno}:{node.name}")
            if isinstance(node, ast.Attribute) and _REMOVAL_NAME_PATTERN.search(node.attr):
                violations.append(f"{rel}:{node.lineno}:{node.attr}")
    assert violations == [], ("发现疑似 Skill 移除入口命名:", violations)


def test_skill_load_is_the_only_skill_named_tool() -> None:
    """skill_load 是唯一以 skill_ 命名的 LangChain 工具;不存在第二移除工具。"""
    tool_names: set[str] = set()
    locations: dict[str, str] = {}
    for rel, tree in _iter_app_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_tool_call = (
                isinstance(func, ast.Name) and func.id == "tool"
            ) or (
                isinstance(func, ast.Attribute) and func.attr == "tool"
            )
            if not is_tool_call or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and (
                first.value.startswith("skill_")
            ):
                tool_names.add(first.value)
                locations[first.value] = f"{rel}:{node.lineno}"
    assert tool_names == ALLOWED_SKILL_TOOL_NAMES, (
        "发现 skill_load 之外的 skill_* 工具:", locations,
    )


def test_whitelisted_callers_exist() -> None:
    """白名单文件必须真实存在，防止白名单漂移成空约束。"""
    for rel in LOAD_SKILL_CALLER_WHITELIST | TRACKING_OWNER_FILES:
        assert (PROJECT_ROOT / rel).is_file(), rel


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
