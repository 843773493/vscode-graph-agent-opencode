"""显式安装、配置图、重载和冻结 active 恢复共用真实 Workspace owner。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.config import ConfigSourceEditRequest
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.inheritance.values import thaw
from configs.installer import (
    install_user_configuration,
    install_workspace_configuration,
)
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@dataclass
class InstalledWorkspace:
    workspace: Path
    config_root: Path
    service: ConfigService
    store: WorkspaceStateStore
    source_owner: WorkspaceSourceOwner


@pytest.fixture
async def installed_workspace(tmp_path: Path) -> AsyncIterator[InstalledWorkspace]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    config_root = tmp_path / "home/config"
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    user = read_jsonc_object(config_root / "workspace.jsonc")
    user["settings"] = {"workspace": {"llm": {"providers": {"primary": {
        "config_ref": "#/profiles/private",
    }}}}}
    user["profiles"] = {"private": {
        "extends": "builtin:workspace#/profiles/provider.primary",
        "settings": {"model_options": {"request_options": {"parameters": {"temperature": 0}}}},
    }}
    (config_root / "workspace.jsonc").write_text(json.dumps(user), encoding="utf-8")
    source_owner = WorkspaceSourceOwner(path=config_root / "workspace-source.sqlite")
    with WorkspaceStateStore(workspace_root=workspace) as store:
        service = ConfigService(config_dir=config_root, config_path=config_root / "workspace.jsonc",
                                workspace_root=workspace, workspace_state_store=store, source_owner=source_owner,
                                source_owner_workspace_id=load_or_create_workspace_id(workspace))
        try:
            service.validate_workspace_config()
            yield InstalledWorkspace(workspace, config_root, service, store, source_owner)
        finally:
            await service.close()


async def test_installer_profile_reload_and_reopen_keep_the_frozen_active_graph(
    installed_workspace: InstalledWorkspace,
) -> None:
    case = installed_workspace
    source = case.config_root / "workspace.jsonc"
    reference = ModelReferenceDTO(provider_id="primary", model_id="default")
    original = case.service.get_snapshot()
    assert len(original.graph.resolve_scope().chain) == 4
    assert case.service.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0
    document = read_jsonc_object(source)
    document["profiles"]["private"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.25
    source.write_text(json.dumps(document), encoding="utf-8")
    assert await case.service.reload()
    active = case.service.get_snapshot()
    assert active.revision != original.revision
    assert case.service.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.25
    with case.service.use_snapshot(original):
        assert case.service.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0
    stored = case.store.get_active_config_snapshot("workspace")
    assert stored is not None
    assert all("document" in layer for layer in stored.source_baseline.values() if layer["presence"] == "present")
    await case.service.close()
    # 文件后来变化不能反向改写冻结 active 的定义和模型选项。
    document["profiles"]["private"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.5
    source.write_text(json.dumps(document), encoding="utf-8")
    restored = ConfigService(config_dir=case.config_root, config_path=source,
                             workspace_root=case.workspace, workspace_state_store=case.store, source_owner=case.source_owner,
                             source_owner_workspace_id=load_or_create_workspace_id(case.workspace))
    try:
        restored.validate_workspace_config()
        assert restored.get_snapshot().revision == active.revision
        assert restored.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.25
        assert await restored.reload()
        assert restored.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.5
    finally:
        await restored.close()


async def test_equal_profile_reference_refresh_survives_restart(installed_workspace: InstalledWorkspace) -> None:
    case = installed_workspace
    source = case.config_root / "workspace.jsonc"
    reference = ModelReferenceDTO(provider_id="primary", model_id="default")
    before = case.service.get_snapshot()
    options = case.service.get_llm_model(reference)
    document = read_jsonc_object(source)
    document["profiles"]["same-options"] = deepcopy(document["profiles"]["private"])
    document["settings"]["workspace"]["llm"]["providers"]["primary"]["config_ref"] = "#/profiles/same-options"
    source.write_text(json.dumps(document), encoding="utf-8")
    assert await case.service.reload() is False
    active = case.service.get_snapshot()
    assert active.revision == before.revision
    assert active.to_dict()["llm"]["providers"]["primary"]["config_ref"] == "#/profiles/same-options"
    assert active.graph.resolve_provider("primary").location.pointer == "/profiles/same-options"
    assert case.service.get_llm_model(reference) == options
    await case.service.close()
    restored = ConfigService(config_dir=case.config_root, config_path=source,
                             workspace_root=case.workspace, workspace_state_store=case.store, source_owner=case.source_owner,
                             source_owner_workspace_id=load_or_create_workspace_id(case.workspace))
    try:
        restored.validate_workspace_config()
        assert restored.get_snapshot().revision == before.revision
        assert restored.get_snapshot().to_dict() == active.to_dict()
        assert restored.get_snapshot().graph.resolve_provider("primary").location.pointer == "/profiles/same-options"
        assert restored.get_llm_model(reference) == options
    finally:
        await restored.close()


async def test_explicit_parent_can_bypass_user_sources(installed_workspace: InstalledWorkspace) -> None:
    case = installed_workspace
    entry = case.workspace / ".boxteam/workspace.jsonc"
    entry.write_text(json.dumps({"config_version": 4, "extends": "builtin:workspace", "settings": {}}), encoding="utf-8")
    (case.config_root / "workspace.jsonc").unlink()
    (case.config_root / "workspace_local.jsonc").unlink()
    assert await case.service.reload()
    graph = case.service.get_snapshot().graph
    assert len(graph.resolve_scope().chain) == 2
    assert set(graph.dependencies) == {entry.resolve(), Path.cwd() / "configs/workspace_inline.jsonc"}
    assert graph.resolve_provider("primary").location.pointer == "/profiles/provider.primary"


async def test_missing_declared_parent_is_not_created_and_can_be_repaired(installed_workspace: InstalledWorkspace) -> None:
    case = installed_workspace
    entry = case.workspace / ".boxteam/workspace.jsonc"
    parent = entry.parent / "parent.jsonc"
    active = case.service.get_snapshot()
    entry.write_text(json.dumps({"config_version": 4, "extends": "./parent.jsonc", "settings": {}}), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
        await case.service.reload()
    assert not parent.exists()
    assert case.service.get_snapshot() is active
    parent.write_text(json.dumps({"config_version": 4, "extends": "user:workspace_local", "settings": {}}), encoding="utf-8")
    assert await case.service.reload() is False
    repaired = case.service.get_snapshot()
    assert repaired.revision == active.revision
    assert parent.resolve() in repaired.graph.dependencies
    assert thaw(repaired.graph.resolve_scope().settings) == thaw(active.graph.resolve_scope().settings)


async def test_parent_change_after_candidate_build_rejects_the_whole_publication(
    installed_workspace: InstalledWorkspace,
) -> None:
    case = installed_workspace
    source = case.config_root / "workspace.jsonc"
    original = case.service.get_snapshot()
    document = read_jsonc_object(source)
    document["profiles"]["private"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.25
    source.write_text(json.dumps(document), encoding="utf-8")

    async def change_parent_after_build(*_arguments):
        document["profiles"]["private"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.5
        source.write_text(json.dumps(document), encoding="utf-8")
        return lambda: None

    with pytest.raises(RuntimeError, match="配置文件在解析后发生变化"):
        await case.service.reload(candidate_applier=change_parent_after_build)
    assert case.service.get_snapshot() is original
    active = case.store.get_active_config_snapshot("workspace")
    assert active is not None and active.effective_digest == original.revision
    assert await case.service.reload()
    assert case.service.get_llm_model(ModelReferenceDTO(provider_id="primary", model_id="default"))["request_options"]["parameters"]["temperature"] == 0.5


async def test_private_child_uses_the_inspected_external_reference_through_save_and_restart(
    installed_workspace: InstalledWorkspace,
) -> None:
    case = installed_workspace
    source = case.config_root / "workspace.jsonc"
    external = case.config_root / "external#parent%.jsonc"
    external.write_text(json.dumps({"config_version": 4, "settings": {}, "profiles": {"base": {
        "extends": "builtin:workspace#/profiles/provider.primary", "settings": {"model_options": {
            "request_options": {"parameters": {"temperature": 0.25}},
        }},
    }}}), encoding="utf-8")
    payload = read_jsonc_object(source)
    provider = payload["settings"]["workspace"]["llm"]["providers"]["primary"]
    external_reference = "./external%23parent%25.jsonc#/profiles/base"
    provider["config_ref"] = external_reference
    source.write_text(json.dumps(payload), encoding="utf-8")
    assert await case.service.reload()
    graph = case.service.get_snapshot().graph
    parent = graph.resolve_provider("primary")
    inspection = case.service._llm_resolution.inspect_provider(graph, "primary")
    reference = next(node.reference for node in inspection.chain if node.node_id == inspection.node_id)
    assert reference == quote(external.resolve().as_posix(), safe="/:") + "#/profiles/base"
    external_key = graph.source_keys[parent.origins.origin.source_uri]
    external_document = case.service.get_source_document(source_key=external_key, gateway_id="gateway_" + "8" * 32)
    assert external_document.source_inherited == {}
    external_nodes = [node for node in external_document.nodes if node.source_key == external_key]
    assert len(external_nodes) == 1 and external_nodes[0].node_id == parent.node_id
    assert external_nodes[0].definition_path == "/profiles/base" and external_nodes[0].reference == reference
    gateway_id, source_key = "gateway_" + "8" * 32, "workspace_mutable_override"
    document = case.service.get_source_document(source_key=source_key, gateway_id=gateway_id)
    payload["profiles"]["private-child"] = {"extends": reference, "settings": {"model_options": {
        "request_options": {"parameters": {"temperature": 0.5}},
    }}}
    provider["config_ref"] = "#/profiles/private-child"
    request = ConfigSourceEditRequest(gateway_id=gateway_id, workspace_id=document.workspace_id,
        source_key=source_key, jsonc=json.dumps(payload), base_file_digest=document.file_digest,
        base_layer_revision=document.layer_revision, base_layer_digest=document.layer_digest,
        expected_active_revision=document.active_revision, expected_active_digest=document.active_digest,
        idempotency_key="private-external-parent")
    preview = await case.service.preflight_source_edit(source_key=source_key, gateway_id=gateway_id,
        request=request, provider_id="primary")
    assert preview.provider_options is not None
    assert preview.provider_options.chain[-2].node_id == parent.node_id
    saved = await case.service.save_source_edit(source_key=source_key, gateway_id=gateway_id, request=request)
    assert saved.edit is not None and saved.edit.state == "active"
    assert external_reference not in source.read_text(encoding="utf-8")
    active = case.service.get_snapshot()
    assert active.graph.resolve_provider("primary").parent.node_id == parent.node_id
    assert external.resolve() in active.graph.dependencies
    await case.service.close()
    restored = ConfigService(config_dir=case.config_root, config_path=source, workspace_root=case.workspace,
        workspace_state_store=case.store, source_owner=case.source_owner,
        source_owner_workspace_id=load_or_create_workspace_id(case.workspace))
    try:
        restored.validate_workspace_config()
        assert restored.get_snapshot().to_dict() == active.to_dict()
        assert restored.get_snapshot().graph.resolve_provider("primary").parent.node_id == parent.node_id
        assert restored.get_llm_model(ModelReferenceDTO(provider_id="primary", model_id="default"))["request_options"]["parameters"]["temperature"] == 0.5
    finally:
        await restored.close()


async def test_shared_external_profile_fanout_recovers_and_stops_after_dependency_removal(
    installed_workspace: InstalledWorkspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = installed_workspace
    await case.service.close()
    source = case.config_root / "workspace.jsonc"
    shared = case.config_root / "shared.jsonc"
    profile = {"config_version": 4, "settings": {}, "profiles": {"shared": {
        "extends": "builtin:workspace#/profiles/provider.primary", "settings": {
            "model_options": {"request_options": {"parameters": {"temperature": 0.25}}},
        },
    }}}
    shared.write_text(json.dumps(profile), encoding="utf-8")
    document = read_jsonc_object(source)
    document["settings"]["workspace"]["llm"]["providers"]["primary"]["config_ref"] = "./shared.jsonc#/profiles/shared"
    source.write_text(json.dumps(document), encoding="utf-8")
    second_root = prepare_default_test_workspace(workspace_root=case.workspace / "second",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    install_workspace_configuration(workspace_root=second_root)
    owner = case.source_owner
    first_id, second_id = (load_or_create_workspace_id(root) for root in (case.workspace, second_root))
    # 下载目录不属于本场景；配置解析、文件监听与原 source owner 全部保留真实入口。
    monkeypatch.setattr("app.runtime.litellm_runtime.refresh_model_metadata", lambda **_arguments: None)

    async def wait_for(check) -> None:
        async with asyncio.timeout(30):
            while not check():
                await asyncio.sleep(0.05)

    async def reject_second(*_arguments):
        raise RuntimeError("第二工作区应用候选失败")

    with WorkspaceStateStore(workspace_root=second_root) as second_store:
        first = ConfigService(config_dir=case.config_root, config_path=source, workspace_root=case.workspace,
            workspace_state_store=case.store, source_owner=owner, source_owner_workspace_id=first_id)
        second = ConfigService(config_dir=case.config_root, config_path=source, workspace_root=second_root,
            workspace_state_store=second_store, source_owner=owner, source_owner_workspace_id=second_id)
        try:
            first.validate_workspace_config()
            second.validate_workspace_config()
            await first.reload()
            await first.start_watching()
            await second.start_watching(candidate_applier=reject_second)
            source_key = first.get_snapshot().graph.source_keys[first._source_uri_for_path(shared)]
            shared_key = "workspace:" + source_key
            reference = ModelReferenceDTO(provider_id="primary", model_id="default")
            profile["profiles"]["shared"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.5
            shared.write_text(json.dumps(profile), encoding="utf-8")
            await wait_for(lambda: {row.workspace_id: row.status for row in owner.list_fanout(
                source_key=shared_key, source_generation=owner.high_water_mark(source_key=shared_key),
            )} == {first_id: "applied", second_id: "failed"})
            assert first.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.5
            assert second.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.25
            await second.close()
            profile["profiles"]["shared"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 0.75
            shared.write_text(json.dumps(profile), encoding="utf-8")
            await wait_for(lambda: first.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.75)
            latest = owner.high_water_mark(source_key=shared_key)
            assert {row.workspace_id: row.status for row in owner.list_fanout(
                source_key=shared_key, source_generation=latest,
            )}[second_id] == "unobserved"
            second = ConfigService(config_dir=case.config_root, config_path=source, workspace_root=second_root,
                workspace_state_store=second_store, source_owner=owner, source_owner_workspace_id=second_id)
            second.validate_workspace_config()
            assert second.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.25
            await second.start_watching()
            await wait_for(lambda: second.get_llm_model(reference)["request_options"]["parameters"]["temperature"] == 0.75)
            assert owner.get_fanout(source_key=shared_key, source_generation=latest, workspace_id=second_id).status == "applied"
            document["settings"]["workspace"]["llm"]["providers"]["primary"]["config_ref"] = "#/profiles/private"
            source.write_text(json.dumps(document), encoding="utf-8")
            await wait_for(lambda: all(shared.resolve() not in service.get_snapshot().graph.dependencies
                                      and shared.resolve() not in service._watcher._candidate_paths
                                      for service in (first, second)))
            before = owner.high_water_mark(source_key=shared_key)
            first_snapshot, second_snapshot = first.get_snapshot(), second.get_snapshot()
            profile["profiles"]["shared"]["settings"]["model_options"]["request_options"]["parameters"]["temperature"] = 1
            shared.write_text(json.dumps(profile), encoding="utf-8")
            await asyncio.sleep(0.7)
            assert owner.high_water_mark(source_key=shared_key) == before
            assert first.get_snapshot().revision == first_snapshot.revision
            assert second.get_snapshot().revision == second_snapshot.revision
            assert first.get_snapshot().to_dict() == first_snapshot.to_dict()
            assert second.get_snapshot().to_dict() == second_snapshot.to_dict()
        finally:
            await second.close()
            await first.close()
