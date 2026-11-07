"""私有 env 的来源隔离、不可变版本与原 SDK 准备边界。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.mcp_candidates import candidate_parameters, parse_candidate_manifest
from app.schemas.internal_v2.mcp import (
    McpCandidateProvenanceDTO,
    McpCandidateVariantRefDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpTargetPlatformDTO,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.source_document import (
    redact_source_payload,
    redact_source_text,
    restore_source_text,
)
from app.services.infrastructure.mcp import session_runtime
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.private_inputs import (
    McpPrivateInputStore,
    candidate_env_definition,
    stdio_env_definition,
)

SECRET = "env-unit-only-私有\n值"


@pytest.fixture
def private_env(empty_workspace_scope: tuple[str, Path]):
    _, root = empty_workspace_scope
    raw = {"transport": "stdio", "command": "/tools/server", "args": ["--serve"], "cwd": str(root)}
    server, = parse_mcp_server_configs({"servers": {"secure": raw}}, workspace_root=root)
    store = McpPrivateInputStore(workspace_root=root)
    reference = store.write_env(server_id="secure", input_key="API_KEY", definition_digest=stdio_env_definition(server.connection), value=SecretStr(SECRET))
    return root, store, reference, raw


def test_private_env_cold_versions_and_public_round_trip_keep_only_identity(private_env):
    root, store, reference, raw = private_env
    second = store.write_env(server_id="secure", input_key="API_KEY", definition_digest=reference.definition_digest, value=SecretStr("rotated"))
    restarted = McpPrivateInputStore(workspace_root=root)
    for ref, expected in ((reference, SECRET), (second, "rotated")):
        assert restarted.resolve_env(ref, server_id="secure", input_key="API_KEY", definition_digest=reference.definition_digest) == expected
    assert reference.version != second.version
    payload = {"mcp": {"servers": {"secure": {**raw, "env": {"API_KEY": reference.model_dump(), "PLAIN": "existing"}}}}}
    text = "// 保留格式\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    public = redact_source_text(text)
    assert json.loads(public.split("\n", 1)[1])["mcp"]["servers"]["secure"]["env"]["API_KEY"] == reference.model_dump()
    assert "existing" not in public and SECRET not in public
    assert restore_source_text(public, text) == text
    assert SECRET not in json.dumps(redact_source_payload(payload), ensure_ascii=False)
    assert len(list((root / ".boxteam/credentials/mcp-inputs").glob("*.json"))) == 2


def test_private_env_rotation_uses_original_immediate_configuration_policy():
    decisions = workspace_config_policy().classify(tuple(
        f"/mcp/servers/secure/env/API_KEY/{field}"
        for field in ("kind", "workspace_id", "server_id", "input_key", "definition_digest", "version")
    ))
    assert all(decision.policy == "immediate_read" and not decision.policy_missing for decision in decisions)


@pytest.mark.parametrize("change", [{"command": "/tools/other"}, {"args": ["--other"]}, {"cwd": "/other"}])
def test_private_env_rejects_changed_actual_launch_before_sdk(private_env, change):
    root, _, reference, raw = private_env
    with pytest.raises(ValueError, match="原来源／launch"):
        parse_mcp_server_configs({"servers": {"secure": {**raw, **change, "env": {"API_KEY": reference.model_dump()}}}}, workspace_root=root)


def test_private_env_cannot_cross_workspace_server_key_or_unknown_version(private_env):
    root, store, ref, _ = private_env
    for changes in ({"server_id": "other"}, {"input_key": "OTHER"}, {"definition_digest": "sha256:jcs:v1:" + "f" * 64}):
        with pytest.raises(ValueError, match="不属于"):
            store.resolve_env(ref, **{"server_id": "secure", "input_key": "API_KEY", "definition_digest": ref.definition_digest, **changes})
    other = root / "other"
    load_or_create_workspace_id(other)
    with pytest.raises(ValueError, match="不属于"):
        McpPrivateInputStore(workspace_root=other).resolve_env(ref, server_id="secure", input_key="API_KEY", definition_digest=ref.definition_digest)
    with pytest.raises(ValueError, match="版本不存在"):
        store.resolve_env(ref.model_copy(update={"version": "0" * 32}), server_id="secure", input_key="API_KEY", definition_digest=ref.definition_digest)


@pytest.mark.parametrize("value", ["", "secret\x00value", "secret\ud800value"])
def test_invalid_private_env_is_rejected_without_file_or_value_in_error(private_env, value):
    root, store, ref, _ = private_env
    before = list((root / ".boxteam/credentials/mcp-inputs").glob("*.json"))
    with pytest.raises(ValidationError) as caught:
        store.write_env(server_id="secure", input_key="API_KEY", definition_digest=ref.definition_digest, value=SecretStr(value))
    assert value not in str(caught.value) if value else "input_value" not in str(caught.value)
    assert list((root / ".boxteam/credentials/mcp-inputs").glob("*.json")) == before


async def test_original_stdio_sdk_resolves_private_env_only_during_prepare(private_env, monkeypatch, mcp_session_lifecycle):
    root, _, reference, raw = private_env
    server, = parse_mcp_server_configs({"servers": {"secure": {**raw, "env": {"API_KEY": reference.model_dump(), "PLAIN": "${workspaceRoot}"}}}}, workspace_root=root)
    assert server.private_env == (reference,) and dict(server.connection).get("env") == {"PLAIN": str(root)}
    assert SECRET not in repr(server) and SECRET not in repr(asdict(server))
    reached = []

    class TransportReached(Exception):
        pass

    @asynccontextmanager
    async def transport(parameters, process, on_started):
        reached.append(parameters)
        assert parameters.env == {"API_KEY": SECRET, "PLAIN": str(root)}
        raise TransportReached
        yield  # 在原 SDK transport 边界停止，不伪造 initialize 成功。

    monkeypatch.setattr(session_runtime, "mcp_stdio_transport", transport)
    with pytest.raises(TransportReached):
        async with session_runtime.production_mcp_session_factory(server, lambda: None, mcp_session_lifecycle, workspace_root=root):
            pytest.fail("不能越过 transport 探针")
    assert SECRET not in repr(server) and len(reached) == 1
    file, = (root / ".boxteam/credentials/mcp-inputs").glob("*.json")
    file.unlink()
    with pytest.raises(ValueError, match="版本不存在"):
        async with session_runtime.production_mcp_session_factory(server, lambda: None, mcp_session_lifecycle, workspace_root=root):
            pytest.fail("缺失私有版本不能进入 transport")
    assert len(reached) == 1


def test_candidate_secret_env_maps_only_reference_and_rejects_plain_args_or_templates(private_env):
    _, _, ref, _ = private_env
    package = {"registryType": "pypi", "identifier": "server", "version": "1", "transport": {"type": "stdio"},
               "environmentVariables": [{"name": "API_KEY", "isSecret": True, "isRequired": True}]}
    variant, = parse_candidate_manifest({"name": "server", "version": "1", "packages": [package]},
        kind="manual", protocol=None, name="server", version="1", entry_id=None).variants
    disposition, parameters = candidate_parameters(variant, {"API_KEY": ref.model_dump()}, purpose="configuration")
    assert disposition == "requires_artifact" and parameters["env"] == {"API_KEY": ref.model_dump()}
    with pytest.raises(ValueError):
        candidate_parameters(variant, {"API_KEY": SECRET}, purpose="configuration")
    for change in (
        {"packageArguments": [{"type": "positional", "valueHint": "password", "isSecret": True}]},
        {"environmentVariables": [{"name": "API_KEY", "isSecret": True, "value": "prefix-{x}", "variables": {"x": {}}}]},
    ):
        altered, = parse_candidate_manifest({"name": "server", "version": "1", "packages": [{**package, **change}]},
            kind="manual", protocol=None, name="server", version="1", entry_id=None).variants
        with pytest.raises(ValueError, match="secret"):
            candidate_parameters(altered, {"API_KEY": ref.model_dump()}, purpose="configuration")


def test_managed_private_env_binds_source_not_generation(private_env):
    root, store, _, _ = private_env
    source = McpCandidateProvenanceDTO(kind="gallery", name="server", version="1", endpoint="https://registry.test", protocol="v0.1",
                                       entry_url="https://registry.test/v0.1/server")
    variant = McpCandidateVariantRefDTO(kind="package", index=0, digest="sha256:jcs:v1:" + "a" * 64)
    manifest = "sha256:jcs:v1:" + "b" * 64
    reference = store.write_env(server_id="secure", input_key="API_KEY", definition_digest=candidate_env_definition(source, manifest, variant), value=SecretStr(SECRET))
    binding = McpManagedLaunchDTO(backend_id="00000000-0000-4000-8000-000000000001", installation_id="c" * 32,
                                   generation="d" * 32, artifact_digest="sha256:jcs:v1:" + "e" * 64)
    artifact = McpInstalledArtifactDTO(binding=binding, source=source, manifest_digest=manifest, variant=variant,
        platform=McpTargetPlatformDTO(operating_system="linux", architecture="x86_64"), identifier="server", version="1", entrypoint="server",
        command="/published/server", lock_digest="sha256:" + "f" * 64, installed_distributions={"server": "1"})
    raw = {"transport": "stdio", "managed_launch": binding.model_dump(), "env": {"API_KEY": reference.model_dump()}}
    for generation in ("d" * 32, "f" * 32):
        current = artifact.model_copy(update={"binding": binding.model_copy(update={"generation": generation})})
        configured = {**raw, "managed_launch": current.binding.model_dump()}
        server, = parse_mcp_server_configs({"servers": {"secure": configured}}, workspace_root=root, installation_resolver=lambda _, current=current: current)
        assert server.private_env == (reference,)
    changed = artifact.model_copy(update={"source": source.model_copy(update={"endpoint": "https://other.test"})})
    with pytest.raises(ValueError, match="不属于"):
        parse_mcp_server_configs({"servers": {"secure": raw}}, workspace_root=root, installation_resolver=lambda _: changed)
