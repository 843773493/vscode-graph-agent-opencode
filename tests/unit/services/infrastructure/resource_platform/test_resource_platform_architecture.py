"""资源平台跨模块静态边界审计。"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path.cwd()


def test_no_dynamic_import_or_plugin_surface_in_core_modules() -> None:
    """derivation/registry 核心禁止动态 import/plugin manifest 面。"""
    forbidden = {"importlib", "__import__", "exec", "eval", "compile"}
    targets = [
        PROJECT_ROOT / "app/services/infrastructure/resource_platform/derivation",
        PROJECT_ROOT / "app/services/infrastructure/resource_platform/registry",
    ]
    violations: list[str] = []
    for directory in targets:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name.startswith("importlib") for alias in node.names
                ):
                    violations.append(f"{path}:{node.lineno}:import importlib")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and (node.func.id in forbidden)
                ):
                    violations.append(f"{path}:{node.lineno}:{node.func.id}")
    assert violations == [], ("发现动态执行/插件面:", violations)
