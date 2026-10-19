"""上下文来源必须显式登记，旧请求反向捕获链不能重新进入生产 import graph。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

LEGACY_CAPTURE_SYMBOLS = {
    "PromptReplayCaptureMiddleware",
    "ItemizedContextProjectionMiddleware",
    "SealedAssemblyDispatchBridge",
    "_PROMPT_REPLAY_LABELS",
    "_instrument_prompt_replay",
    "_prompt_contributions",
    "prompt_replay",
    "prompt_replay_capture",
    "itemized_context_projection",
}


@pytest.fixture
def production_trees() -> tuple[tuple[str, ast.Module], ...]:
    root = Path.cwd()
    return tuple(
        (str(path.relative_to(root)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted((root / "app").rglob("*.py"))
    )


def test_production_has_no_reverse_prompt_capture(production_trees) -> None:
    """检查真实 AST 定义和引用，注释中的历史说明不算生产调用。"""
    violations: list[str] = []
    for path, tree in production_trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names = (node.id,)
            elif isinstance(node, ast.Attribute):
                names = (node.attr,)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = (node.name,)
            elif isinstance(node, ast.alias):
                names = (node.name, node.asname or "")
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            for name in names:
                if set(name.split(".")) & LEGACY_CAPTURE_SYMBOLS:
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == [], f"生产重新引入了请求反向捕获或旧 dispatch owner: {violations}"
