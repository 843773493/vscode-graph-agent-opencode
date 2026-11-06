"""安装事务、进程归属、受管配置拒绝与显式动态命令的边界。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import cast

import psutil
import pytest
from langchain_mcp_adapters.sessions import StdioConnection

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
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
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.python import (
    InstallationUnavailable,
    StageCallback,
    artifact_digest,
    python_package,
)
from app.services.infrastructure.mcp.installation.store import (
    InstallationReservation,
    InstallationStore,
    artifact_key,
)
from app.services.infrastructure.mcp.invocation import McpResourceUse
from app.services.infrastructure.mcp.session_runtime import (
    production_mcp_session_factory,
)
from tests.support.mcp.session_doubles import FakeMcpSessionFactory


@pytest.fixture
def installation_store(tmp_path: Path, binding: McpManagedLaunchDTO) -> InstallationStore:
    return InstallationStore(tmp_path / "home/artifacts/mcp" / binding.backend_id)


@pytest.fixture
def operation() -> McpInstallationOperationDTO:
    return McpInstallationOperationDTO(operation_id="original", intent_digest="same-intent", status="running", stage="install", created_at=1, updated_at=1)


@pytest.fixture
def binding() -> McpManagedLaunchDTO:
    return McpManagedLaunchDTO(backend_id="00000000-0000-4000-8000-000000000001", installation_id="a" * 32,
        generation="b" * 32, artifact_digest="sha256:jcs:v1:" + "c" * 64)


@pytest.fixture
def artifact_key_value(installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]]) -> str:
    request, plan, _ = installation_intent
    return artifact_key(plan, identifier="server", version="1", entrypoint=request.entrypoint)


@pytest.fixture
def prepared_artifact(tmp_path: Path, binding: McpManagedLaunchDTO, operation: McpInstallationOperationDTO,
    installation_store: InstallationStore, artifact_key_value: str) -> tuple[McpInstalledArtifactDTO, Path]:
    home = tmp_path / "home"
    installation_root = home / "artifacts/mcp" / binding.backend_id
    preparation = installation_store.reserve(operation, key=artifact_key_value).preparation
    assert preparation is not None
    binding = binding.model_copy(update={"installation_id": preparation.installation_id, "generation": preparation.generation})
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
    return artifact, dependency


@pytest.fixture
def published_artifact(tmp_path: Path, operation: McpInstallationOperationDTO, installation_store: InstallationStore,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path]) -> tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]:
    artifact, dependency = prepared_artifact
    installation_store.publish(operation, artifact)
    return McpInstallationOwner(home=tmp_path / "home", backend_id=artifact.binding.backend_id), artifact, dependency


async def test_original_sdk_launch_rejects_artifact_changed_after_config_parse(published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path], mcp_session_lifecycle) -> None:
    owner, artifact, dependency = published_artifact
    raw = {"transport": "stdio", "managed_launch": artifact.binding.model_dump()}
    server, = parse_mcp_server_configs({"servers": {"managed": raw}}, workspace_root=dependency.parent,
        installation_resolver=owner.artifact)
    assert owner.resolve(artifact.binding, verify=True) == artifact.command
    dependency.write_text("VALUE = 'tampered'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="制品内容已改变"):
        async with production_mcp_session_factory(server, lambda: None, mcp_session_lifecycle, installation_owner=owner):
            pytest.fail("被篡改的 generation 不得进入原 SDK session")
    assert owner.artifacts() == [artifact]
    await owner.close()


def test_unique_intent_survives_another_live_backend_handle(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    tmp_path: Path, binding: McpManagedLaunchDTO, artifact_key_value: str) -> None:
    first = installation_store.reserve(operation, key=artifact_key_value)
    assert first.operation == operation and first.preparation is not None and first.artifact is None
    second = InstallationStore(tmp_path / "home/artifacts/mcp" / binding.backend_id)
    assert second.operation("original").status == "running"
    assert second.reserve(operation, key=artifact_key_value) == InstallationReservation(operation)
    with pytest.raises(McpCandidateConflict, match="另一目标"):
        second.reserve(operation.model_copy(update={"intent_digest": "another-intent"}), key=artifact_key_value)
    with pytest.raises(McpCandidateConflict, match="另一目标"):
        second.reserve(operation, key="different-artifact")
    assert installation_store.artifacts() == []


def test_only_observed_owner_exit_marks_interrupted(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    tmp_path: Path, binding: McpManagedLaunchDTO, artifact_key_value: str) -> None:
    installation_store.reserve(operation, key=artifact_key_value)
    # 只改变持久进程证据为已不可能存活的 PID；不写结果状态。
    with sqlite3.connect(tmp_path / "home/artifacts/mcp" / binding.backend_id / "installations.sqlite") as connection:
        connection.execute("UPDATE operation_owners SET pid=?, started=? WHERE operation_id=?", (2147483647, 0, operation.operation_id))
    observed = installation_store.operation("original")
    assert observed.status == "interrupted" and observed.stage == "install" and observed.artifact is None
    assert installation_store.reserve(operation, key=artifact_key_value) == InstallationReservation(observed)


@pytest.mark.parametrize("status", ["running", "failed", "interrupted"])
def test_late_writes_cannot_retract_published_result(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], status: str) -> None:
    artifact, _ = prepared_artifact
    installation_store.publish(operation, artifact)
    complete = installation_store.operation(operation.operation_id)
    late = operation.model_copy(update={"status": status, "error": "late observer"})
    assert installation_store.update(late) == complete
    assert installation_store.operation(operation.operation_id) == complete
    assert installation_store.artifacts() == [artifact]


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_settled_operation_refuses_late_publication(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], status: str) -> None:
    artifact, _ = prepared_artifact
    terminal = installation_store.update(operation.model_copy(update={"status": status, "error": "original failure"}))
    assert installation_store.update(operation) == terminal
    with pytest.raises(McpCandidateConflict, match="不能迟到发布"):
        installation_store.publish(operation, artifact)
    assert installation_store.operation(operation.operation_id) == terminal
    assert installation_store.artifacts() == []


def test_mutations_cannot_bypass_original_intent_or_publication(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path]) -> None:
    artifact, _ = prepared_artifact
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
    observer = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    follower_request = request.model_copy(update={"operation_id": "shielded-follower"})
    try:
        await owner.install(request, plan=plan, parameters=parameters)
        assert await asyncio.to_thread(entered.wait, 5), "安装没有进入被测持久化边界"
        follower = await observer.install(follower_request, plan=plan, parameters=parameters)
        assert follower.status == "running"
        task, = tasks
        task.cancel("original-installation-cancellation")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        assert owner.operation(request.operation_id).status == "running"
        assert observer.operation(follower.operation_id).status == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original-installation-cancellation"):
            await task
        observed = owner.operation(request.operation_id)
        followed = observer.operation(follower.operation_id)
        assert observed.status == expected_status
        assert followed.status == observed.status and followed.artifact == observed.artifact
        assert followed.stage == observed.stage and followed.log == observed.log and followed.error == observed.error
        assert followed.intent_digest == follower.intent_digest and followed.created_at == follower.created_at
        assert (observed.artifact is not None) == (expected_status == "succeeded")
        assert owner.artifacts() == ([observed.artifact] if observed.artifact else [])
        assert await owner.install(request, plan=plan, parameters=parameters) == observed
        assert await observer.install(follower_request, plan=plan, parameters=parameters) == followed
        assert len(tasks) == 1
    finally:
        release.set()
        await owner.close()
        await observer.close()


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


async def _completed_installation(owner: McpInstallationOwner, operation_id: str) -> McpInstallationOperationDTO:
    async with asyncio.timeout(3):
        while (operation := owner.operation(operation_id)).status == "running":
            await asyncio.sleep(0.01)
    return operation


@pytest.fixture
def no_artifact_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def prepare(root: Path, *, name: str, version: str, index: str, entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
        pytest.fail("暖复用不得重新准备或下载制品")

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)


@pytest.mark.usefixtures("no_artifact_preparation")
async def test_warm_reuse_keeps_independent_operation_intent_and_exact_binding(
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path],
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]],
) -> None:
    owner, artifact, dependency = published_artifact
    request, plan, parameters = installation_intent
    original = owner.operation("original")
    first = await owner.install(request, plan=plan, parameters={**parameters, "arguments": ["A1"], "env": {"WORKSPACE": "A1"}})
    completed = await _completed_installation(owner, first.operation_id)
    assert completed.status == "succeeded" and completed.artifact == artifact
    changed_digest = "sha256:jcs:v1:" + "f" * 64
    second_request = request.model_copy(update={"operation_id": "warm-A2", "plan_digest": changed_digest})
    second_plan = plan.model_copy(update={"plan_digest": changed_digest, "parameters": {**parameters, "arguments": ["A2"], "env": {"WORKSPACE": "A2"}}})
    second = await owner.install(second_request, plan=second_plan, parameters=second_plan.parameters)
    second_completed = await _completed_installation(owner, second.operation_id)
    assert second_completed.status == "succeeded" and second_completed.artifact == artifact
    assert second_completed.operation_id != completed.operation_id and second_completed.intent_digest != completed.intent_digest
    assert second_completed.created_at > original.created_at
    assert owner.operation("original") == original
    assert owner.artifacts() == [artifact] and dependency.read_text(encoding="utf-8") == "VALUE = 'locked'\n"
    assert await owner.install(second_request, plan=second_plan, parameters=second_plan.parameters) == second_completed
    await owner.close()


@pytest.mark.parametrize("field", ["source", "manifest_digest", "variant", "platform", "identifier", "version", "entrypoint"])
def test_warm_reuse_requires_exact_artifact_identity(
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path],
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]], tmp_path: Path,
    operation: McpInstallationOperationDTO, field: str,
) -> None:
    _, artifact, _ = published_artifact
    _, plan, _ = installation_intent
    values: dict[str, object] = {"identifier": artifact.identifier, "version": artifact.version, "entrypoint": artifact.entrypoint}
    changed = plan.model_dump()
    if field == "source":
        changed["source"] = {**plan.source.model_dump(), "name": "another-source"}
    elif field == "manifest_digest":
        changed[field] = "sha256:jcs:v1:" + "0" * 64
    elif field == "variant":
        changed[field] = {**plan.variant.model_dump(), "index": 1}
    elif field == "platform":
        changed[field] = {**plan.platform.model_dump(), "architecture": "another-architecture"}
    else:
        values[field] = "2" if field == "version" else "another"
    store = InstallationStore(tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id)
    reservation = store.reserve(operation.model_copy(update={"operation_id": "different-artifact"}),
        key=artifact_key(McpCandidatePlanDTO.model_validate(changed), identifier=str(values["identifier"]),
            version=str(values["version"]), entrypoint=str(values["entrypoint"])))
    assert reservation.artifact is None and reservation.preparation is not None
    assert store.artifacts() == [artifact]


@pytest.mark.usefixtures("no_artifact_preparation")
@pytest.mark.parametrize("failure", ["tampered", "missing"])
async def test_warm_reuse_fails_without_rebuilding_or_changing_original_artifact(
    published_artifact: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path],
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]], failure: str,
) -> None:
    owner, artifact, dependency = published_artifact
    request, plan, parameters = installation_intent
    if failure == "tampered":
        dependency.write_text("VALUE = 'changed'\n", encoding="utf-8")
        message = "制品内容已改变"
    else:
        Path(artifact.command).unlink()
        message = "制品发布路径丢失"
    original = owner.operation("original")
    await owner.install(request, plan=plan, parameters=parameters)
    observed = await _completed_installation(owner, request.operation_id)
    assert observed.status == "failed" and observed.artifact is None and message in (observed.error or "")
    assert owner.operation("original") == original and owner.artifacts() == [artifact]
    await owner.close()


def test_reuse_publication_cannot_rewrite_existing_artifact(installation_store: InstallationStore, operation: McpInstallationOperationDTO,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], artifact_key_value: str) -> None:
    artifact, _ = prepared_artifact
    installation_store.publish(operation, artifact)
    next_operation = operation.model_copy(update={"operation_id": "warm-operation"})
    reservation = installation_store.reserve(next_operation, key=artifact_key_value)
    assert reservation.artifact == artifact and reservation.preparation is None
    with pytest.raises(McpCandidateConflict, match="不能改写原 generation"):
        installation_store.publish(next_operation, artifact.model_copy(update={"lock_digest": "sha256:" + "0" * 64}))
    assert installation_store.operation(next_operation.operation_id) == next_operation
    assert installation_store.artifacts() == [artifact]


def test_two_store_transactions_claim_only_one_cold_preparation(tmp_path: Path,
    operation: McpInstallationOperationDTO, artifact_key_value: str) -> None:
    root = tmp_path / "contended"
    stores = [InstallationStore(root), InstallationStore(root)]
    barrier = threading.Barrier(2)

    def reserve(index: int) -> InstallationReservation:
        barrier.wait(timeout=5)
        return stores[index].reserve(operation.model_copy(update={"operation_id": f"parallel-{index}",
            "intent_digest": f"independent-{index}", "created_at": index + 1}), key=artifact_key_value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))
    producer, = [result for result in results if result.preparation is not None]
    follower, = [result for result in results if result.preparation is None]
    assert producer.artifact is None and follower.artifact is None
    assert producer.operation.operation_id != follower.operation.operation_id
    progress = stores[0].update(producer.operation.model_copy(update={"stage": "lock", "log": "原执行者有界进度\n", "updated_at": 3}))
    observed = stores[1].operation(follower.operation.operation_id)
    assert observed.stage == progress.stage and observed.log == progress.log
    assert observed.intent_digest == follower.operation.intent_digest and observed.created_at == follower.operation.created_at
    with sqlite3.connect(root / "installations.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM artifact_preparations").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM operation_owners").fetchone() == (1,)
        assert connection.execute("SELECT producer_operation_id FROM operations").fetchall() == [(producer.operation.operation_id,)] * 2
    assert stores[0].artifacts() == []


def test_v2_migration_adds_only_workspace_locators(installation_store: InstallationStore,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], operation: McpInstallationOperationDTO, tmp_path: Path) -> None:
    artifact, _ = prepared_artifact
    installation_store.publish(operation, artifact)
    original = installation_store.operation(operation.operation_id)
    root = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id
    with sqlite3.connect(root / "installations.sqlite") as connection:
        connection.execute("DROP TABLE workspace_locators")
        connection.execute("PRAGMA user_version=2")
    upgraded = InstallationStore(root)
    assert upgraded.operation(operation.operation_id) == original and upgraded.artifacts() == [artifact]
    assert upgraded.workspaces() == []
    with sqlite3.connect(root / "installations.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)


def test_follower_observes_original_process_and_cannot_write_or_publish(installation_store: InstallationStore,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], operation: McpInstallationOperationDTO,
    artifact_key_value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, _ = prepared_artifact
    root = tmp_path / "home/artifacts/mcp" / artifact.binding.backend_id
    other = InstallationStore(root)
    # 等待者发起进程的创建时间不参与准备进程归属；账本也不为它登记执行 owner。
    monkeypatch.setattr(other, "_process_started", 0)
    follower = operation.model_copy(update={"operation_id": "follower", "intent_digest": "follower-intent", "created_at": 2})
    assert other.reserve(follower, key=artifact_key_value).preparation is None
    assert other.operation("follower").status == "running"
    with pytest.raises(McpCandidateConflict, match="等待操作不持有"):
        other.update(follower.model_copy(update={"status": "failed"}))
    with pytest.raises(McpCandidateConflict, match="等待操作不持有"):
        other.publish(follower, artifact)
    with sqlite3.connect(root / "installations.sqlite") as connection:
        assert connection.execute("SELECT operation_id FROM operation_owners").fetchall() == [(operation.operation_id,)]
        connection.execute("UPDATE operation_owners SET started=0 WHERE operation_id=?", (operation.operation_id,))
    observed = other.operation("follower")
    original = installation_store.operation(operation.operation_id)
    assert observed.status == original.status == "interrupted" and observed.error == original.error
    assert observed.stage == original.stage == operation.stage and observed.log == original.log == operation.log
    assert artifact.command and Path(artifact.command).is_file() and installation_store.artifacts() == []
    assert other.reserve(follower, key=artifact_key_value) == InstallationReservation(observed)
    with pytest.raises(McpCandidateConflict, match="不能迟到发布"):
        installation_store.publish(operation, artifact)
    recovered = installation_store.reserve(operation.model_copy(update={"operation_id": "explicit-recovery"}), key=artifact_key_value)
    assert recovered.preparation is not None
    assert (recovered.preparation.installation_id, recovered.preparation.generation) != (artifact.binding.installation_id, artifact.binding.generation)
    assert other.operation("follower") == observed and installation_store.operation(operation.operation_id) == original


def test_missing_process_evidence_cannot_interrupt_or_reassign_original_preparation(installation_store: InstallationStore,
    operation: McpInstallationOperationDTO, artifact_key_value: str, tmp_path: Path, binding: McpManagedLaunchDTO) -> None:
    original = installation_store.reserve(operation, key=artifact_key_value)
    assert original.preparation is not None
    database = tmp_path / "home/artifacts/mcp" / binding.backend_id / "installations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM operation_owners")
    with pytest.raises(McpCandidateConflict, match="缺少原进程.*不能推定"):
        installation_store.operation(operation.operation_id)
    with pytest.raises(McpCandidateConflict, match="缺少原进程.*不能推定"):
        installation_store.reserve(operation.model_copy(update={"operation_id": "another"}), key=artifact_key_value)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT payload FROM operations").fetchall() == [(operation.model_dump_json(),)]
        assert connection.execute("SELECT installation_id, generation FROM artifact_preparations").fetchone() == (
            original.preparation.installation_id, original.preparation.generation)


@pytest.mark.parametrize("field", ["key", "installation_id", "generation"])
def test_publication_requires_original_artifact_key_and_reserved_path(installation_store: InstallationStore,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], operation: McpInstallationOperationDTO, field: str) -> None:
    artifact, _ = prepared_artifact
    changed = artifact.model_copy(update={"entrypoint": "another"}) if field == "key" else artifact.model_copy(update={
        "binding": artifact.binding.model_copy(update={field: "0" * 32})})
    with pytest.raises(McpCandidateConflict, match="原准备身份|原事务预留"):
        installation_store.publish(operation, changed)
    assert installation_store.operation(operation.operation_id) == operation and installation_store.artifacts() == []


async def test_cold_owner_reuses_original_worker_progress_and_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]]) -> None:
    request, plan, parameters = installation_intent
    entered, advance, progressed, release = (asyncio.Event() for _ in range(4))
    roots: list[Path] = []

    async def prepare(root: Path, *, name: str, version: str, index: str, entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
        roots.append(root)
        await progress("lock", "原执行者开始锁定\n")
        entered.set()
        await advance.wait()
        await progress("install", "原执行者实际准备\n")
        progressed.set()
        await release.wait()
        command = root / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
        command.parent.mkdir(parents=True)
        command.write_bytes(b"validated executable")
        return str(command), "sha256:" + "f" * 64, {name: version}

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)
    home = tmp_path / "home"
    first = McpInstallationOwner(home=home, backend_id=request.candidate.target.backend_id)
    second = McpInstallationOwner(home=home, backend_id=request.candidate.target.backend_id)
    second_request = request.model_copy(update={"operation_id": "cold-A2", "plan_digest": "sha256:jcs:v1:" + "f" * 64})
    second_plan = plan.model_copy(update={"plan_digest": second_request.plan_digest,
        "parameters": {**parameters, "arguments": ["A2"], "env": {"WORKSPACE": "A2"}}})
    try:
        original = await first.install(request, plan=plan, parameters=parameters)
        await asyncio.wait_for(entered.wait(), 3)
        follower = await second.install(second_request, plan=second_plan, parameters=second_plan.parameters)
        assert follower.stage == "lock" and follower.log == "原执行者开始锁定\n" and len(roots) == 1
        assert follower.operation_id != original.operation_id and follower.intent_digest != original.intent_digest
        advance.set()
        await asyncio.wait_for(progressed.wait(), 3)
        observed = second.operation(follower.operation_id)
        assert observed.stage == "install" and observed.log == "原执行者开始锁定\n原执行者实际准备\n"
        release.set()
        completed = await _completed_installation(first, original.operation_id)
        reused = await _completed_installation(second, follower.operation_id)
        assert completed.status == reused.status == "succeeded" and completed.artifact is not None
        assert reused.artifact == completed.artifact and reused.artifact is not None and completed.log == reused.log
        assert reused.operation_id == follower.operation_id and reused.intent_digest == follower.intent_digest
        assert reused.created_at == follower.created_at and completed.created_at == original.created_at
        assert first.artifacts() == second.artifacts() == [completed.artifact]
        assert roots == [home / "artifacts/mcp" / first.backend_id / "generations" /
            completed.artifact.binding.installation_id / completed.artifact.binding.generation]
        assert second.resolve(reused.artifact.binding, verify=True) == completed.artifact.command
        assert await second.install(second_request, plan=second_plan, parameters=second_plan.parameters) == reused
        assert len(roots) == 1
    finally:
        advance.set()
        release.set()
        await first.close()
        await second.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_cold_failure_settles_followers_and_only_new_intent_can_recover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    installation_intent: tuple[McpInstallationRequestDTO, McpCandidatePlanDTO, dict[str, object]], cancelled: bool) -> None:
    request, plan, parameters = installation_intent
    entered, release = asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    roots: list[Path] = []
    failing = True

    async def prepare(root: Path, *, name: str, version: str, index: str, entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        roots.append(root)
        root.mkdir(parents=True)
        (root / "unpublished").write_text("原未发布目录", encoding="utf-8")
        await progress("install", "已确认的原准备阶段\n")
        entered.set()
        await release.wait()
        if failing:
            raise OSError("原 index 下载失败")
        command = root / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
        command.parent.mkdir(parents=True)
        command.write_bytes(b"validated executable")
        return str(command), "sha256:" + "f" * 64, {name: version}

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.prepare_python_artifact", prepare)
    first = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    second = McpInstallationOwner(home=tmp_path / "home", backend_id=request.candidate.target.backend_id)
    follower_request = request.model_copy(update={"operation_id": "failed-follower"})
    try:
        await first.install(request, plan=plan, parameters=parameters)
        await asyncio.wait_for(entered.wait(), 3)
        await second.install(follower_request, plan=plan, parameters=parameters)
        if cancelled:
            tasks[0].cancel("原安装被取消")
            with pytest.raises(asyncio.CancelledError, match="原安装被取消"):
                await tasks[0]
        else:
            release.set()
            await tasks[0]
        original, follower = first.operation(request.operation_id), second.operation(follower_request.operation_id)
        assert original.status == follower.status == ("interrupted" if cancelled else "failed")
        assert original.stage == follower.stage == "install" and original.log == follower.log == "已确认的原准备阶段\n"
        assert original.error == follower.error == ("原安装被取消" if cancelled else "原 index 下载失败")
        assert original.artifact is follower.artifact is None and first.artifacts() == []
        assert await first.install(request, plan=plan, parameters=parameters) == original
        assert await second.install(follower_request, plan=plan, parameters=parameters) == follower
        assert len(tasks) == 1 and (roots[0] / "unpublished").is_file()
        failing = False
        release.set()
        recovery_request = request.model_copy(update={"operation_id": "new-explicit-recovery"})
        await second.install(recovery_request, plan=plan, parameters=parameters)
        recovered = await _completed_installation(second, recovery_request.operation_id)
        assert recovered.status == "succeeded" and len(tasks) == 2 and roots[0] != roots[1]
        assert (roots[0] / "unpublished").is_file()
        assert first.operation(request.operation_id) == original and second.operation(follower_request.operation_id) == follower
    finally:
        release.set()
        await first.close()
        await second.close()


LegacyLedgerFactory = Callable[[list[McpInstallationOperationDTO], list[McpInstalledArtifactDTO], dict[str, tuple[int, float]]], Path]


@pytest.fixture
def v1_ledger(tmp_path: Path) -> LegacyLedgerFactory:
    def create(operations: list[McpInstallationOperationDTO], artifacts: list[McpInstalledArtifactDTO], owners: dict[str, tuple[int, float]]) -> Path:
        root = tmp_path / "legacy"
        root.mkdir()
        with sqlite3.connect(root / "installations.sqlite") as connection:
            connection.executescript("""
                CREATE TABLE operations (operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE artifacts (installation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE operation_owners (operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id), pid INTEGER NOT NULL, started REAL NOT NULL);
                PRAGMA user_version=1;
            """)
            connection.executemany("INSERT INTO operations VALUES (?, ?)", [(operation.operation_id, operation.model_dump_json()) for operation in operations])
            connection.executemany("INSERT INTO artifacts VALUES (?, ?)", [(artifact.binding.installation_id, artifact.model_dump_json()) for artifact in artifacts])
            connection.executemany("INSERT INTO operation_owners VALUES (?, ?, ?)", [(operation_id, *owner) for operation_id, owner in owners.items()])
        return root

    return create


def test_v1_migration_keys_exact_artifacts_and_keeps_terminal_intents(v1_ledger: LegacyLedgerFactory,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], operation: McpInstallationOperationDTO, artifact_key_value: str) -> None:
    artifact, _ = prepared_artifact
    completed = operation.model_copy(update={"status": "succeeded", "stage": "complete", "artifact": artifact})
    failed = operation.model_copy(update={"operation_id": "failed", "status": "failed", "error": "原失败", "log": "原日志"})
    interrupted = operation.model_copy(update={"operation_id": "interrupted", "status": "interrupted", "error": "原中断"})
    root = v1_ledger([completed, failed, interrupted], [artifact], {})
    store = InstallationStore(root)
    assert store.artifacts() == [artifact]
    assert [store.operation(item.operation_id) for item in (completed, failed, interrupted)] == [completed, failed, interrupted]
    for terminal in (completed, failed, interrupted):
        assert store.reserve(terminal, key=artifact_key_value) == InstallationReservation(terminal)
    warm = store.reserve(operation.model_copy(update={"operation_id": "new-warm-intent"}), key=artifact_key_value)
    assert warm.artifact == artifact and warm.preparation is None
    with sqlite3.connect(root / "installations.sqlite") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("SELECT artifact_key FROM artifacts").fetchall() == [(artifact_key_value,)]
        assert connection.execute("SELECT producer_operation_id FROM operations WHERE operation_id='original'").fetchone() == (None,)


def test_v1_live_preparation_refuses_migration_without_changing_original(v1_ledger: LegacyLedgerFactory, operation: McpInstallationOperationDTO) -> None:
    root = v1_ledger([operation], [], {operation.operation_id: (os.getpid(), psutil.Process().create_time())})
    path = root / "installations.sqlite"
    original = path.read_bytes()
    with pytest.raises(McpCandidateConflict, match="活进程安装.*original"):
        InstallationStore(root)
    assert path.read_bytes() == original


def test_v1_missing_process_evidence_refuses_migration_without_guessing_exit(v1_ledger: LegacyLedgerFactory,
    operation: McpInstallationOperationDTO) -> None:
    root = v1_ledger([operation], [], {})
    path = root / "installations.sqlite"
    original = path.read_bytes()
    with pytest.raises(McpCandidateConflict, match="缺少原进程.*不能推定"):
        InstallationStore(root)
    assert path.read_bytes() == original


def test_v1_dead_preparation_is_interrupted_without_replay(v1_ledger: LegacyLedgerFactory, operation: McpInstallationOperationDTO, artifact_key_value: str) -> None:
    operation = operation.model_copy(update={"stage": "verify", "log": "已验证阶段原日志"})
    root = v1_ledger([operation], [], {operation.operation_id: (2147483647, 0)})
    unpublished = root / "unpublished"
    unpublished.write_text("待核实", encoding="utf-8")
    store = InstallationStore(root)
    observed = store.operation(operation.operation_id)
    assert observed.status == "interrupted" and observed.stage == "verify" and observed.log == operation.log and observed.artifact is None
    assert store.reserve(operation, key=artifact_key_value) == InstallationReservation(observed)
    assert store.artifacts() == [] and unpublished.read_text(encoding="utf-8") == "待核实"
    recovery = store.reserve(operation.model_copy(update={"operation_id": "new-intent"}), key=artifact_key_value)
    assert recovery.preparation is not None and store.operation(operation.operation_id) == observed


def test_v1_duplicate_exact_generations_refuse_migration_and_keep_originals(v1_ledger: LegacyLedgerFactory,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path], tmp_path: Path) -> None:
    artifact, dependency = prepared_artifact
    destination = tmp_path / "duplicate-generation"
    shutil.copytree(dependency.parent, destination)
    duplicate = artifact.model_copy(update={"binding": artifact.binding.model_copy(update={"installation_id": "e" * 32, "generation": "f" * 32}),
        "command": str(destination / Path(artifact.command).relative_to(dependency.parent))})
    third = duplicate.model_copy(update={"binding": duplicate.binding.model_copy(update={"installation_id": "9" * 32, "generation": "8" * 32})})
    root = v1_ledger([], [artifact, duplicate, third], {})
    path = root / "installations.sqlite"
    original = path.read_bytes()
    with pytest.raises(McpCandidateConflict, match="多个已发布 generation") as caught:
        InstallationStore(root)
    assert all(item.binding.installation_id in str(caught.value) for item in (artifact, duplicate, third))
    assert path.read_bytes() == original and dependency.is_file() and Path(duplicate.command).is_file()


@pytest.mark.parametrize("corruption", ["missing-artifact", "changed-artifact", "missing-result"])
def test_v1_success_must_match_original_publication(v1_ledger: LegacyLedgerFactory, prepared_artifact: tuple[McpInstalledArtifactDTO, Path],
    operation: McpInstallationOperationDTO, corruption: str) -> None:
    artifact, _ = prepared_artifact
    result = artifact.model_copy(update={"lock_digest": "sha256:" + "0" * 64}) if corruption == "changed-artifact" else artifact
    completed = operation.model_copy(update={"status": "succeeded", "stage": "complete", "artifact": None if corruption == "missing-result" else result})
    root = v1_ledger([completed], [] if corruption == "missing-artifact" else [artifact], {})
    path = root / "installations.sqlite"
    original = path.read_bytes()
    with pytest.raises(McpCandidateConflict, match="成功操作与已发布制品不一致"):
        InstallationStore(root)
    assert path.read_bytes() == original


def test_v1_schema_rebuild_rolls_back_as_one_transaction(v1_ledger: LegacyLedgerFactory, operation: McpInstallationOperationDTO,
    monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = operation.model_copy(update={"status": "failed", "error": "原失败"})
    root = v1_ledger([terminal], [], {})
    path = root / "installations.sqlite"
    original = path.read_bytes()
    execute_sql = InstallationStore._execute_sql

    def reject_after_ddl(store: InstallationStore, connection: sqlite3.Connection, resource: str) -> None:
        execute_sql(store, connection, resource)
        if resource == "schema.sql":
            raise OSError("迁移提交前失败")

    monkeypatch.setattr(InstallationStore, "_execute_sql", reject_after_ddl)
    with pytest.raises(OSError, match="迁移提交前失败"):
        InstallationStore(root)
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT payload FROM operations").fetchone() == (terminal.model_dump_json(),)


def test_managed_launch_requires_owner_and_cannot_override_command(binding: McpManagedLaunchDTO, tmp_path: Path,
    prepared_artifact: tuple[McpInstalledArtifactDTO, Path]) -> None:
    raw = {"transport": "stdio", "managed_launch": binding.model_dump(), "args": ["--workspace", "${workspaceRoot}"]}
    with pytest.raises(ValueError, match="目标安装 owner"):
        parse_mcp_server_configs({"servers": {"managed": raw}}, workspace_root=tmp_path)
    seen: list[McpManagedLaunchDTO] = []

    def resolve(reference: McpManagedLaunchDTO) -> McpInstalledArtifactDTO:
        seen.append(reference)
        return prepared_artifact[0].model_copy(update={"binding": reference, "command": "/published/bin/verified-server"})

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


@pytest.fixture
async def managed_switch_case(published_artifact, installation_store, event_channel_service, tmp_path):
    installation, first, _ = published_artifact
    operation = McpInstallationOperationDTO(operation_id="upgrade", intent_digest="upgrade-intent",
        status="running", stage="install", created_at=2, updated_at=2)
    preparation = installation_store.reserve(operation,
        key=artifact_key(first, identifier=first.identifier, version="2", entrypoint=first.entrypoint)).preparation
    assert preparation is not None
    generation = installation._root / "generations" / preparation.installation_id / preparation.generation
    command = generation / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"next published executable")
    second = first.model_copy(update={"binding": first.binding.model_copy(update={
        "installation_id": preparation.installation_id, "generation": preparation.generation,
        "artifact_digest": artifact_digest(generation)}), "command": str(command), "version": "2"})
    installation_store.publish(operation, second)
    payloads = [{"servers": {"managed": {"enabled": True, "transport": "stdio",
        "managed_launch": artifact.binding.model_dump()}}} for artifact in (first, second)]
    ledger = ExternalResourceLeaseLedger()
    factory = FakeMcpSessionFactory({"managed": ["echo"]})
    owner = McpCatalogOwner(raw_config=payloads[0], workspace_root=tmp_path, event_service=event_channel_service,
        leases=ledger, session_factory=factory, installation_owner=installation)
    await owner.start()
    try:
        yield owner, ledger, payloads
    finally:
        await owner.shutdown()
        await installation.close()


@pytest.mark.parametrize("state", ["prepared", "invocation", "unknown", "cleanup_failed"])
async def test_managed_version_rejects_unsettled_connection_before_any_side_effect(managed_switch_case, state):
    owner, ledger, payloads = managed_switch_case
    binding = owner.binding_snapshot()
    target = binding.resolve("mcp__managed__echo")
    runtime = owner._runtimes["managed"]
    connection_ref = target.connection_ref
    operation = software_tool_operation_id("mcp_call", "session", "thread", "model", "call")
    use = McpResourceUse("holder", operation, "session", "thread", "model")
    effects = []
    scope = LifetimeScope("managed-version-rejection")
    with owner.execution_handles.reserve_model_call(binding, session_id="session", thread_id="thread", model_call_id="model") as retain:
        if state != "cleanup_failed":
            retain([{"name": "invoke_extension_tool", "id": "call", "args": {"tool_name": "mcp__managed__echo"}}])
    if state == "cleanup_failed":
        ledger.update_external_state(connection_ref, lifetime_scope="workspace", status="running", cleanup_failed=True)
    invocation = owner.execution_handles.execution_target(target, use) if state in {"invocation", "unknown"} else nullcontext()
    with invocation as execution:
        if execution is not None:
            execution.started = True
        if state != "unknown":
            with pytest.raises(ConfigConflictError, match="未结清"), owner.guard_configuration(payloads[1]):
                pytest.fail("未结旧版本不能进入来源 writer")
            with pytest.raises(ConfigConflictError, match="未结清"):
                await owner.prepare_config(payloads[1], scope, effects.append)
            assert owner._runtimes["managed"] is runtime and runtime.running
            assert owner.binding_snapshot() == binding and effects == []
        if state == "invocation":
            assert execution is not None
            assert await execution.tool.ainvoke({"text": "old"}) == "echo:old"
            execution.result_confirmed = True
    if state == "unknown":
        assert any(lease.status == "reconcile_required" for lease in ledger.leases_for_resource(connection_ref))
        with pytest.raises(ConfigConflictError, match="未结清"):
            await owner.prepare_config(payloads[1], scope, effects.append)
        assert owner._runtimes["managed"] is runtime and runtime.running
        assert owner.binding_snapshot() == binding and effects == []
    await scope.close()


async def test_managed_version_freezes_new_qualification_until_explicit_guard_finishes(managed_switch_case):
    owner, ledger, payloads = managed_switch_case
    binding = owner.binding_snapshot()

    def reserve(model_id):
        return owner.execution_handles.reserve_model_call(binding, session_id="session", thread_id="thread", model_call_id=model_id)

    with pytest.raises(RuntimeError, match="来源保存失败"), owner.guard_configuration(payloads[1]):
        with owner.guard_configuration(payloads[1]), pytest.raises(ExtensionTargetResolutionError), reserve("during-nested-guard"):
            pytest.fail("配置事务期间不能增加旧连接资格")
        with pytest.raises(ExtensionTargetResolutionError), reserve("after-nested-guard"):
            pytest.fail("内层退出不能提前释放外层准入屏障")
        raise RuntimeError("来源保存失败")
    with reserve("after-failure") as retain:
        retain([])
    assert ledger.leases_for_resource(binding.resolve("mcp__managed__echo").connection_ref) == []
    scope = LifetimeScope("managed-version-publication")
    try:
        publish, _ = await owner.prepare_config(payloads[1], scope, lambda _effect: None)
        publish()
        assert owner.server_configuration("managed").managed_launch.model_dump() == payloads[1]["servers"]["managed"]["managed_launch"]
        with owner.execution_handles.reserve_model_call(owner.binding_snapshot(), session_id="session", thread_id="thread", model_call_id="new-version") as retain:
            retain([])
    finally:
        await scope.close()
