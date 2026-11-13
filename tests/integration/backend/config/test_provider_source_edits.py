"""Provider CRUD 经原来源 HTTP 预检、CAS 与真实配置 owner 提交。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import config as config_api
from app.api.deps import get_config_service, verify_local_token
from app.core.mounted_workspaces import MountedWorkspace
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.main import workspace_http_exception_handler
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config.source_document import parse_source_text
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

SOURCE = "workspace_mutable_override"
GATEWAY = "gateway_" + "7" * 32
URL = f"/api/v1/config/sources/{SOURCE}"


@dataclass(frozen=True)
class ProviderCase:
    client: AsyncClient
    service: ConfigService
    source: Path
    workspace: Path

    async def document(self, *, url: str = URL, key: str | None = None) -> dict:
        response = await self.client.get(url, params={"idempotency_key": key} if key else {})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
        return response.json()["data"]


@pytest.fixture
async def provider_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[ProviderCase]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    load_or_create_workspace_id(workspace)
    monkeypatch.delenv("PROVIDER_SOURCE_UNSET_KEY", raising=False)
    providers = [{
        "id": name, "endpoint": f"https://{name}.example/v1", "model": f"{name}-model",
        "api_key": f"{name}-test-secret", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {
            "supports_function_calling": True, "supports_reasoning": False,
        }, "supports_reasoning": {"reasoning_content": False}},
    } for name in ("unused", "primary", "fallback")]
    providers[2]["api_key"] = "${PROVIDER_SOURCE_UNSET_KEY}"
    payload = {"config_version": 2, "llm": {"providers": providers}, "default_agent": "default",
               "agents": {"default": {"name": "Default", "instructions": {"system_prompt": "test"},
                   "model": {"primary_provider": "primary", "fallback_providers": ["fallback"]}},
                   **{name: {"model": {"primary_provider": "primary", "fallback_providers": []}}
                      for name in ("coder", "reviewer", "researcher")}}}
    source = tmp_path / "workspace.jsonc"
    source.write_text("// 保留来源注释\n" + json.dumps(payload, ensure_ascii=False, indent=2)
                      .replace("primary-test-secret", "primary-test-secr\\u0065t") + "\n", encoding="utf-8")
    with WorkspaceStateStore(workspace_root=workspace) as store:
        service = ConfigService(config_dir=Path.cwd() / "configs", config_path=source,
                                workspace_root=workspace, workspace_state_store=store)
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.exception_handler(HTTPException)(workspace_http_exception_handler)
        app.include_router(config_api.router, prefix="/api/v1")
        app.dependency_overrides[verify_local_token] = lambda: "test-token"
        app.dependency_overrides[get_config_service] = lambda: service
        try:
            service.validate_workspace_config()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                   headers={"X-BoxTeam-Gateway-Id": GATEWAY, "X-Request-ID": "provider-edit"}) as client:
                yield ProviderCase(client, service, source, workspace)
        finally:
            await service.close()


def _request(document: dict, payload: dict[str, object], key: str) -> dict:
    return {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"],
            "source_key": document["source_key"], "jsonc": "// 保留来源注释\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"], "idempotency_key": key}


async def test_provider_reorder_create_remove_and_cas_share_original_source_writer(provider_case: ProviderCase) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    # 未展开的缺失环境凭据不应阻止定义管理。
    providers = payload["llm"]["providers"]
    providers[:] = [providers[2], providers[1]]
    original = case.source.read_bytes()
    request = _request(document, payload, "provider-reorder")
    preview = await case.client.post(URL + "/preflight", json=request)
    assert preview.status_code == 200, preview.text
    assert case.source.read_bytes() == original
    saved = await case.client.put(URL, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.headers["X-Request-ID"] == saved.json()["request_id"] == "provider-edit"
    assert saved.json()["data"]["edit"]["state"] == "active"
    assert "primary-test-secret" not in saved.text and "PROVIDER_SOURCE_UNSET_KEY" not in saved.text
    raw = case.source.read_text()
    assert raw.startswith("// 保留来源注释\n") and "primary-test-secr\\u0065t" in raw
    assert [(item["id"], item["api_key"]) for item in parse_source_text(raw)["llm"]["providers"]] == [
        ("fallback", "${PROVIDER_SOURCE_UNSET_KEY}"), ("primary", "primary-test-secret"),
    ]
    repeated = await case.client.put(URL, json=request)
    assert repeated.status_code == 200 and repeated.json()["data"]["edit"] == saved.json()["data"]["edit"]
    stale = await case.client.put(URL, json={**request, "idempotency_key": "provider-stale"})
    assert stale.status_code == 409 and case.source.read_text() == raw
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    added = {**payload["llm"]["providers"][1], "id": "created", "api_key": "created-test-secret"}
    payload["llm"]["providers"].append(added)
    created = await case.client.put(URL, json=_request(document, payload, "provider-create"))
    assert created.status_code == 200, created.text
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    assert [item["id"] for item in payload["llm"]["providers"]] == ["fallback", "primary", "created"]
    payload["llm"]["providers"].pop()
    removed = await case.client.put(URL, json=_request(document, payload, "provider-remove"))
    assert removed.status_code == 200, removed.text
    assert "created-test-secret" not in case.source.read_text()


@pytest.mark.parametrize("change", ["primary", "fallback", "duplicate", "duplicate-explicit", "invalid-item", "endpoint", "identity", "foreign-source", "config-default", "workspace-default"])
async def test_source_preflight_and_save_reject_invalid_current_references_and_secret_binding(
    provider_case: ProviderCase, change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    providers = payload["llm"]["providers"]
    if change in {"primary", "fallback"}:
        providers[:] = [item for item in providers if item["id"] != change]
    elif change == "invalid-item":
        providers.append(None)
    elif change.startswith("duplicate"):
        duplicate = dict(providers[1])
        if change == "duplicate-explicit":
            duplicate["api_key"] = "explicit-test-key"
        providers.append(duplicate)
        if change == "duplicate-explicit":
            for provider in providers:
                provider["api_key"] = "explicit-test-key"
    elif change == "endpoint":
        providers[1]["endpoint"] = "https://changed.example/v1"
    elif change == "identity":
        providers[1]["id"] = "changed"
    elif change == "foreign-source":
        url = "/api/v1/config/sources/workspace_root_mutable_override"
        document = await case.document(url=url)
    elif change == "config-default":
        payload["default_agent"] = "missing"
    else:
        with monkeypatch.context() as credentials:
            credentials.setenv("PROVIDER_SOURCE_UNSET_KEY", "test-runtime-key")
            case.service.set_workspace_default_provider("default", "primary")
        for agent in payload["agents"].values():
            agent["model"]["primary_provider"] = "fallback"
        providers[:] = [item for item in providers if item["id"] != "primary"]
    url = f'/api/v1/config/sources/{document["source_key"]}'
    original = case.source.read_bytes()
    snapshot = case.service.get_snapshot()
    request = _request(document, payload, "provider-rejected")
    for method, suffix in (("POST", "/preflight"), ("PUT", "")):
        response = await case.client.request(method, url + suffix, json=request)
        assert response.status_code == 422, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
        assert "primary-test-secret" not in response.text
        if change == "workspace-default":
            assert "field=provider_by_agent" in response.text
    assert case.source.read_bytes() == original and case.service.get_snapshot() is snapshot
    assert (await case.document(key="provider-rejected"))["edit"] is None


async def test_reordered_source_retries_same_accepted_edit_after_replace_failure(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["llm"]["providers"].reverse()
    request = _request(document, payload, "provider-reorder-recover")

    def fail_after_replace():
        raise RuntimeError("来源已替换，导入前故障")

    with monkeypatch.context() as fault:
        fault.setattr(case.service._snapshot_store, "_candidate_builder", fail_after_replace)
        failed = await case.client.put(URL, json=request)
    assert failed.status_code == 500, failed.text
    document = await case.document(key="provider-reorder-recover")
    assert document["edit"]["source_saved"] and document["edit"]["state"] == "candidate_validated"
    candidate_id = document["edit"]["candidate_id"]
    recovered = await case.client.put(URL, json=request)
    assert recovered.status_code == 200, recovered.text
    edit = recovered.json()["data"]["edit"]
    assert edit["candidate_id"] == candidate_id and edit["state"] == "active"
    assert [(item["id"], item["api_key"]) for item in parse_source_text(case.source.read_text())["llm"]["providers"]] == [
        ("fallback", "${PROVIDER_SOURCE_UNSET_KEY}"), ("primary", "primary-test-secret"), ("unused", "unused-test-secret"),
    ]


@pytest.mark.parametrize("resume", ["retry", "watcher"])
async def test_saved_candidate_rechecks_defaults_changed_after_source_replace(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, resume: str,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["llm"]["providers"][:] = [item for item in payload["llm"]["providers"] if item["id"] != "fallback"]
    payload["agents"]["default"]["model"]["fallback_providers"] = []
    request = _request(document, payload, "provider-defaults-recover")

    async def fail_apply(*_args):
        raise RuntimeError("来源已导入，应用候选前故障")

    with monkeypatch.context() as fault:
        fault.setattr(case.service, "_candidate_applier", fail_apply)
        failed = await case.client.put(URL, json=request)
    assert failed.status_code == 500, failed.text
    with monkeypatch.context() as credentials:
        credentials.setenv("PROVIDER_SOURCE_UNSET_KEY", "test-runtime-key")
        case.service.set_workspace_default_provider("default", "fallback")
    snapshot = case.service.get_snapshot()
    if resume == "retry":
        with pytest.raises(ValueError, match="field=provider_by_agent"):
            await case.service.retry_source_edit(source_key=SOURCE, gateway_id=GATEWAY, idempotency_key=request["idempotency_key"])
    else:
        with pytest.raises(ValueError, match="field=provider_by_agent"):
            await case.service.reload()
    assert case.service.get_snapshot() is snapshot
    document = await case.document(key=request["idempotency_key"])
    assert document["edit"]["source_saved"] and document["edit"]["state"] == "rejected"


@pytest.mark.parametrize("resume", ["retry", "resolve", "startup"])
async def test_pending_restart_source_rechecks_current_defaults_before_effect(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, resume: str,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["llm"]["providers"][:] = [item for item in payload["llm"]["providers"] if item["id"] != "fallback"]
    payload["agents"]["default"]["model"]["fallback_providers"] = []
    request = _request(document, payload, "provider-pending-restart")

    async def require_restart(*_args):
        raise ConfigRestartRequiredError("候选等待重启", changed_sections=("llm",))

    with monkeypatch.context() as pending_runtime:
        pending_runtime.setattr(case.service, "_candidate_applier", require_restart)
        saved = await case.client.put(URL, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["edit"]["state"] == "pending_restart"
    store = case.service._workspace_state_store
    pending = store.get_pending_config_candidate(config_domain="workspace", idempotency_key=request["idempotency_key"])
    proof = case.service._pending_restart.build_pending_restart_health_proof(candidate_ref=pending.candidate_ref, pending=pending)
    with monkeypatch.context() as credentials:
        credentials.setenv("PROVIDER_SOURCE_UNSET_KEY", "test-runtime-key")
        case.service.set_workspace_default_provider("default", "fallback")
    snapshot = case.service.get_snapshot()
    if resume == "startup":
        mount = MountedWorkspace(document["workspace_id"], case.workspace.resolve(),
            config_candidate_ref=pending.candidate_ref, config_generation=pending.target_generation,
            config_fencing_token=pending.fencing_token)
        restarted = ConfigService(config_dir=Path.cwd() / "configs", config_path=case.source,
            workspace_root=case.workspace, workspace_state_store=store, startup_mount=mount)
        try:
            with pytest.raises(ValueError, match="field=provider_by_agent"):
                restarted.validate_workspace_config()
        finally:
            await restarted.close()
    else:
        response = await case.client.post(f"/api/v1/config/pending/{resume}", params={"candidate_ref": pending.candidate_ref},
                                         json=proof if resume == "resolve" else None)
        assert response.status_code == 409 and "field=provider_by_agent" in response.text, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
    assert case.service.get_snapshot() is snapshot
    assert store.get_pending_config_candidate(config_domain="workspace", idempotency_key=request["idempotency_key"]) == pending
