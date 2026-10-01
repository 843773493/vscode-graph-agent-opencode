from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import get_args

import pytest

from app.core.identifier import (
    IdentifierPrefix,
    create_prefixed_id,
    create_uuid_hex,
)


def test_create_uuid_hex_has_uuidv7_profile():
    value = create_uuid_hex()

    assert len(value) == 32
    assert value == value.lower()
    # UUIDv7 位 profile：第 13 个 hex 为 7，第 17 个 hex 属于 variant 8|9|a|b。
    assert value[12] == "7"
    assert value[16] in "89ab"


def test_create_prefixed_id_has_uuidv7_profile():
    value = create_prefixed_id("evt")
    prefix, raw_uuid = value.split("_", maxsplit=1)

    assert prefix == "evt"
    assert len(raw_uuid) == 32
    assert raw_uuid[12] == "7"
    assert raw_uuid[16] in "89ab"


def test_generated_ids_are_unique():
    values = {create_prefixed_id("msg") for _ in range(10_000)}

    assert len(values) == 10_000


def test_uuid_utils_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """uuid_utils 导入失败时 MUST 抛错，MUST NOT 回退 uuid.uuid4()。"""
    # sys.modules 中置 None 会让 ``import uuid_utils`` 抛 ImportError。
    monkeypatch.setitem(sys.modules, "uuid_utils", None)

    with pytest.raises(RuntimeError, match="uuid-utils"):
        create_uuid_hex()
    with pytest.raises(RuntimeError, match="uuid-utils"):
        create_prefixed_id("ses")


def test_uuid7_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """uuid_utils 缺 uuid7 时 MUST 抛错，MUST NOT 回退 uuid.uuid4()。"""
    fake = types.SimpleNamespace(__version__="9.9.9")
    monkeypatch.setitem(sys.modules, "uuid_utils", fake)

    with pytest.raises(RuntimeError, match="uuid7"):
        create_uuid_hex()


def _literal_prefixes_used_by_callers() -> set[str]:
    """扫描 ``app/`` 下所有把字面量传给 ``create_prefixed_id`` 的调用点。

    只认从 ``app.core.identifier`` 导入的该函数，避免把同名助手算进来；
    非字面量实参直接报错（本仓既无此类调用，也不允许它悄悄绕过口径）。
    """
    prefix_use: dict[str, list[str]] = {}
    app_root = Path.cwd() / "app"
    if not app_root.is_dir():
        raise RuntimeError(
            "本断言要求从仓库根目录运行 pytest（需扫描 app/ 调用面）: "
            f"cwd={Path.cwd()}"
        )
    for path in sorted(app_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.core.identifier":
                for alias in node.names:
                    if alias.name == "create_prefixed_id":
                        local_names.add(alias.asname or alias.name)
        if not local_names:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id in local_names):
                continue
            if not node.args or not (
                isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                raise AssertionError(
                    f"create_prefixed_id 只接受字面量前缀: {path}:{node.lineno}"
                )
            prefix = node.args[0].value
            prefix_use.setdefault(prefix, []).append(f"{path}:{node.lineno}")
    return set(prefix_use)


def test_identifier_prefix_literal_matches_callers():
    """``IdentifierPrefix`` 声明面必须与全仓实际调用面完全一致。

    该类 Literal 只是静态注解、不被运行时代码内省，因此「声明了没人用」
    或「在用了但没声明」都不会在任何东西上炸掉——只能靠本断言兜住。历史
    上曾漂移出 12 个在用未声明前缀，并留下一个零引用声明 ``mcall``。
    """
    declared = set(get_args(IdentifierPrefix))
    used = _literal_prefixes_used_by_callers()

    assert declared - used == set(), f"声明但零引用: {sorted(declared - used)}"
    assert used - declared == set(), f"在用但未声明: {sorted(used - declared)}"
