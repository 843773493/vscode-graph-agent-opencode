"""配置 owner 的真实文件输入、监听发布、失败和冻结恢复合同。"""

from __future__ import annotations

import asyncio
import json
import logging
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
    with pytest.raises(RuntimeError, match="instruction-source-unavailable") as unavailable:
        case.config.require_instruction_sources_available("default")
    if failure in {"empty", "oversize"}:
        rule = "minLength" if failure == "empty" else "maxLength"
        assert f"rule={rule}" in str(unavailable.value)
    elif failure in {"missing", "invalid_utf8", "directory"}:
        assert str(unavailable.value.__cause__) in str(unavailable.value)
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
    watcher = config._watcher
    assert watcher is not None
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
        # 快照先发布，旧 generation 排空后才重绑；不能将正文可见当作重绑完成。
        await _wait_for(lambda: first.resolve() not in watcher._candidate_paths)
        assert first.resolve() not in watcher._candidate_paths
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


@pytest.fixture(params=[False, True], ids=["logging", "logging-unavailable"])
def instruction_diagnostic_sink(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    blocked = tmp_path / "blocked-log-parent"
    blocked.write_text("不是目录", encoding="utf-8")
    original = logging.Logger.callHandlers

    def call_handlers(logger: logging.Logger, record: logging.LogRecord) -> None:
        original(logger, record)
        if request.param and record.name == "app.core.failures":
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logging.Logger, "callHandlers", call_handlers)
    return bool(request.param), blocked


@pytest.mark.parametrize("producer", ["R01", "R02"])
@pytest.mark.parametrize("fault", ["native-loop", "runtime", "value"])
async def test_readiness_keeps_native_source_diagnostics_out_of_public_errors(
    file_case: FileCase, monkeypatch: pytest.MonkeyPatch, caplog,
    instruction_diagnostic_sink, producer: str, fault: str,
) -> None:
    case = file_case
    directory = case.workspace / "prompts"
    directory.mkdir()
    path = directory / "prompt.txt"
    path.write_text("已发布的原文件正文", encoding="utf-8")
    case.select(path, producer)
    before = case.config.get_snapshot()
    key = "system_prompt" if producer == "R01" else "runtime_identity_description"
    inputs = case.config._instruction_inputs
    resolve = inputs._resolve_file
    assert resolve is not None
    uri = case.paths.reference(path)
    saved = case.workspace / "original-prompts"
    failure = None if fault == "native-loop" else (
        RuntimeError if fault == "runtime" else ValueError
    )(f"instruction-source-unavailable: {path}; C:\\private\\prompt.txt; \\\\server\\share\\prompt.txt")

    def replace_after_resolution(value: str):
        if value == uri and failure is not None:
            raise failure
        result = resolve(value)
        if value == uri:
            directory.rename(saved)
            directory.symlink_to(directory.name, target_is_directory=True)
        return result

    try:
        with monkeypatch.context() as source_fault:
            source_fault.setattr(inputs, "_resolve_file", replace_after_resolution)
            with pytest.raises((RuntimeError, ValueError)) as load_error:
                await case.config.reload()
        original = load_error.value
        if failure is not None:
            assert original is failure
        else:
            assert type(original) is RuntimeError and str(path) in str(original)
        original_args = original.args
        with pytest.raises(RuntimeError, match="instruction-source-unavailable") as unavailable:
            case.config.require_instruction_sources_available("default")
        detail = str(unavailable.value)
        assert str(case.workspace) not in detail and "C:\\private" not in detail
        assert "\\\\server\\share" not in detail and "reason=" not in detail
        assert uri in detail and f"error={type(original).__name__}" in detail
        assert "stage=instruction-source-readiness" in detail
        assert unavailable.value.__cause__ is original and original.args == original_args
        assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
        broken_logging, blocked = instruction_diagnostic_sink
        assert str(blocked) not in detail
        if broken_logging:
            assert "diagnostic_sink_error=NotADirectoryError" in detail
            assert str(blocked) in "\n".join(original.__notes__)
        assert case.config.get_snapshot().to_dict() == before.to_dict()
        assert case.config.get_agent_runtime_config("default")[key] == "已发布的原文件正文"
        if fault == "native-loop":
            directory.unlink()
            saved.rename(directory)
        path.write_text("由原入口恢复的文件正文", encoding="utf-8")
        assert await case.config.reload()
        case.config.require_instruction_sources_available("default")
        assert case.config.get_agent_runtime_config("default")[key] == "由原入口恢复的文件正文"
    finally:
        if directory.is_symlink():
            directory.unlink()
            saved.rename(directory)
        await case.config.close()


@pytest.mark.parametrize(("fault", "reason", "error_type"), [
    ("missing", "提示词文件来源没有绑定目标文件 owner", RuntimeError),
    ("different", "提示词文件 owner 返回了不同资源 URI", ValueError),
])
async def test_readiness_preserves_explicit_source_binding_rules(
    file_case: FileCase, monkeypatch: pytest.MonkeyPatch,
    instruction_diagnostic_sink, fault: str, reason: str,
    error_type: type[RuntimeError | ValueError],
) -> None:
    case = file_case
    source = case.workspace / "prompt.txt"
    source.write_text("已发布的绑定正文", encoding="utf-8")
    case.select(source)
    before = case.config.get_snapshot()
    inputs = case.config._instruction_inputs
    resolve = inputs._resolve_file
    assert resolve is not None

    def different(value: str):
        path, _uri = resolve(value)
        return path, case.paths.reference(case.workspace / "another.txt")

    try:
        with monkeypatch.context() as source_fault:
            source_fault.setattr(inputs, "_resolve_file", None if fault == "missing" else different)
            with pytest.raises(error_type) as load_error:
                await case.config.reload()
        assert type(load_error.value) is error_type and str(load_error.value) == reason
        with pytest.raises(RuntimeError) as unavailable:
            case.config.require_instruction_sources_available("default")
        assert reason in str(unavailable.value) and unavailable.value.__cause__ is load_error.value
        assert case.config.get_snapshot().to_dict() == before.to_dict()
        assert not await case.config.reload()
        case.config.require_instruction_sources_available("default")
    finally:
        await case.config.close()
