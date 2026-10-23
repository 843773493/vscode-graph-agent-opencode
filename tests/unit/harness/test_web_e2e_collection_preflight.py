from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_web_e2e_conftest() -> ModuleType:
    conftest_path = (
        Path.cwd() / "tests" / "e2e" / "clients" / "web" / "conftest.py"
    )
    spec = importlib.util.spec_from_file_location(
        "web_e2e_collection_conftest",
        conftest_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法载入 Web E2E collection fixture: {conftest_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_collection_preflight_checks_paths_only_for_target_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest = _load_web_e2e_conftest()
    project_root = Path.cwd().resolve()
    target_file = project_root / conftest._SECTION8_TARGET_FILE
    selection_contract = {"mode": "default"}
    monkeypatch.setattr(
        conftest,
        "_read_section8_selection_contract",
        lambda _root: selection_contract,
    )

    class UnrelatedItem:
        nodeid = "tests/e2e/clients/web/test_other.py::test_unrelated"

        @property
        def path(self) -> Path:
            raise AssertionError("无关用例不应解析文件路径")

    target_item = SimpleNamespace(
        nodeid=(
            f"{conftest._SECTION8_TARGET_FILE}::"
            f"{conftest._SECTION8_TARGET_NAME}[default]"
        ),
        path=target_file,
    )
    session = SimpleNamespace(
        config=SimpleNamespace(
            option=SimpleNamespace(collectonly=False),
            stash={},
        ),
        items=[UnrelatedItem(), target_item],
    )

    conftest.pytest_collection_finish(session)

    assert session.config.stash[conftest._SECTION8_CONTRACT_KEY] is selection_contract
