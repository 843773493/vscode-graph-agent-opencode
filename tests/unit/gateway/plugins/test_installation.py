from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.plugins.installation import PluginInstallations
from app.gateway.plugins.packages import PluginPackageStore
from app.schemas.gateway.plugin_installation import (
    FrozenPluginInstallArtifact,
    InstallPluginRequest,
    PluginSourceArtifact,
)
from app.services.infrastructure.config.state import ConfigConflictError
from tests.support.plugin_package import native_plugin_target


def _archive(path: Path, *, package_id: str = "install.test") -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": package_id,
        "version": "1.0.0",
        "name": "Install test",
        "target": native_plugin_target("node"),
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe", "name": "Probe", "instance_scope": "gateway",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{
                "service_id": "probe", "service_type": "mcp", "protocol": "streamable-http",
            }]},
            "runtime": "node",
            "entrypoint": ["probe.mjs"],
            "management_protocol_version": 1,
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("probe.mjs", "export {};\n")
    return path


def _declaration_archive(path: Path, *, package_id: str) -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": package_id,
        "version": "1.0.0",
        "name": "Declaration test",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "declaration", "name": "Declaration", "instance_scope": "gateway",
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object"}},
        }],
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
    return path


@pytest.fixture
def node_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("测试主机没有 Node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(node).resolve(strict=True)))


@pytest.mark.asyncio
async def test_local_install_is_unique_and_publishes_with_succeeded_state(
    tmp_path: Path, node_bin: None,
) -> None:
    gateway_id = "gateway-install-test"
    database = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    packages = PluginPackageStore(tmp_path / "packages")
    archive = _archive(tmp_path / "plugin.zip")
    async with httpx.AsyncClient() as client:
        owner = PluginInstallations(gateway_id, packages, database, client, dict)
        request = InstallPluginRequest(
            gateway_id=gateway_id, operation_id="install-1", path=str(archive),
        )

        pending = await owner.start(request)
        assert pending.status == "running"
        assert (await owner.start(request)).status == "running"
        task = owner._tasks["install-1"]
        await task

        succeeded = owner.get("install-1")
        assert succeeded.status == "succeeded"
        assert packages.get("install.test", "1.0.0").manifest.package_id == "install.test"
        assert owner._tasks == {}
        archive.unlink()
        assert (await owner.start(request)).status == "succeeded"
        assert owner.cancel("install-1").status == "succeeded"
        await owner.close()
    database.close()


@pytest.mark.asyncio
async def test_cancel_committed_before_execution_prevents_publication(
    tmp_path: Path, node_bin: None,
) -> None:
    gateway_id = "gateway-install-cancel"
    database = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    packages = PluginPackageStore(tmp_path / "packages")
    archive = _archive(tmp_path / "plugin.zip", package_id="install.cancel")
    async with httpx.AsyncClient() as client:
        owner = PluginInstallations(gateway_id, packages, database, client, dict)
        observer_database = GatewayStateStore(
            path=tmp_path / "gateway.sqlite", allow_shared_processes=True,
        )
        observer = PluginInstallations(gateway_id, packages, observer_database, client, dict)
        request = InstallPluginRequest(
            gateway_id=gateway_id, operation_id="install-cancel", path=str(archive),
        )
        await owner.start(request)
        assert observer.get("install-cancel").status == "running"
        cancelled = observer.cancel("install-cancel")
        assert cancelled.cancel_requested
        await owner._tasks["install-cancel"]
        assert owner.get("install-cancel").status == "cancelled"
        with pytest.raises(FileNotFoundError):
            packages.get("install.cancel", "1.0.0")
        await owner.close()
        await observer.close()
        observer_database.close()
    database.close()


@pytest.mark.asyncio
async def test_same_operation_id_rejects_different_request(
    tmp_path: Path, node_bin: None,
) -> None:
    gateway_id = "gateway-install-conflict"
    database = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    packages = PluginPackageStore(tmp_path / "packages")
    archive = _archive(tmp_path / "plugin.zip")
    async with httpx.AsyncClient() as client:
        owner = PluginInstallations(gateway_id, packages, database, client, dict)
        first = InstallPluginRequest(
            gateway_id=gateway_id, operation_id="install-conflict", path=str(archive),
        )
        await owner.start(first)
        other = first.model_copy(update={"path": str(tmp_path / "other.zip")})
        with pytest.raises(ConfigConflictError, match="operation_id 已绑定不同输入"):
            await owner.start(other)
        await owner._tasks["install-conflict"]
        await owner.close()
    database.close()


@pytest.mark.parametrize(
    ("archive_id", "http_status", "expected_status"),
    [
        ("install.source", 200, "succeeded"),
        ("install.other", 200, "failed"),
        ("install.source", 403, "failed"),
    ],
)
@pytest.mark.asyncio
async def test_source_zip_identity_is_checked_before_publish_and_url_is_not_persisted(
    tmp_path: Path,
    node_bin: None,
    archive_id: str,
    http_status: int,
    expected_status: str,
) -> None:
    gateway_id = "gateway-install-source"
    database = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    packages = PluginPackageStore(tmp_path / "packages")
    archive = _declaration_archive(tmp_path / "source.zip", package_id=archive_id)
    source_url = "https://plugins.test/download.zip?token=private-query-token"

    async def serve(incoming: httpx.Request) -> httpx.Response:
        assert str(incoming.url) == source_url
        content = archive.read_bytes() if http_status == 200 else b"private-query-token denied"
        return httpx.Response(http_status, content=content, request=incoming)

    def configuration() -> dict[str, object]:
        return {"plugins": {"sources": {"entries": [{
            "source_id": "official", "protocol_version": 1,
            "base_url": "https://plugins.test", "enabled": True,
        }]}}}

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        owner = PluginInstallations(gateway_id, packages, database, client, configuration)
        artifact = {
            "package_id": "install.source", "package_version": "1.0.0",
            "target": None,
            "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
            "url": source_url, "filename": "source.zip",
        }
        request = InstallPluginRequest(
            gateway_id=gateway_id, operation_id=f"source-{archive_id}-{http_status}",
            source_id="official", artifact=PluginSourceArtifact.model_validate(artifact), catalog_revision="catalog-1",
        )
        accepted = await owner.start(request)
        assert "url" not in accepted.artifact.model_dump()
        await owner._tasks[request.operation_id]
        result = owner.get(request.operation_id)
        assert result.status == expected_status
        assert "private-query-token" not in (result.error or "")
        if http_status == 403:
            assert result.error == "插件来源请求失败: HTTP 403"
        connection = database.connection()
        try:
            row = connection.execute(
                "SELECT request_json,artifact_json FROM plugin_install_operations WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        assert "private-query-token" not in str(row[0]) + str(row[1])
        if expected_status == "failed":
            if http_status == 200:
                assert result.error == "插件包 package_id 与受理时冻结的制品不一致"
            with pytest.raises(FileNotFoundError):
                packages.get("install.source", "1.0.0")
        assert list(packages.root.glob(".source-download-*.zip")) == []
        await owner.close()
    database.close()


@pytest.mark.asyncio
async def test_dead_install_executor_becomes_unknown_without_replay(tmp_path: Path) -> None:
    gateway_id = "gateway-install-orphan"
    database = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    packages = PluginPackageStore(tmp_path / "packages")
    artifact = FrozenPluginInstallArtifact(
        source_kind="source", package_id="orphan.package",
        package_version="1.0.0", target=None, digest="sha256:" + "0" * 64,
        source_id="official", catalog_revision="catalog-1", filename="orphan.zip",
    )
    database.create_plugin_install_operation(
        gateway_id=gateway_id, operation_id="orphan-op",
        request_json='{"sha256":"request"}',
        artifact_json=artifact.model_dump_json(),
        owner_pid=2**31 - 1, owner_created_at=1,
    )
    async with httpx.AsyncClient() as client:
        owner = PluginInstallations(gateway_id, packages, database, client, dict)
        operation = owner.get("orphan-op")
        assert operation.status == "unknown"
        assert operation.error == "执行者已退出；发布结果未确认，不会自动重放"
        await owner.close()
    database.close()
