"""配置 owner 的真实文件输入、监听发布、失败和冻结恢复合同。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.resource_uri import file_uri_child, resource_display_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.unit.services.infrastructure.test_config_service import (
    _base_config,
    _write_workspace_config,
)


@dataclass
class FileCase:
    config: ConfigService
    workspace: Path
    config_path: Path
    paths: WorkspaceFilePaths

    def select(self, path: Path | None, producer: str = "R01") -> None:
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        instructions = value["agents"]["default"]["instructions"]
        if producer == "R02":
            instructions = instructions.setdefault(
                "runtime_identity", {"description": "inline identity"}
            )
        key = "system_prompt_file" if producer == "R01" else "description_file"
        instructions[key] = self.paths.reference(path) if path is not None else None
        self.config_path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def file_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileCase:
    monkeypatch.setenv("TEST_API_KEY", "unused-key")
    workspace = tmp_path / "workspace"
    (workspace / ".boxteam").mkdir(parents=True)
    paths = WorkspaceFilePaths(
        workspace=workspace,
        workspace_id=load_or_create_workspace_id(workspace),
        bindings=None,
    )
    config_path = _write_workspace_config(tmp_path, _base_config())
    config = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace,
        instruction_file_resolver=lambda uri: paths.resolve(
            uri, allow_empty=False, reject_symlink=True
        ),
    )
    return FileCase(config, workspace, config_path, paths)


@pytest.mark.parametrize("producer", ["R01", "R02"])
@pytest.mark.asyncio
async def test_file_replaces_inline_and_null_clears_lower_selector(
    file_case: FileCase, producer: str
) -> None:
    case = file_case
    path = case.workspace / "prompt.txt"
    path.write_text("文件中的唯一正文", encoding="utf-8")
    case.select(path, producer)
    before = case.config.get_snapshot()
    key = "system_prompt" if producer == "R01" else "runtime_identity_description"
    assert case.config.get_agent_runtime_config("default")[key] == "文件中的唯一正文"
    higher = case.workspace / ".boxteam/workspace.jsonc"
    fields = (
        {"system_prompt_file": None}
        if producer == "R01"
        else {"runtime_identity": {"description_file": None}}
    )
    higher.write_text(
        json.dumps({"agents": {"default": {"instructions": fields}}}), encoding="utf-8"
    )
    assert await case.config.reload()
    assert case.config.get_revision() != before.revision
    assert case.config.get_agent_runtime_config("default")[key] == (
        "hello" if producer == "R01" else "inline identity"
    )
    await case.config.close()


@pytest.mark.parametrize(
    "failure", ["missing", "invalid_utf8", "directory", "symlink", "empty", "oversize"]
)
@pytest.mark.asyncio
async def test_failed_file_is_explicit_preserves_frozen_body_and_recovers(
    file_case: FileCase, failure: str
) -> None:
    case = file_case
    path = case.workspace / "prompt.txt"
    path.write_text("已发布正文", encoding="utf-8")
    case.select(path)
    before = case.config.get_snapshot()
    path.unlink()
    if failure == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif failure == "empty":
        path.write_text("", encoding="utf-8")
    elif failure == "oversize":
        path.write_text("x" * 30001, encoding="utf-8")
    elif failure == "directory":
        path.mkdir()
    elif failure == "symlink":
        other = case.workspace / "other.txt"
        other.write_text("不可跟随", encoding="utf-8")
        path.symlink_to(other)
    with pytest.raises((RuntimeError, ValueError)):
        await case.config.reload()
    assert case.config.get_snapshot().to_dict() == before.to_dict()
    assert (
        case.config.get_agent_runtime_config("default")["system_prompt"] == "已发布正文"
    )
    with pytest.raises(RuntimeError, match="instruction-source-unavailable"):
        case.config.require_instruction_sources_available("default")
    public = await case.config.get()
    diagnostic = next(
        item
        for item in public.metadata["instruction_inputs"]
        if item["producer_id"] == "R01" and item["agent_id"] == "default"
    )
    assert diagnostic["available"] is False and diagnostic[
        "uri"
    ] == case.paths.reference(path)
    assert str(case.workspace) not in json.dumps(public.metadata)
    if path.is_dir() and not path.is_symlink():
        path.rmdir()
    elif path.exists() or path.is_symlink():
        path.unlink()
    path.write_text("修复后正文", encoding="utf-8")
    assert await case.config.reload()
    case.config.require_instruction_sources_available("default")
    assert (
        case.config.get_agent_runtime_config("default")["system_prompt"] == "修复后正文"
    )
    await case.config.close()


@pytest.mark.asyncio
async def test_cold_active_snapshot_uses_frozen_file_body(file_case: FileCase) -> None:
    case = file_case
    path = case.workspace / "prompt.txt"
    path.write_text("重启前正文", encoding="utf-8")
    case.select(path)
    store = WorkspaceStateStore(workspace_root=case.workspace)
    resolver = case.config._instruction_inputs._resolve_file
    config = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=case.config_path,
        workspace_root=case.workspace,
        workspace_state_store=store,
        instruction_file_resolver=resolver,
    )
    config.validate_workspace_config()
    before = config.get_snapshot()
    await config.close()
    path.unlink()

    def forbidden_read(_uri: str):
        pytest.fail("冷恢复 active snapshot 不得读取当前文件")

    restored = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=case.config_path,
        workspace_root=case.workspace,
        workspace_state_store=store,
        instruction_file_resolver=forbidden_read,
    )
    assert restored.get_snapshot().to_dict() == before.to_dict()
    assert restored.get_agent_runtime_config("default")["system_prompt"] == "重启前正文"
    await restored.close()
    store.close()


async def _wait_for(predicate) -> None:
    async with asyncio.timeout(6):
        while not predicate():
            await asyncio.sleep(0.025)


@pytest.mark.asyncio
async def test_watcher_rebinds_file_selection_and_recovers_missing_parent(
    file_case: FileCase,
) -> None:
    case = file_case
    first = case.workspace / "first.txt"
    first.write_text("第一份", encoding="utf-8")
    case.select(first)
    config = case.config
    config.get_snapshot()
    first.write_text("订阅窗口中的更新", encoding="utf-8")
    await config.start_watching()
    try:
        assert config.get_agent_runtime_config("default")["system_prompt"] == "订阅窗口中的更新"
        first.write_text("后台更新", encoding="utf-8")
        await _wait_for(
            lambda: (
                config.get_agent_runtime_config("default")["system_prompt"]
                == "后台更新"
            )
        )
        second = case.workspace / "new" / "nested" / "second.txt"
        case.select(second)
        await _wait_for(lambda: not config._snapshot_store.status().healthy)
        second.parent.mkdir(parents=True)
        second.write_text("第二份", encoding="utf-8")
        await _wait_for(
            lambda: (
                config.get_agent_runtime_config("default")["system_prompt"] == "第二份"
            )
        )
        assert first.resolve() not in config._watcher._candidate_paths
        revision = config.get_revision()
        first.write_text("旧文件不再生效", encoding="utf-8")
        await asyncio.sleep(0.35)
        assert config.get_revision() == revision
    finally:
        await config.close()


@pytest.mark.parametrize("value", ["/etc/hosts", "relative.txt", "C:\\notes.txt"])
def test_native_file_selector_is_not_a_persisted_locator(
    file_case: FileCase, value: str
) -> None:
    case = file_case
    payload = json.loads(case.config_path.read_text())
    payload["agents"]["default"]["instructions"]["system_prompt_file"] = value
    case.config_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        case.config.get_snapshot()


def test_file_selector_rejects_wrong_target_workspace(file_case: FileCase) -> None:
    case = file_case
    root = resource_display_uri(
        scope="workspace",
        scope_id="00000000-0000-7000-8000-000000000001",
        kind="files",
        tail_segments=("root",),
    )
    payload = json.loads(case.config_path.read_text())
    payload["agents"]["default"]["instructions"]["system_prompt_file"] = file_uri_child(
        root, "prompt.txt"
    )
    case.config_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="工作区"):
        case.config.get_snapshot()


@pytest.mark.asyncio
async def test_failed_candidate_watch_rebind_does_not_replace_the_original_error(
    file_case: FileCase,
) -> None:
    case = file_case
    before = case.config.get_snapshot()
    await case.config.start_watching()
    try:
        payload = json.loads(case.config_path.read_text())
        missing = case.paths.reference(case.workspace / "missing.txt")
        wrong = resource_display_uri(
            scope="workspace",
            scope_id="00000000-0000-7000-8000-000000000001",
            kind="files",
            tail_segments=("root", "wrong.txt"),
        )
        instructions = payload["agents"]["default"]["instructions"]
        instructions["system_prompt_file"] = missing
        instructions["runtime_identity"] = {"description_file": wrong}
        case.config_path.write_text(json.dumps(payload))
        with pytest.raises(RuntimeError, match="来源文件不存在"):
            await case.config.reload()
        assert case.config.get_snapshot().to_dict() == before.to_dict()
    finally:
        await case.config.close()
