"""安装事务、进程归属、受管配置拒绝与显式动态命令的边界。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from typing import cast

import pytest
from langchain_mcp_adapters.sessions import StdioConnection

from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpCandidatePlanDTO,
    McpCandidateProvenanceDTO,
    McpCandidateVariantRefDTO,
    McpInstallationOperationDTO,
    McpInstallationRequestDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
)
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.python import (
    InstallationUnavailable,
    StageCallback,
    artifact_digest,
    python_package,
)
from app.services.infrastructure.mcp.installation.store import InstallationStore
from app.services.infrastructure.mcp.session_runtime import (
    production_mcp_session_factory,
)


@pytest.fixture
def installation_store(tmp_path: Path) -> InstallationStore:
    return InstallationStore(tmp_path / "installations")


@pytest.fixture
def operation() -> McpInstallationOperationDTO:
    return McpInstallationOperationDTO(operation_id="original", intent_digest="same-intent", status="running", stage="install", created_at=1, updated_at=1)


@pytest.fixture
def binding() -> McpManagedLaunchDTO:
    return McpManagedLaunchDTO(backend_id="00000000-0000-4000-8000-000000000001", installation_id="a" * 32,
        generation="b" * 32, artifact_digest="sha256:jcs:v1:" + "c" * 64)


@pytest.fixture
def published_artifact(tmp_path: Path, binding: McpManagedLaunchDTO, operation: McpInstallationOperationDTO) -> tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]:
    home = tmp_path / "home"
    installation_root = home / "artifacts/mcp" / binding.backend_id
    generation = installation_root / "generations" / binding.installation_id / binding.generation
    command = generation / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"published executable")
    dependency = generation / "dependency.py"
    dependency.write_text("VALUE = 'locked'\n", encoding="utf-8")
    artifact = McpInstalledArtifactDTO(binding=binding.model_copy(update={"artifact_digest": artifact_digest(generation)}),
        source=McpCandidateProvenanceDTO(kind="manual", name="server", version="1"), manifest_digest="sha256:jcs:v1:" + "c" * 64,
        variant=McpCandidateVariantRefDTO(kind="package", index=0, digest="sha256:jcs:v1:" + "d" * 64),
        platform=native_mcp_platform(), identifier="server", version="1", entrypoint="server", command=str(command),
        lock_digest="sha256:" + "e" * 64, installed_distributions={"server": "1"})
    store = InstallationStore(installation_root)
    store.reserve(operation)
    store.publish(operation, artifact)
    return McpInstallationOwner(home=home, backend_id=binding.backend_id), artifact, dependency


async def test_original_sdk_launch_rejects_artifact_changed_after_config_parse(published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]) -> None:
    owner, artifact, dependency = published_artifact
    raw = {"transport": "stdio", "managed_launch": artifact.binding.model_dump()}
    server, = parse_mcp_server_configs({"servers": {"managed": raw}}, workspace_root=dependency.parent,
        installation_resolver=owner.resolve)
    assert owner.resolve(artifact.binding, verify=True) == artifact.command
    dependency.write_text("VALUE = 'tampered'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="制品内容已改变"):
        async with production_mcp_session_factory(server, lambda: None, installation_owner=owner):
            pytest.fail("被篡改的 generation 不得进入原 SDK session")
    assert owner.artifacts() == [artifact]
    await owner.close()


def test_unique_intent_survives_another_live_backend_handle(installation_store: InstallationStore, operation: McpInstallationOperationDTO, tmp_path: Path) -> None:
    first, created = installation_store.reserve(operation)
    assert first == operation and created is True
    second = InstallationStore(tmp_path / "installations")
    assert second.operation("original").status == "running"
    assert second.reserve(operation) == (operation, False)
    with pytest.raises(McpCandidateConflict, match="另一目标"):
        second.reserve(operation.model_copy(update={"intent_digest": "another-intent"}))
    assert installation_store.artifacts() == []


def test_only_observed_owner_exit_marks_interrupted(installation_store: InstallationStore, operation: McpInstallationOperationDTO, tmp_path: Path) -> None:
    installation_store.reserve(operation)
    # 只改变持久进程证据为已不可能存活的 PID；不写结果状态。
    with sqlite3.connect(tmp_path / "installations/installations.sqlite") as connection:
        connection.execute("UPDATE operation_owners SET pid=?, started=? WHERE operation_id=?", (2147483647, 0, operation.operation_id))
    observed = installation_store.operation("original")
    assert observed.status == "interrupted" and observed.stage == "install" and observed.artifact is None
    assert installation_store.reserve(operation) == (observed, False)


@pytest.mark.parametrize("status", ["running", "failed", "interrupted"])
def test_late_writes_cannot_retract_published_result(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path], status: str) -> None:
    _, artifact, _ = published_artifact
    installation_store.reserve(operation)
    installation_store.publish(operation, artifact)
    complete = installation_store.operation(operation.operation_id)
    late = operation.model_copy(update={"status": status, "error": "late observer"})
    assert installation_store.update(late) == complete
    assert installation_store.operation(operation.operation_id) == complete
    assert installation_store.artifacts() == [artifact]


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_settled_operation_refuses_late_publication(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path], status: str) -> None:
    _, artifact, _ = published_artifact
    installation_store.reserve(operation)
    terminal = installation_store.update(operation.model_copy(update={"status": status, "error": "original failure"}))
    assert installation_store.update(operation) == terminal
    with pytest.raises(McpCandidateConflict, match="不能迟到发布"):
        installation_store.publish(operation, artifact)
    assert installation_store.operation(operation.operation_id) == terminal
    assert installation_store.artifacts() == []


def test_mutations_cannot_bypass_original_intent_or_publication(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]) -> None:
    _, artifact, _ = published_artifact
    installation_store.reserve(operation)
    wrong_intent = operation.model_copy(update={"intent_digest": "different"})
    with pytest.raises(McpCandidateConflict, match="原操作意图"):
        installation_store.update(wrong_intent)
    with pytest.raises(McpCandidateConflict, match="原操作意图"):
        installation_store.publish(wrong_intent, artifact)
    with pytest.raises(ValueError, match="随制品发布"):
        installation_store.update(operation.model_copy(update={"status": "succeeded"}))
    assert installation_store.operation(operation.operation_id) == operation
    assert installation_store.artifacts() == []


@pytest.fixture
def installation_intent(binding: McpManagedLaunchDTO) -> tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]]:
    source = {"kind": "manual", "name": "server", "version": "1", "definition": {"packages": [
        {"registryType": "pypi", "identifier": "server", "version": "1", "transport": {"type": "stdio"}}]}}
    candidate = {"target": {"gateway_id": "gateway_" + "2" * 32, "backend_id": binding.backend_id},
        "source": source, "manifest_digest": "sha256:jcs:v1:" + "c" * 64,
        "variant": {"kind": "package", "index": 0, "digest": "sha256:jcs:v1:" + "d" * 64}}
    request = McpInstallationRequestDTO.model_validate({"candidate": candidate, "plan_digest": "sha256:jcs:v1:" + "e" * 64,
        "operation_id": "cancel-installation", "entrypoint": "server"})
    parameters: dict[str, object] = {"registry_type": "pypi", "identifier": "server", "version": "1"}
    plan = McpCandidatePlanDTO.model_validate({"source": {key: value for key, value in source.items() if key != "definition"},
        "manifest_digest": candidate["manifest_digest"], "variant": candidate["variant"],
        "platform": native_mcp_platform(), "plan_digest": request.plan_digest, "disposition": "requires_artifact",
        "download_required": True, "parameters": parameters})
    return request, plan, parameters


@pytest.mark.parametrize("stage,expected_status", [("verify", "interrupted"), ("publish", "succeeded")])
async def test_cancellation_waits_for_original_worker_before_settling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]], stage: str, expected_status: str) -> None:
    request, plan, parameters = installation_intent
    entered, release = threading.Event(), threading.Event()
    tasks: list[asyncio.Task[None]] = []

    async def prepare(root: Path, *, name: str, version: str, index: str, entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        command = root / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
        command.parent.mkdir(parents=True)
        command.write_bytes(b"validated executable")
        await progress("verify", "")
        return str(command), "sha256:" + "f" * 64, {name: version}

    original_update, original_publish = InstallationStore.update, InstallationStore.publish

    def update(store: InstallationStore, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        if stage == "verify" and operation.stage == "verify" and operation.status == "running":
            entered.set()
            assert release.wait(10), "测试未释放原进度 worker"
        return original_update(store, operation)

    def publish(store: InstallationStore, operation: McpInstallationOperationDTO, artifact: McpInstalledArtifactDTO) -> None:
        if stage == "publish":
            entered.set()
            assert release.wait(10), "测试未释放原发布 worker"
        original_publish(store, operation, artifact)

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)
    monkeypatch.setattr(InstallationStore, "update", update)
    monkeypatch.setattr(InstallationStore, "publish", publish)
    owner = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    try:
        await owner.install(request, plan=plan, parameters=parameters)
        assert await asyncio.to_thread(entered.wait, 5), "安装没有进入被测持久化边界"
        task, = tasks
        task.cancel("original-installation-cancellation")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        assert owner.operation(request.operation_id).status == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original-installation-cancellation"):
            await task
        observed = owner.operation(request.operation_id)
        assert observed.status == expected_status
        assert (observed.artifact is not None) == (expected_status == "succeeded")
        assert owner.artifacts() == ([observed.artifact] if observed.artifact else [])
        assert await owner.install(request, plan=plan, parameters=parameters) == observed
        assert len(tasks) == 1
    finally:
        release.set()
        await owner.close()


async def test_preparation_error_remains_primary_when_failure_recording_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]]) -> None:
    request, plan, parameters = installation_intent
    original = OSError("原制品准备失败")
    recording = OSError("原操作失败状态写入失败")
    preparing: asyncio.Future[asyncio.Task[None]] = asyncio.get_running_loop().create_future()

    async def prepare(root: Path, *, name: str, version: str, index: str, entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
        task = asyncio.current_task()
        assert task is not None
        preparing.set_result(task)
        raise original

    def update(store: InstallationStore, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        assert operation.status == "failed" and operation.error == str(original)
        raise recording

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)
    monkeypatch.setattr(InstallationStore, "update", update)
    owner = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    await owner.install(request, plan=plan, parameters=parameters)
    with pytest.raises(OSError) as caught:
        await (await preparing)
    assert caught.value is original and caught.value.__cause__ is recording
    assert owner.operation(request.operation_id).status == "running"
    assert owner.artifacts() == []
    await owner.close()


def test_managed_launch_requires_owner_and_cannot_override_command(binding: McpManagedLaunchDTO, tmp_path: Path) -> None:
    raw = {"transport": "stdio", "managed_launch": binding.model_dump(), "args": ["--workspace", "${workspaceRoot}"]}
    with pytest.raises(ValueError, match="目标安装 owner"):
        parse_mcp_server_configs({"servers": {"managed": raw}}, workspace_root=tmp_path)
    seen: list[McpManagedLaunchDTO] = []

    def resolve(reference: McpManagedLaunchDTO) -> str:
        seen.append(reference)
        return "/published/bin/verified-server"

    server, = parse_mcp_server_configs({"servers": {"managed": raw}}, workspace_root=tmp_path, installation_resolver=resolve)
    connection = cast(StdioConnection, server.connection)
    assert server.managed_launch == binding and seen == [binding]
    assert connection["command"] == "/published/bin/verified-server"
    assert connection["args"] == ["--workspace", str(tmp_path)]
    with pytest.raises(ValueError, match="不能另设 command"):
        parse_mcp_server_configs({"servers": {"managed": {**raw, "command": "uvx"}}}, workspace_root=tmp_path, installation_resolver=resolve)
    with pytest.raises(ValueError, match="替换锁定依赖"):
        parse_mcp_server_configs({"servers": {"managed": {**raw, "env": {"PYTHONPATH": "/other"}}}}, workspace_root=tmp_path, installation_resolver=resolve)
    dynamic, = parse_mcp_server_configs({"servers": {"dynamic": {"transport": "stdio", "command": "npx", "args": ["package@latest"]}}}, workspace_root=tmp_path)
    dynamic_connection = cast(StdioConnection, dynamic.connection)
    assert dynamic.managed_launch is None and dynamic_connection["command"] == "npx" and dynamic_connection["args"] == ["package@latest"]


@pytest.mark.parametrize("parameters", [
    {"registry_type": "npm"}, {"registry_type": "oci"}, {"registry_type": "nuget"},
    {"registry_type": "pypi", "runtime_hint": "custom"}, {"registry_type": "pypi", "runtime_arguments": ["--from", "other"]},
    {"registry_type": "pypi", "expected_sha256": "a" * 64},
])
def test_unsupported_installation_never_turns_into_dynamic_launcher(parameters: dict[str, object]) -> None:
    with pytest.raises(InstallationUnavailable):
        python_package({"identifier": "server", "version": "1.0.0", **parameters})
