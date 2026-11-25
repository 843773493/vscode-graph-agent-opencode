from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from app.core.config_sources import read_stable_config_file
from app.core.path_utils import get_workspace_config_path
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.config import ConfigSourceEditRequest
from app.services.infrastructure.config import WorkspaceSourceOwner
from app.services.infrastructure.config.contracts import ConfigPublication
from app.services.infrastructure.config.snapshot import ConfigRestartRequiredError
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import (
    install_user_configuration,
    install_workspace_configuration,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture
def config_workspace_roots(request: pytest.FixtureRequest) -> tuple[Path, Path]:
    output_workspace = TestRunContext.from_test_file(Path(request.node.path)).workspace_root
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    roots = (output_workspace / "workspace-a", output_workspace / "workspace-b")
    for root in roots:
        prepare_test_workspace(
            workspace_root=root,
            template_root=template_root,
            template_items=(),
        )
        install_workspace_configuration(workspace_root=root)
        load_or_create_workspace_id(root)
    return roots


def _write_config(path: Path, *, model_name: str) -> None:
    install_user_configuration(config_root=path.parent, profile="default")
    path.write_text(
        json.dumps(
            {
                "config_version": 4,
                "extends": "builtin:workspace",
                "settings": {"workspace": {"llm": {
                    "providers": {
                        "primary": {"config_ref": "#/profiles/primary", "models": {
                            "default": {"model": model_name, "config_ref": "context:provider/primary"},
                        }}
                    }
                },
                "default_agent": "default",
                "agents": {
                    **{name: {"model": {"primary": {"provider_id": "primary", "model_id": "default"}, "fallbacks": []}}
                       for name in ("coder", "reviewer", "researcher")},
                    "default": {
                        "name": "Default Agent",
                        "instructions": {"system_prompt": "hello"},
                        "model": {
                            "primary": {"provider_id": "primary", "model_id": "default"},
                            "fallbacks": [],
                        },
                    }
                }}},
                "profiles": {"primary": {"extends": "context:workspace", "settings": {
                    "model_options": {
                            "endpoint": "https://example.com/v1",
                            "api_key": "${TEST_API_KEY}",
                            "custom_llm_provider": "openai",
                            "api_mode": {
                                "protocol": "chat_completions",
                                "model_info": {
                                    "supports_function_calling": True,
                                    "supports_reasoning": True,
                                },
                                "supports_reasoning": {"reasoning_content": True},
                            },
                    },
                }}},
            }
        ),
        encoding="utf-8",
    )


def test_shared_source_owner_distinguishes_a_b_a_and_deduplicates_adjacent_observation(
    tmp_path: Path,
) -> None:
    first_owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    second_owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    uri = "boxteam://workspace/ws-1/resources/config/workspace_mutable_override"

    first = first_owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    duplicate = second_owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    second = first_owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-b",
        origin="file-watcher",
    )
    third = second_owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )

    assert duplicate == first
    assert (first.source_generation, second.source_generation, third.source_generation) == (
        1,
        2,
        3,
    )
    assert UUID(hex=first.source_event_id.rsplit(":", 1)[1]).version == 7
    assert UUID(hex=first.fanout_id.rsplit(":", 1)[1]).version == 7
    assert first.fanout_id != third.fanout_id
    assert first.source_event_id != third.source_event_id
    assert first_owner.high_water_mark() == 3


async def test_user_source_writer_previews_saves_and_queries_original_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_workspace_roots: tuple[Path, Path],
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    root = config_workspace_roots[0]
    workspace_id = load_or_create_workspace_id(root)
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="model-a")
    original = config_path.read_bytes()
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    with WorkspaceStateStore(workspace_root=root) as store:
        service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path,
            workspace_root=root, workspace_state_store=store, source_owner=owner, source_owner_workspace_id=workspace_id)
        try:
            service.validate_workspace_config()
            document = service.get_source_document(source_key="workspace_mutable_override", gateway_id="gateway-test")
            payload = json.loads(document.jsonc)
            payload["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "共享文件新值"
            text = "// 保留共享来源注释\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            request = ConfigSourceEditRequest(gateway_id=document.gateway_id, workspace_id=workspace_id,
                source_key=document.source_key, jsonc=text, base_file_digest=document.file_digest,
                base_layer_revision=document.layer_revision, base_layer_digest=document.layer_digest,
                expected_active_revision=document.active_revision, expected_active_digest=document.active_digest,
                idempotency_key="shared-source-save")
            preview = await service.preflight_source_edit(source_key=document.source_key, gateway_id=document.gateway_id, request=request)
            candidate_agents = preview.candidate["agents"]
            assert isinstance(candidate_agents, dict)
            assert candidate_agents["default"]["instructions"]["system_prompt"] == "共享文件新值"
            assert config_path.read_bytes() == original and owner.high_water_mark() == 1
            assert store.get_pending_config_candidate(config_domain="workspace") is None
            saved = await service.save_source_edit(source_key=document.source_key, gateway_id=document.gateway_id, request=request)
            assert saved.edit is not None and saved.edit.source_saved and saved.edit.layer_imported
            assert saved.edit.state == "active"
            mask = payload["profiles"]["primary"]["settings"]["model_options"]["api_key"]
            assert config_path.read_text() == text.replace(mask, "${TEST_API_KEY}")
            assert saved.fanout is not None and saved.fanout.source_generation == 2
            assert saved.fanout.coverage == "observed_only" and saved.fanout.workspaces[0].result == "applied"
            assert await service.save_source_edit(source_key=document.source_key, gateway_id=document.gateway_id, request=request) == saved
            with pytest.raises(ConfigConflictError):
                await service.save_source_edit(source_key=document.source_key, gateway_id=document.gateway_id,
                    request=request.model_copy(update={"idempotency_key": "stale-source-save"}))
            payload["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "后续文件版本"
            payload["profiles"]["primary"]["settings"]["model_options"]["api_key"] = "${TEST_API_KEY}"
            config_path.write_text(json.dumps(payload))
            await service.reload()
            queried = service.get_source_document(source_key=document.source_key, gateway_id=document.gateway_id,
                idempotency_key=request.idempotency_key)
            assert queried.fanout is not None and queried.fanout.source_generation == 2
            assert queried.fanout.fanout_id == saved.fanout.fanout_id
            assert owner.high_water_mark() == 3
        finally:
            await service.close()


async def test_saved_restart_source_is_not_rematerialized_or_replayed_by_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_workspace_roots: tuple[Path, Path],
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    workspace = config_workspace_roots[0]
    workspace_id = load_or_create_workspace_id(workspace)
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="model-a")
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    with WorkspaceStateStore(workspace_root=workspace) as store:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace,
            workspace_state_store=store,
            source_owner=owner,
            source_owner_workspace_id=workspace_id,
        )
        apply_calls = 0

        async def require_restart(_previous, _candidate, _scope, _record_side_effect) -> ConfigPublication:
            nonlocal apply_calls
            apply_calls += 1
            raise ConfigRestartRequiredError("runtime requires restart", changed_sections=("agents",))

        service._candidate_applier = require_restart
        try:
            service.validate_workspace_config()
            document = service.get_source_document(
                source_key="workspace_mutable_override", gateway_id="gateway-test",
            )
            payload = json.loads(document.jsonc)
            payload["settings"]["workspace"]["agents"]["default"]["name"] = "Restart Required Agent"
            request = ConfigSourceEditRequest(
                gateway_id=document.gateway_id,
                workspace_id=workspace_id,
                source_key=document.source_key,
                jsonc=json.dumps(payload, ensure_ascii=False),
                base_file_digest=document.file_digest,
                base_layer_revision=document.layer_revision,
                base_layer_digest=document.layer_digest,
                expected_active_revision=document.active_revision,
                expected_active_digest=document.active_digest,
                idempotency_key="restart-source-save",
            )

            saved = await service.save_source_edit(
                source_key=document.source_key,
                gateway_id=document.gateway_id,
                request=request,
            )
            assert saved.edit is not None and saved.edit.state == "pending_restart"
            pending = store.get_pending_config_candidate(
                config_domain="workspace", idempotency_key=request.idempotency_key,
            )
            assert pending is not None and pending.state == "pending_restart" and pending.candidate_ref is not None
            original_candidate = (
                pending.candidate_id,
                pending.candidate_ref,
                pending.state,
                pending.last_attempt_id,
                pending.last_apply_id,
                pending.last_error,
                pending.source_edit,
            )
            original_fanout = owner.get_fanout(
                source_key="workspace:user", source_generation=2, workspace_id=workspace_id,
            )
            assert original_fanout.status == "applying" and original_fanout.result == "restart_required"
            assert apply_calls == 1
            original_journals = store.list_config_apply_journals(config_domain="workspace")
            assert len(original_journals) == 1 and original_journals[0].side_effects == ()

            service._build_candidate_snapshot()
            repeated_fanout = owner.get_fanout(
                source_key="workspace:user", source_generation=2, workspace_id=workspace_id,
            )
            assert repeated_fanout.status == "applying" and repeated_fanout.result == "restart_required"
            assert repeated_fanout.updated_at == original_fanout.updated_at

            await service._reload_from_watcher(candidate_applier=require_restart)
            unchanged = store.get_pending_config_candidate(
                config_domain="workspace", idempotency_key=request.idempotency_key,
            )
            assert unchanged is not None
            assert (
                unchanged.candidate_id,
                unchanged.candidate_ref,
                unchanged.state,
                unchanged.last_attempt_id,
                unchanged.last_apply_id,
                unchanged.last_error,
                unchanged.source_edit,
            ) == original_candidate
            assert apply_calls == 1
            assert store.list_config_apply_journals(config_domain="workspace") == original_journals

            root_config_path = get_workspace_config_path(workspace)
            root_config_path.write_text(json.dumps({
                "config_version": 4,
                "extends": "user:workspace_local",
                "settings": {"workspace": {"agents": {"default": {
                    "instructions": {"system_prompt": "workspace-root update"},
                }}}},
            }), encoding="utf-8")
            root_file = read_stable_config_file(root_config_path)

            await service._reload_from_watcher(candidate_applier=require_restart)

            root_source = store.get_source_layer("workspace_root_mutable_override")
            assert root_source is not None
            assert root_source.presence == "present" and root_source.layer_digest == root_file.digest
            assert root_source.payload == {
                "config_version": 4,
                "extends": "user:workspace_local",
                "settings": {"workspace": {"agents": {"default": {
                    "instructions": {"system_prompt": "workspace-root update"},
                }}}},
            }
            assert apply_calls == 2
            unchanged = store.get_pending_config_candidate(
                config_domain="workspace", idempotency_key=request.idempotency_key,
            )
            assert unchanged is not None
            assert (
                unchanged.candidate_id,
                unchanged.candidate_ref,
                unchanged.state,
                unchanged.last_attempt_id,
                unchanged.last_apply_id,
                unchanged.last_error,
                unchanged.source_edit,
            ) == original_candidate
            final_fanout = owner.get_fanout(
                source_key="workspace:user", source_generation=2, workspace_id=workspace_id,
            )
            assert (final_fanout.status, final_fanout.result, final_fanout.updated_at) == (
                original_fanout.status, original_fanout.result, original_fanout.updated_at,
            )
            assert store.get_config_apply_journal(apply_id=original_journals[0].apply_id) == original_journals[0]
        finally:
            await service.close()


async def test_initial_source_schema_failure_reports_failed_without_active_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_workspace_roots: tuple[Path, Path],
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="model-a")
    payload = json.loads(config_path.read_text())
    payload["unexpected"] = True
    config_path.write_text(json.dumps(payload))
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    with WorkspaceStateStore(workspace_root=config_workspace_roots[0]) as store:
        service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path,
            workspace_root=config_workspace_roots[0], workspace_state_store=store,
            source_owner=owner, source_owner_workspace_id="workspace-a")
        try:
            with pytest.raises(ValueError):
                service.validate_workspace_config()
            result = owner.get_fanout(source_key="workspace:user", source_generation=1, workspace_id="workspace-a")
            assert result.status == result.result == "failed"
            assert store.get_active_config_snapshot("workspace") is None
        finally:
            await service.close()


def test_shared_source_owner_prepares_stopped_workspace_and_preserves_failed_result(
    tmp_path: Path,
) -> None:
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    uri = "boxteam://workspace/ws-1/resources/config/workspace_mutable_override"
    first = owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    second = owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-b",
        origin="file-watcher",
    )

    replay = owner.prepare_fanout(workspace_id="workspace-b")
    assert [record.source_generation for record in replay] == [1, 2]
    owner.record_fanout(
        source_generation=first.source_generation,
        workspace_id="workspace-b",
        status="superseded",
        result="superseded",
    )
    owner.record_fanout(
        source_generation=second.source_generation,
        workspace_id="workspace-b",
        status="conflict",
        result="conflict",
        error="本地 Workspace layer 已变化",
    )
    third = owner.observe(
        uri=uri,
        presence="present",
        layer_digest="digest-c",
        origin="file-watcher",
    )
    owner.prepare_fanout(workspace_id="workspace-b")
    report, = owner.list_fanout(source_generation=third.source_generation)
    assert report.workspace_id == "workspace-b" and report.status == "pending"
    assert owner.get_fanout(
        source_key="workspace:user",
        source_generation=second.source_generation,
        workspace_id="workspace-b",
    ).status == "conflict"


async def test_config_service_materializes_shared_user_source_generation(
    tmp_path: Path,
    monkeypatch,
    config_workspace_roots: tuple[Path, Path],
) -> None:
    first_workspace_root, second_workspace_root = config_workspace_roots
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="info")
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    first_store = WorkspaceStateStore(workspace_root=first_workspace_root)
    second_store = WorkspaceStateStore(workspace_root=second_workspace_root)
    first_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=first_workspace_root,
        workspace_state_store=first_store,
        source_owner=owner,
        source_owner_workspace_id="workspace-a",
    )
    second_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=second_workspace_root,
        workspace_state_store=second_store,
        source_owner=owner,
        source_owner_workspace_id="workspace-b",
    )
    try:
        first_service.get_revision()
        second_service.get_revision()
        assert owner.high_water_mark() == 1
        assert first_store.get_source_layer("workspace_mutable_override").source_generation == 1
        assert second_store.get_source_layer("workspace_mutable_override").source_generation == 1

        _write_config(config_path, model_name="warning")
        assert await first_service.reload() is True
        assert await second_service.reload() is True
        assert owner.high_water_mark() == 2

        _write_config(config_path, model_name="info")
        assert await first_service.reload() is True
        assert await second_service.reload() is True
        assert owner.high_water_mark() == 3
        assert first_store.get_source_layer("workspace_mutable_override").source_generation == 3
        assert second_store.get_source_layer("workspace_mutable_override").source_generation == 3
        reports = owner.list_fanout(source_generation=3)
        assert {record.workspace_id for record in reports} == {"workspace-a", "workspace-b"}
        assert {record.status for record in reports} == {"applied"}
    finally:
        first_store.close()
        second_store.close()
        owner.close()


def test_config_service_catches_up_stopped_workspace_from_source_high_water(
    tmp_path: Path,
    monkeypatch,
    config_workspace_roots: tuple[Path, Path],
) -> None:
    workspace_root = config_workspace_roots[0]
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="info")
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
            source_owner=owner,
            source_owner_workspace_id="stopped-workspace",
        )
        service.get_revision()
        assert store.get_source_layer("workspace_mutable_override").source_generation == 1

        _write_config(config_path, model_name="warning")
        warning_digest = read_stable_config_file(config_path).digest
        assert warning_digest is not None
        owner.observe(
            uri="boxteam://workspace/stopped-workspace/resources/config/workspace_mutable_override",
            presence="present",
            layer_digest=warning_digest,
            origin="file-watcher",
        )
        _write_config(config_path, model_name="info")
        restored_digest = read_stable_config_file(config_path).digest
        assert restored_digest is not None
        owner.observe(
            uri="boxteam://workspace/stopped-workspace/resources/config/workspace_mutable_override",
            presence="present",
            layer_digest=restored_digest,
            origin="file-watcher",
        )
        assert owner.high_water_mark() == 3

        restarted = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
            source_owner=owner,
            source_owner_workspace_id="stopped-workspace",
        )
        restarted.get_revision()

        assert store.get_source_layer("workspace_mutable_override").source_generation == 3
        assert owner.get_fanout(
            source_key="workspace:user",
            source_generation=2,
            workspace_id="stopped-workspace",
        ).status == "superseded"
        assert owner.get_fanout(
            source_key="workspace:user",
            source_generation=3,
            workspace_id="stopped-workspace",
        ).status == "applying"
        restarted.validate_workspace_config()
        assert owner.get_fanout(source_key="workspace:user", source_generation=3,
            workspace_id="stopped-workspace").status == "applied"
    finally:
        store.close()
        owner.close()


@pytest.mark.parametrize("outcome", [
    "applied", "unchanged", "conflict", "failed", "builder_failed", "source_read_failed", "restart_required", "restart_cleanup_failed",
])
async def test_shared_source_fanout_reports_candidate_application_not_layer_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    config_workspace_roots: tuple[Path, Path], outcome: str,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, model_name="before")
    workspace = config_workspace_roots[0]
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path,
        workspace_root=workspace, workspace_state_store=store, source_owner=owner,
        source_owner_workspace_id="workspace-a")
    if outcome == "unchanged":
        (workspace / ".boxteam/workspace.jsonc").write_text(json.dumps({
            "config_version": 4, "extends": "user:workspace_local",
            "settings": {"workspace": {"default_agent": "default"}},
        }))
    service.validate_workspace_config()
    active = store.get_active_config_snapshot("workspace")
    assert active is not None
    if outcome == "unchanged":
        payload = json.loads(config_path.read_text())
        # 只改变被工作区层遮蔽的字段，来源版本仍须前进。
        payload["settings"]["workspace"]["default_agent"] = "unused-default"
        config_path.write_text(json.dumps(payload))
    else:
        _write_config(config_path, model_name="after")
        if outcome == "builder_failed":
            payload = json.loads(config_path.read_text())
            payload["unexpected"] = True
            config_path.write_text(json.dumps(payload))
        if outcome == "source_read_failed":
            (workspace / ".boxteam/workspace.jsonc").write_text("{broken")

    async def apply(_previous, _candidate, _scope, _record_side_effect) -> ConfigPublication:
        progress = owner.get_fanout(source_key="workspace:user", source_generation=2,
            workspace_id="workspace-a")
        assert progress.status == "applying" and progress.result == "materialized"
        assert store.get_active_config_snapshot("workspace") == active
        if outcome == "conflict":
            raise ConfigConflictError("runtime candidate baseline changed")
        if outcome == "failed":
            raise RuntimeError("runtime refused candidate")
        if outcome == "builder_failed":
            raise AssertionError("invalid schema candidate must not reach the applier")
        if outcome in {"restart_required", "restart_cleanup_failed"}:
            raise ConfigRestartRequiredError("runtime requires restart", changed_sections=("llm",))
        return lambda: None

    try:
        if outcome in {"conflict", "failed", "builder_failed", "source_read_failed", "restart_required", "restart_cleanup_failed"}:
            expected_error = (
                ValueError if outcome in {"builder_failed", "source_read_failed"}
                else ConfigConflictError if outcome == "conflict"
                else RuntimeError if outcome == "failed"
                else ConfigRestartRequiredError
            )
            with pytest.raises(expected_error):
                await service.reload(candidate_applier=apply)
            assert store.get_active_config_snapshot("workspace") == active
            if outcome == "source_read_failed":
                # 入口文档尚未解析，候选没有到达共享 user 来源。
                with pytest.raises(RuntimeError, match="fanout 提交后无法读取"):
                    owner.get_fanout(source_key="workspace:user", source_generation=2,
                        workspace_id="workspace-a")
                return
        else:
            assert await service.reload(candidate_applier=apply) is (outcome == "applied")
        result = owner.get_fanout(source_key="workspace:user", source_generation=2,
            workspace_id="workspace-a")
        expected_result = {
            "builder_failed": "failed",
            "source_read_failed": "failed",
            "restart_cleanup_failed": "restart_required",
        }.get(outcome, outcome)
        assert result.result == expected_result
        assert result.status == {"conflict": "conflict", "failed": "failed", "builder_failed": "failed", "source_read_failed": "failed", "restart_required": "applying",
            "restart_cleanup_failed": "applying"}.get(outcome, "applied")
        assert result.error is not None if outcome in {"conflict", "failed", "builder_failed", "source_read_failed", "restart_required", "restart_cleanup_failed"} else result.error is None
        reports = owner.list_fanout(source_generation=2)
        assert (all(record.status == "applied" for record in reports)
                is (outcome not in {"conflict", "failed", "builder_failed", "source_read_failed", "restart_required", "restart_cleanup_failed"}))
        if outcome in {"restart_required", "restart_cleanup_failed"}:
            pending = store.get_pending_config_candidate(config_domain="workspace")
            assert pending is not None and pending.candidate_ref is not None
            proof = service._pending_restart.build_pending_restart_health_proof(
                candidate_ref=pending.candidate_ref, pending=pending)
            if outcome == "restart_cleanup_failed":
                def fail_cleanup(_payload: dict[str, object]) -> None:
                    raise RuntimeError("private input cleanup failed")

                monkeypatch.setattr(service, "_remove_released_plugin_private_inputs", fail_cleanup)
                with pytest.raises(RuntimeError, match="private input cleanup failed"):
                    service.resolve_pending_restart(candidate_ref=pending.candidate_ref, health_proof=proof)
                active_after_cleanup_failure = store.get_active_config_snapshot("workspace")
                assert active_after_cleanup_failure is not None
                assert active_after_cleanup_failure.source_generation == 2
                assert active_after_cleanup_failure.state == "active"
                assert active_after_cleanup_failure.effective_digest == pending.effective_digest
            else:
                service.resolve_pending_restart(candidate_ref=pending.candidate_ref, health_proof=proof)
            assert owner.get_fanout(source_key="workspace:user", source_generation=2,
                workspace_id="workspace-a").result == "applied"
            assert {record.status for record in owner.list_fanout(source_generation=2)} == {"applied"}
    finally:
        await service.close()
        store.close()
        owner.close()
