from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal, TypedDict, cast

import pytest


class Section8SelectionContract(TypedDict):
    mode: Literal["default", "focus"]
    requested: list[str]
    selected: list[str]
    validScenarios: list[str]
    defaultScenarios: list[str]


_SECTION8_TARGET_FILE = Path("tests/e2e/clients/web/test_basic_chat_tool_loop.py")
_SECTION8_TARGET_NAME = "test_basic_chat_tool_loop_through_real_web_surface"
_SECTION8_CONTRACT_KEY: pytest.StashKey[Section8SelectionContract] = pytest.StashKey()


def _section8_contract_error(message: str) -> pytest.UsageError:
    return pytest.UsageError(f"Section 8 Web E2E focus preflight 失败：{message}")


def _read_section8_selection_contract(project_root: Path) -> Section8SelectionContract:
    selector = project_root / "tests/e2e/clients/web/basic-chat-tool-loop.e2e.mjs"
    command = ["bun", str(selector.relative_to(project_root)), "--section8-selection-contract"]
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _section8_contract_error(f"无法执行唯一 selector contract CLI：{error}") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "没有诊断信息"
        raise _section8_contract_error(detail)

    contract_output_lines = result.stdout.splitlines()
    if len(contract_output_lines) != 1:
        raise _section8_contract_error("selector contract CLI 未返回单行 JSON")
    try:
        contract_value: object = json.loads(contract_output_lines[0])
    except json.JSONDecodeError as error:
        raise _section8_contract_error(f"selector contract CLI 返回无效 JSON：{error}") from error
    if not isinstance(contract_value, dict):
        raise _section8_contract_error("selector contract 必须是 JSON 对象")

    contract_payload = cast(dict[str, object], contract_value)
    mode = contract_payload.get("mode")
    if mode not in {"default", "focus"}:
        raise _section8_contract_error("selector contract 的 mode 无效")

    scenario_lists: dict[str, list[str]] = {}
    for key in ("requested", "selected", "validScenarios", "defaultScenarios"):
        scenario_values = contract_payload.get(key)
        if not isinstance(scenario_values, list) or not all(
            isinstance(item, str) for item in scenario_values
        ):
            raise _section8_contract_error(f"selector contract 的 {key} 必须是字符串数组")
        scenario_lists[key] = cast(list[str], scenario_values)

    return {
        "mode": cast(Literal["default", "focus"], mode),
        "requested": scenario_lists["requested"],
        "selected": scenario_lists["selected"],
        "validScenarios": scenario_lists["validScenarios"],
        "defaultScenarios": scenario_lists["defaultScenarios"],
    }


def pytest_collection_finish(session: pytest.Session) -> None:
    """仅在真实目标 E2E 入选后、fixture setup 前校验 MJS 权威 focus。"""

    if session.config.option.collectonly:
        return

    project_root = Path.cwd().resolve()
    target_file = project_root / _SECTION8_TARGET_FILE
    target_selected = any(
        item.nodeid.rsplit("::", maxsplit=1)[-1].split("[", maxsplit=1)[0]
        == _SECTION8_TARGET_NAME
        and Path(item.path).resolve() == target_file
        for item in session.items
    )
    if not target_selected:
        return

    session.config.stash[_SECTION8_CONTRACT_KEY] = _read_section8_selection_contract(project_root)


@pytest.fixture(scope="session")
def section8_selection_contract(pytestconfig: pytest.Config) -> Section8SelectionContract:
    """把 collection preflight 得到的 MJS 权威选择提供给目标用例。"""

    return pytestconfig.stash[_SECTION8_CONTRACT_KEY]
