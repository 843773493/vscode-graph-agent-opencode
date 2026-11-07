"""npm 受管安装的原生态入口、冻结 Node 与配置边界。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from langchain_mcp_adapters.sessions import StdioConnection
from pydantic import ValidationError

from app.schemas.internal_v2.mcp import (
    McpCandidatePlanDTO,
    McpInstallationOperationDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
)
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.installation.npm import (
    npm_entrypoint,
    npm_package,
    npm_runtime,
)
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.runtime import (
    InstallationUnavailable,
    artifact_digest,
)
from app.services.infrastructure.mcp.installation.store import (
    InstallationStore,
    artifact_key,
)


@pytest.fixture
def npm_package_root(tmp_path: Path) -> Path:
    package = tmp_path / "generation/node_modules/@example/server"
    package.mkdir(parents=True)
    (package / "cli.js").write_text("console.log('original');\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"name": "@example/server", "version": "1.0.0",
        "bin": {"explicit-server": "cli.js"}}), encoding="utf-8")
    return tmp_path / "generation"


@pytest.fixture
def published_npm(tmp_path: Path, npm_package_root: Path) -> tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]:
    backend_id = "00000000-0000-4000-8000-000000000001"
    home = tmp_path / "home"
    store = InstallationStore(home / "artifacts/mcp" / backend_id)
    operation = McpInstallationOperationDTO(operation_id="npm-original", intent_digest="npm-intent",
        status="running", stage="verify", created_at=1, updated_at=1)
    plan = McpCandidatePlanDTO.model_validate({"source": {"kind": "manual", "name": "npm-server", "version": "release-1"},
        "manifest_digest": "sha256:jcs:v1:" + "c" * 64, "variant": {"kind": "package", "index": 0, "digest": "sha256:jcs:v1:" + "d" * 64},
        "platform": native_mcp_platform().model_dump(), "plan_digest": "sha256:jcs:v1:" + "f" * 64,
        "disposition": "requires_artifact", "download_required": True, "parameters": {}})
    preparation = store.reserve(operation, key=artifact_key(plan, identifier="@example/server", version="1.0.0", entrypoint="explicit-server")).preparation
    assert preparation is not None
    root = home / "artifacts/mcp" / backend_id / "generations" / preparation.installation_id / preparation.generation
    root.parent.mkdir(parents=True)
    npm_package_root.rename(root)
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_text("original Node", encoding="utf-8")
    node.chmod(0o755)
    artifact = McpInstalledArtifactDTO(binding=McpManagedLaunchDTO(backend_id=backend_id,
        installation_id=preparation.installation_id, generation=preparation.generation, artifact_digest=artifact_digest(root)),
        source=plan.source, manifest_digest=plan.manifest_digest, variant=plan.variant, platform=plan.platform,
        registry_type="npm", identifier="@example/server", version="1.0.0", entrypoint="explicit-server",
        command=str(root / "node_modules/@example/server/cli.js"), lock_digest="sha256:" + "e" * 64,
        installed_distributions={"@example/server": "1.0.0"}, runtime_executable=str(node), runtime_version="v22.17.0")
    store.publish(operation, artifact)
    return McpInstallationOwner(home=home, backend_id=backend_id), artifact, node


def test_explicit_missing_node_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    searched: list[str] = []
    monkeypatch.setenv("BOXTEAM_NODE_BIN", "/explicit/missing/node")

    def missing(command: str) -> None:
        searched.append(command)

    monkeypatch.setattr("app.services.infrastructure.mcp.installation.npm.shutil.which", missing)
    with pytest.raises(InstallationUnavailable, match="目标缺少已有 Node"):
        npm_runtime()
    assert searched == ["/explicit/missing/node"]


def test_missing_npm_has_independent_capability_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = tmp_path / "node"
    node.write_text("existing runtime", encoding="utf-8")
    monkeypatch.delenv("BOXTEAM_NODE_BIN", raising=False)
    monkeypatch.setattr("app.services.infrastructure.mcp.installation.npm.shutil.which", lambda command: str(node) if command == "node" else None)
    monkeypatch.setattr("app.services.infrastructure.mcp.installation.owner.python_package", lambda parameters: ("probe", "1", "https://example.test"))
    owner = McpInstallationOwner(home=tmp_path / "home", backend_id="00000000-0000-4000-8000-000000000001")
    capability = owner.capabilities()
    assert capability.pypi_wheel_available and capability.unavailable_reason is None
    assert not capability.npm_available and capability.npm_unavailable_reason is not None
    assert "标准 npm CLI" in capability.npm_unavailable_reason
    assert capability.unsupported_registry_types == ["oci", "nuget"]
    assert any("install scripts" in limitation for limitation in capability.limitations)


@pytest.mark.parametrize("extra,reason", [
    ({"runtime_hint": "node"}, "runtimeArguments"),
    ({"runtime_arguments": ["--no-install"]}, "runtimeArguments"),
    ({"expected_sha256": "a" * 64}, "fileSha256"),
    ({"registry_base_url": "https://secret@example.test/npm"}, "无凭据"),
    ({"registry_base_url": "https://example.test/npm?token=secret"}, "无凭据"),
    ({"registry_base_url": "http://external.example.test/npm"}, "loopback"),
])
def test_unsupported_npm_inputs_fail_explicitly(extra: dict[str, object], reason: str) -> None:
    with pytest.raises(InstallationUnavailable, match=reason):
        npm_package({"identifier": "@example/server", "version": "1.0.0", **extra})


@pytest.mark.parametrize("bins,entrypoint", [({"explicit-server": "cli.js"}, "explicit-server"), ("cli.js", "server")])
def test_scoped_root_bin_is_selected_explicitly(npm_package_root: Path, bins: object, entrypoint: str) -> None:
    package = npm_package_root / "node_modules/@example/server"
    (package / "package.json").write_text(json.dumps({"name": "@example/server", "version": "1.0.0", "bin": bins}), encoding="utf-8")
    assert npm_entrypoint(npm_package_root, name="@example/server", version="1.0.0", entrypoint=entrypoint) == package / "cli.js"
    with pytest.raises(InstallationUnavailable, match="根 package 的 bin"):
        npm_entrypoint(npm_package_root, name="@example/server", version="1.0.0", entrypoint="dependency-bin")
    with pytest.raises(InstallationUnavailable, match="精确版本"):
        npm_entrypoint(npm_package_root, name="@example/server", version="2.0.0", entrypoint=entrypoint)


def test_root_bin_cannot_escape_its_package(npm_package_root: Path) -> None:
    package = npm_package_root / "node_modules/@example/server"
    outside = npm_package_root / "node_modules/outside.js"
    outside.write_text("console.log('outside');\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"name": "@example/server", "version": "1.0.0",
        "bin": {"escape": "../../outside.js"}}), encoding="utf-8")
    with pytest.raises(InstallationUnavailable, match="包内的实际 JavaScript 文件"):
        npm_entrypoint(npm_package_root, name="@example/server", version="1.0.0", entrypoint="escape")


def test_runtime_metadata_is_required_only_for_npm(published_npm: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]) -> None:
    _, artifact, _ = published_npm
    raw = artifact.model_dump(exclude={"runtime_executable", "runtime_version"})
    with pytest.raises(ValidationError, match="实际 Node 路径及版本"):
        McpInstalledArtifactDTO.model_validate(raw)
    python = McpInstalledArtifactDTO.model_validate({**raw, "registry_type": "pypi"})
    assert python.runtime_executable is None and python.runtime_version is None


@pytest.mark.parametrize("key", ["NODE_OPTIONS", "node_path"])
@pytest.mark.parametrize("private", [False, True])
def test_managed_npm_rejects_dependency_replacement_environment(
    published_npm: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path], key: str, private: bool,
) -> None:
    owner, artifact, _ = published_npm
    value: str | dict[str, str] = {"kind": "mcp_private_env", "workspace_id": "00000000-0000-4000-8000-000000000002",
        "server_id": "managed", "input_key": key, "definition_digest": "sha256:jcs:v1:" + "a" * 64,
        "version": "b" * 32} if private else "injected"
    with pytest.raises(ValueError, match="Node 导入路径或启动选项"):
        parse_mcp_server_configs({"servers": {"managed": {"transport": "stdio", "managed_launch": artifact.binding.model_dump(),
            "env": {key: value}}}}, workspace_root=Path(artifact.command).parent, installation_resolver=owner.artifact)


def test_managed_npm_keeps_plain_environment_without_python_flags(published_npm: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path]) -> None:
    owner, artifact, _ = published_npm
    server, = parse_mcp_server_configs({"servers": {"managed": {"transport": "stdio", "managed_launch": artifact.binding.model_dump(),
        "env": {"LABEL": "explicit"}}}}, workspace_root=Path(artifact.command).parent, installation_resolver=owner.artifact)
    assert cast(StdioConnection, server.connection).get("env") == {"LABEL": "explicit"}


def test_launch_uses_original_node_path_without_hash_or_path_reselection(
    published_npm: tuple[McpInstallationOwner, McpInstalledArtifactDTO, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, artifact, node = published_npm
    monkeypatch.setenv("BOXTEAM_NODE_BIN", "/changed/node")
    monkeypatch.setenv("PATH", "/changed/path")
    node.write_text("same path updated runtime", encoding="utf-8")
    assert owner.resolve(artifact.binding, command=artifact.command, args=["--label", "保留 空格"]) == (
        str(node), [artifact.command, "--label", "保留 空格"])
    node.unlink()
    with pytest.raises(InstallationUnavailable, match="原 Node 路径缺失或不可执行"):
        owner.resolve(artifact.binding, command=artifact.command)
    assert owner.artifacts() == [artifact]
