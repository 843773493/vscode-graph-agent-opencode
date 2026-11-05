"""安装事务、进程归属、受管配置拒绝与显式动态命令的边界。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from langchain_mcp_adapters.sessions import StdioConnection

from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpCandidateProvenanceDTO,
    McpCandidateVariantRefDTO,
    McpInstallationOperationDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
)
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.python import (
    InstallationUnavailable,
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
