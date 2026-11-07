"""引用经过真实来源预检、CAS 保存、权威快照与冷恢复；不建立 MCP 连接。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import TypedDict

import httpx
import pytest
from pydantic import SecretStr

from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.config import ConfigSourceEditRequest
from app.schemas.internal_v2.mcp import (
    McpCandidateConfigurationRequestDTO,
    McpCandidateInspectRequestDTO,
    McpCandidatePlanRequestDTO,
    McpCandidatePlanValidationRequestDTO,
    McpCandidateSourceDTO,
    McpInstallationOperationDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpPrivateEnvWriteRequestDTO,
    McpWorkspaceTargetDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner, candidate_source
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.runtime import artifact_digest
from app.services.infrastructure.mcp.installation.store import artifact_key
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

GATEWAY = "gateway_" + "a" * 32
SOURCE = "workspace_root_mutable_override"
SECRET = "Bearer source-unit-only"


class SourceConfigPaths(TypedDict):
    config_dir: Path
    inline_config_path: Path
    config_path: Path
    workspace_root: Path


@pytest.fixture
def private_source(empty_workspace_scope: tuple[str, Path], tmp_path: Path, monkeypatch):
    import app.services.infrastructure.config_service.config_source_layers as source_layers

    workspace_id, root = empty_workspace_scope
    directory = tmp_path / "configuration"
    directory.mkdir()
    inline = directory / "workspace_inline.jsonc"
    inline.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    (directory / "workspace_schema.jsonc").write_bytes(Path("configs/workspace_schema.jsonc").read_bytes())
    monkeypatch.setattr(source_layers, "get_user_workspace_schema_path", lambda: directory / "absent.jsonc")
    path = root / ".boxteam/workspace.jsonc"
    path.write_text("{}\n")
    kwargs = SourceConfigPaths(config_dir=directory, inline_config_path=inline, config_path=directory / "workspace.jsonc", workspace_root=root)
    with WorkspaceStateStore(workspace_root=root) as store:
        service = ConfigService(**kwargs, workspace_state_store=store)
        service.get_snapshot()
        service.validate_workspace_config()
        reference = McpPrivateInputStore(workspace_root=root).write(
            server_id="secure", header_name="Authorization", endpoint="https://service.test/mcp", value=SecretStr(SECRET),
        )
        yield workspace_id, service, store, reference, path, kwargs


async def test_reference_survives_source_candidate_cas_save_and_cold_snapshot(private_source):
    workspace_id, service, store, reference, path, kwargs = private_source
    original = service.get_source_document(source_key=SOURCE, gateway_id=GATEWAY)
    payload = {"mcp": {"servers": {"secure": {"transport": "streamable_http", "url": reference.endpoint,
               "headers": {"Authorization": reference.model_dump(mode="json")}}}}}
    text = "// 用户保留的格式\n" + json.dumps(payload, indent=2) + "\n"
    request = ConfigSourceEditRequest(
        gateway_id=GATEWAY, workspace_id=workspace_id, source_key=SOURCE, jsonc=text,
        base_file_digest=original.file_digest, base_layer_revision=original.layer_revision,
        base_layer_digest=original.layer_digest, expected_active_revision=original.active_revision,
        expected_active_digest=original.active_digest, idempotency_key="private-header-source",
    )
    preview = service.preflight_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=request)
    assert preview.candidate["mcp"]["servers"]["secure"]["headers"]["Authorization"] == reference.model_dump(mode="json")
    assert path.read_text() == "{}\n" and SECRET not in preview.model_dump_json()
    stale = request.model_copy(update={"expected_active_digest": "stale"})
    with pytest.raises(ConfigConflictError):
        await service.save_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=stale)
    assert path.read_text() == "{}\n"
    saved = await service.save_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=request)
    assert saved.jsonc == text and saved.edit is not None and saved.edit.source_saved and saved.edit.layer_imported
    assert saved.edit.state == "active" and path.read_text() == text
    assert SECRET not in saved.model_dump_json() and "boxteam-redacted:" not in saved.jsonc
    restarted = ConfigService(**kwargs, workspace_state_store=store)
    assert restarted.get_mcp_config()["servers"]["secure"]["headers"]["Authorization"] == reference.model_dump(mode="json")
    with sqlite3.connect(store.path) as connection:
        assert SECRET not in "\n".join(connection.iterdump())


async def test_catalog_secret_env_consumes_original_request_through_source_cas(private_source, tmp_path, monkeypatch):
    workspace_id, service, state, _, path, kwargs = private_source
    backend = "00000000-0000-4000-8000-000000000011"
    target = McpWorkspaceTargetDTO(gateway_id=GATEWAY, workspace_id=workspace_id)
    installation_target = McpInstallationTargetDTO(gateway_id=GATEWAY, backend_id=backend)
    home = tmp_path / "installation-home"
    installation = McpInstallationOwner(home=home, backend_id=backend)
    state.bind_mcp_installations(installation)
    owner = McpCatalogOwner(raw_config={}, workspace_root=state.workspace_root, event_service=EventChannelService(),
        leases=ExternalResourceLeaseLedger(), installation_owner=installation)
    raw = {"server": {"name": "org.example/secure", "version": "1", "packages": [{
        "registryType": "pypi", "identifier": "server", "version": "1", "transport": {"type": "stdio"},
        "environmentVariables": [{"name": "API_KEY", "isSecret": True, "isRequired": True}],
    }]}, "_meta": {}}
    source = McpCandidateSourceDTO(kind="gallery", name="org.example/secure", version="1", endpoint="https://registry.test",
        entry_url="https://registry.test/v0.1/servers/org.example%2Fsecure/versions/1")
    real_client = httpx.AsyncClient
    reads = []

    def respond(request):
        reads.append(str(request.url))
        return httpx.Response(200, json=raw)

    monkeypatch.setattr(candidate_source.httpx, "AsyncClient", lambda **options: real_client(transport=httpx.MockTransport(respond), **options))
    inspected = await owner.inspect_installation_candidate(McpCandidateInspectRequestDTO(target=installation_target, source=source), actual_target=installation_target)
    candidate = McpCandidatePlanRequestDTO(target=installation_target, source=source, manifest_digest=inspected.manifest_digest,
        variant=inspected.variants[0].ref, server_id="secure")
    reference = await owner.write_private_env(McpPrivateEnvWriteRequestDTO(target=target, server_id="secure", input_key="API_KEY",
        candidate=candidate, value=SecretStr(SECRET)), actual_target=target, installation_target=installation_target, candidate_required=True)
    candidate = candidate.model_copy(update={"inputs": {"API_KEY": reference.model_dump()}})
    plan = await owner.prepare_installation_candidate(candidate, actual_target=installation_target)
    assert plan.parameters["env"] == {"API_KEY": reference.model_dump()} and SECRET not in plan.model_dump_json()

    operation = McpInstallationOperationDTO(operation_id="publish-unit", intent_digest="unit", status="running", stage="prepare", created_at=1, updated_at=1)
    reservation = installation._store.reserve(operation, key=artifact_key(plan, identifier="server", version="1", entrypoint="server"))
    preparation = reservation.preparation
    assert preparation is not None
    root = home / "artifacts/mcp" / backend / "generations" / preparation.installation_id / preparation.generation
    command = root / ".venv" / ("Scripts/server.exe" if os.name == "nt" else "bin/server")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"unit-published-command")
    binding = McpManagedLaunchDTO(backend_id=backend, installation_id=preparation.installation_id,
        generation=preparation.generation, artifact_digest=artifact_digest(root))
    artifact = McpInstalledArtifactDTO(binding=binding, source=plan.source, manifest_digest=plan.manifest_digest,
        variant=plan.variant, platform=plan.platform, identifier="server", version="1", entrypoint="server", command=str(command),
        lock_digest="sha256:" + "a" * 64, installed_distributions={"server": "1"})
    installation._store.publish(operation, artifact)
    document = service.get_source_document(source_key=SOURCE, gateway_id=GATEWAY)
    config = {"mcp": {"servers": {"secure": {"transport": "stdio", "managed_launch": binding.model_dump(),
        "args": [], "env": {"API_KEY": reference.model_dump()}}}}}
    edit = ConfigSourceEditRequest(gateway_id=GATEWAY, workspace_id=workspace_id, source_key=SOURCE,
        jsonc="// 保留原来源格式\n" + json.dumps(config, indent=2) + "\n", base_file_digest=document.file_digest,
        base_layer_revision=document.layer_revision, base_layer_digest=document.layer_digest,
        expected_active_revision=document.active_revision, expected_active_digest=document.active_digest, idempotency_key="catalog-env-source")
    request = McpCandidateConfigurationRequestDTO(target=target, server_id="secure", managed_launch=binding,
        plan=McpCandidatePlanValidationRequestDTO(candidate=candidate, plan_digest=plan.plan_digest), source_edit=edit)
    stale = request.model_copy(update={"source_edit": edit.model_copy(update={"expected_active_digest": "stale"})})
    with pytest.raises(ConfigConflictError):
        await owner.save_candidate_configuration(stale, actual_target=target, installation_target=installation_target, config_service=service)
    assert path.read_text() == "{}\n"
    changed_config = json.loads(json.dumps(config))
    changed_config["mcp"]["servers"]["secure"]["args"] = ["--another-command-mode"]
    tampered = request.model_copy(update={"source_edit": edit.model_copy(update={"jsonc": json.dumps(changed_config), "idempotency_key": "tampered-launch"})})
    with pytest.raises(McpCandidateConflict, match="实际 launch"):
        await owner.save_candidate_configuration(tampered, actual_target=target, installation_target=installation_target, config_service=service)
    assert path.read_text() == "{}\n"
    saved = await owner.save_candidate_configuration(request, actual_target=target, installation_target=installation_target, config_service=service)
    assert saved.edit is not None and saved.edit.state == "active" and saved.edit.source_saved and saved.edit.layer_imported
    assert saved.jsonc == edit.jsonc and SECRET not in saved.model_dump_json()
    restarted = ConfigService(**kwargs, workspace_state_store=state)
    assert restarted.get_mcp_config()["servers"]["secure"]["env"]["API_KEY"] == reference.model_dump()
    before = path.read_bytes()
    raw["server"]["packages"][0]["identifier"] = "changed"
    with pytest.raises(McpCandidateConflict):
        await owner.save_candidate_configuration(request, actual_target=target, installation_target=installation_target, config_service=service)
    assert path.read_bytes() == before and len(reads) >= 5
    with sqlite3.connect(state.path) as connection:
        assert SECRET not in "\n".join(connection.iterdump())
    await installation.close()
