"""真实配置 owner、SQLite 与 ASGI 状态的故障注入；不代替运行时启动验收。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Request, Response

from app.api import config as config_api
from app.api.deps import get_config_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@dataclass(frozen=True)
class ConfigCase:
    service: ConfigService
    store: WorkspaceStateStore
    app: FastAPI
    config_path: Path
    payload: dict[str, object]
    workspace: Path


@pytest.fixture
async def config_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConfigCase]:
    context = TestRunContext.from_test_file(Path(__file__))
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    monkeypatch.setenv("TEST_CONFIG_KEY", "test-key")
    payload: dict[str, object] = {
        "config_version": 2,
        "llm": {"providers": [{
            "id": "primary", "endpoint": "https://example.com/v1", "model": "model-a",
            "api_key": "${TEST_CONFIG_KEY}", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {
                "supports_function_calling": True, "supports_reasoning": True,
            }, "supports_reasoning": {"reasoning_content": True}},
        }]},
        "default_agent": "default",
        "agents": {"default": {
            "name": "Default Agent", "instructions": {"system_prompt": "hello"},
            "model": {"primary_provider": "primary", "fallback_providers": []},
        }},
    }
    path = tmp_path / "workspace.jsonc"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(
        config_dir=Path.cwd() / "configs", config_path=path,
        workspace_root=workspace, workspace_state_store=store,
    )
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(config_api.router, prefix="/api/v1")
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    app.dependency_overrides[get_config_service] = lambda: service
    try:
        service.validate_workspace_config()
        yield ConfigCase(service, store, app, path, payload, workspace)
    finally:
        await service.close()
        store.close()


@pytest.mark.parametrize("native", ["/private-config/source.jsonc", r"C:\private-config\source.jsonc", r"\\server\private-config\source.jsonc"])
@pytest.mark.parametrize("phase", ["apply", "pending_resolve"])
@pytest.mark.asyncio
async def test_failure_state_persists_facts_and_recovers_through_original_owner(
    config_case: ConfigCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native: str, phase: str,
) -> None:
    case = config_case
    active = case.store.get_active_config_snapshot("workspace")
    assert active is not None
    revision = case.service.get_revision()
    candidate = case.payload | {"agents": {"default": {
        "name": "Updated Agent", "instructions": {"system_prompt": "hello"},
        "model": {"primary_provider": "primary", "fallback_providers": []},
    }}}
    case.config_path.write_text(json.dumps(candidate), encoding="utf-8")
    original = PermissionError(13, f"配置故障 {native}", native)
    original.__cause__ = RuntimeError(f"完整原因 {native}")

    async def reject_apply(*_args: object) -> NoReturn:
        raise original

    async def require_restart(*_args: object) -> NoReturn:
        raise ConfigRestartRequiredError("需要受控重启", changed_sections=("agents",))

    if phase == "apply":
        with pytest.raises(PermissionError) as caught:
            await case.service.reload(candidate_applier=reject_apply)
    else:
        with pytest.raises(ConfigRestartRequiredError):
            await case.service.reload(candidate_applier=require_restart)
        pending = case.store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None and pending.candidate_ref is not None
        # 证明由原 coordinator 生成；这里不声称已实际启动另一个 runtime。
        proof = case.service._pending_restart.build_pending_restart_health_proof(
            candidate_ref=pending.candidate_ref, pending=pending,
        )

        def reject_promotion(**_kwargs: object) -> NoReturn:
            raise original

        with monkeypatch.context() as patch:
            patch.setattr(case.store, "promote_active_config_snapshot", reject_promotion)
            with pytest.raises(PermissionError) as caught:
                case.service.resolve_pending_restart(candidate_ref=pending.candidate_ref, health_proof=proof)
    assert caught.value is original
    failed = case.store.get_pending_config_candidate(config_domain="workspace")
    assert failed is not None and failed.last_apply_id is not None
    expected_state = "rejected" if phase == "apply" else "recovery_required"
    expected_stage = "apply_failed" if phase == "apply" else "pending_resolve"
    assert failed.state == expected_state
    detail = f"Workspace 配置应用失败: stage={expected_stage}, error=PermissionError, errno=13"
    assert failed.last_error == detail
    journal = case.store.get_config_apply_journal(apply_id=failed.last_apply_id)
    assert journal is not None and journal.last_error == detail
    assert journal.state == ("failed" if phase == "apply" else "recovery_required")
    assert case.store.get_config_apply_claim(config_domain="workspace") is None
    assert case.store.get_active_config_snapshot("workspace") == active
    assert case.service.get_revision() == revision
    events = case.store.list_config_events(config_domain="workspace")
    assert [event.result for event in events] == (["apply_failed"] if phase == "apply" else ["restart_required", "recovery_required"])
    assert events[-1].error == detail
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert native in caplog.text and "完整原因" in caplog.text

    async with AsyncClient(transport=ASGITransport(app=case.app), base_url="http://test") as client:
        responses = [await client.get(path, headers={"X-Request-ID": "config-failed"}) for path in (
            "/api/v1/config/reload-status", "/api/v1/config", "/api/v1/config/events",
        )]
        for response in responses:
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "config-failed"
            assert "private-config" not in json.dumps(response.json())
        status = responses[0].json()["data"]
        assert status["state"] == expected_state and status["healthy"] is False
        assert status["last_error"] == detail
        assert responses[1].json()["data"]["metadata"]["reload"]["last_error"] == detail
        assert "private-config" not in json.dumps([asdict(failed), asdict(journal), *[asdict(event) for event in events]], default=str)

        if phase == "apply":
            await case.service.reload()
        else:
            assert failed.candidate_ref is not None
            proof = case.service._pending_restart.build_pending_restart_health_proof(
                candidate_ref=failed.candidate_ref, pending=failed,
            )
            response = await client.post(
                "/api/v1/config/pending/resolve", params={"candidate_ref": failed.candidate_ref},
                json=proof, headers={"X-Request-ID": "config-recovered"},
            )
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "config-recovered"
        resolved = case.store.get_pending_config_candidate(config_domain="workspace")
        assert resolved is not None and resolved.state == "active"
        assert resolved.candidate_id == failed.candidate_id
        assert case.store.get_config_apply_claim(config_domain="workspace") is None
        assert [event.result for event in case.store.list_config_events(config_domain="workspace")] == [*[event.result for event in events], "applied"]

    await case.service.close()
    case.store.close()
    with WorkspaceStateStore(workspace_root=case.workspace) as cold:
        assert cold.get_config_apply_journal(apply_id=failed.last_apply_id) == journal
        assert cold.get_pending_config_candidate(config_domain="workspace") == resolved
        assert cold.get_config_apply_claim(config_domain="workspace") is None
        assert cold.list_config_events(config_domain="workspace")[:len(events)] == events


@pytest.mark.parametrize("native", ["/private-restart/backend", r"C:\private-restart\backend", r"\\server\private-restart\backend"])
@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.asyncio
async def test_gateway_restart_failure_posts_facts_to_real_workspace_owner(
    config_case: ConfigCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native: str, recovered: bool, sink_fails: bool,
) -> None:
    case = config_case
    active = case.store.get_active_config_snapshot("workspace")
    candidate = case.payload | {"agents": {"default": {
        "name": "Restart Agent", "instructions": {"system_prompt": "hello"},
        "model": {"primary_provider": "primary", "fallback_providers": []},
    }}}
    case.config_path.write_text(json.dumps(candidate), encoding="utf-8")

    async def require_restart(*_args: object) -> NoReturn:
        raise ConfigRestartRequiredError("需要受控重启", changed_sections=("agents",))

    with pytest.raises(ConfigRestartRequiredError):
        await case.service.reload(candidate_applier=require_restart)
    pending = case.store.get_pending_config_candidate(config_domain="workspace")
    assert pending is not None and pending.candidate_ref is not None
    original = PermissionError(13, f"重启失败 {native}", native)
    original.__cause__ = RuntimeError(f"原始重启原因 {native}")
    requests: list[dict[str, object]] = []
    replies: list[dict[str, object]] = []
    workspace_id = load_or_create_workspace_id(case.workspace)

    async def capture_request(request: Request) -> None:
        assert request.url.path == "/api/v1/config/pending/restart-failed"
        assert request.url.params["candidate_ref"] == pending.candidate_ref
        assert request.headers["X-BoxTeam-Workspace-Id"] == workspace_id
        assert request.headers["X-Request-ID"] == "restart-failed"
        requests.append(json.loads(request.content))

    async def capture_response(response: Response) -> None:
        await response.aread()
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "restart-failed"
        replies.append(response.json())

    def workspace_client(*, timeout: float, trust_env: bool) -> AsyncClient:
        return AsyncClient(
            timeout=timeout, trust_env=trust_env, transport=ASGITransport(app=case.app),
            event_hooks={"request": [capture_request], "response": [capture_response]},
        )

    def reject_log(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(5, f"日志故障 {native}", native)

    with closing(GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")) as registry:
        controller = GatewayWorkspaceRuntimeController(registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs")
        with monkeypatch.context() as patch:
            patch.setattr("app.gateway.runtime.controller.httpx.AsyncClient", workspace_client)
            if sink_fails:
                patch.setattr("app.core.failures.logger.error", reject_log)
            await controller._record_pending_restart_failure(
                WorkspaceTarget(workspace_id="member", name="Member", root_path=str(case.workspace),
                                backend_url="http://workspace-backend", backend_workspace_id=workspace_id, connection_kind="local"),
                candidate_ref=pending.candidate_ref, error=original,
                old_runtime_recovered=recovered, request_id="restart-failed",
            )
    detail = "Workspace 配置应用失败: stage=workspace_restart, error=PermissionError, errno=13"
    if sink_fails:
        detail += ", diagnostic_sink_error=OSError"
        assert any("诊断日志输出失败" in note and native in note for note in original.__notes__)
    else:
        assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
        assert native in caplog.text and "原始重启原因" in caplog.text
    assert requests == [{"error": detail, "old_runtime_recovered": recovered}]
    assert len(replies) == 1
    failed = case.store.get_pending_config_candidate(config_domain="workspace")
    assert failed is not None and failed.candidate_id == pending.candidate_id
    assert failed.state == ("pending_restart" if recovered else "recovery_required")
    assert failed.last_error == detail
    assert case.store.get_config_apply_claim(config_domain="workspace") is None
    assert case.store.get_active_config_snapshot("workspace") == active
    events = case.store.list_config_events(config_domain="workspace")
    assert [event.result for event in events] == ["restart_required", "restart_failed" if recovered else "recovery_required"]
    assert events[-1].error == detail
    assert "private-restart" not in json.dumps([requests, replies, asdict(failed), *[asdict(event) for event in events]], default=str)
    retried = case.service.retry_pending_restart(candidate_ref=pending.candidate_ref)
    assert retried.state == "pending_restart" and retried.candidate_id == pending.candidate_id
    await case.service.close()
    case.store.close()
    with WorkspaceStateStore(workspace_root=case.workspace) as cold:
        assert cold.get_active_config_snapshot("workspace") == active
        assert cold.list_config_events(config_domain="workspace")[:len(events)] == events
