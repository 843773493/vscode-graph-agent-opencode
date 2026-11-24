"""Provider／Agent 定义经原来源 HTTP 预检、CAS 与真实配置 owner 提交。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
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
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config.source_document import parse_source_text
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

SOURCE = "workspace_mutable_override"
GATEWAY = "gateway_" + "7" * 32
URL = f"/api/v1/config/sources/{SOURCE}"
PRIMARY = {"provider_id": "edit-primary", "model_id": "main"}
FALLBACK = {"provider_id": "edit-fallback", "model_id": "main"}


@dataclass(frozen=True)
class ProviderCase:
    client: AsyncClient
    service: ConfigService
    source: Path
    workspace: Path
    app: FastAPI

    async def document(self, *, url: str = URL, key: str | None = None) -> dict:
        response = await self.client.get(url, params={"idempotency_key": key} if key else {})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
        return response.json()["data"]

    async def save(self, payload: dict, key: str, *, url: str = URL) -> dict:
        document = await self.document(url=url)
        request = _request(document, payload, key)
        preview = await self.client.post(url + "/preflight", json=request)
        assert preview.status_code == 200, preview.text
        saved = await self.client.put(url, json=request)
        assert saved.status_code == 200, saved.text
        assert saved.headers["X-Request-ID"] == saved.json()["request_id"] == "provider-edit"
        assert saved.json()["data"]["edit"]["source_saved"]
        return saved.json()["data"]


@pytest.fixture
async def provider_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[ProviderCase]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    load_or_create_workspace_id(workspace)
    monkeypatch.delenv("PROVIDER_SOURCE_UNSET_KEY", raising=False)
    options = {name: {
        "endpoint": f"https://{name}.example/v1", "custom_llm_provider": "openai",
        "api_key": "secret:sec_" + "1" * 32,
        "api_mode": {"protocol": "chat_completions", "model_info": {
            "supports_function_calling": True, "supports_reasoning": False,
        }, "supports_reasoning": {"reasoning_content": False}},
    } for name in ("unused", "primary", "fallback")}
    options["fallback"]["api_key"] = "${PROVIDER_SOURCE_UNSET_KEY}"
    providers = {f"edit-{name}": {"config_ref": f"#/profiles/{name}",
        "models": {"main": {"model": f"{name}-model", "config_ref": f"context:provider/edit-{name}"}}}
        for name in options}
    payload = {"config_version": 4, "extends": "builtin:workspace", "settings": {"workspace": {
        "llm": {"providers": providers}, "default_agent": "default",
               "agents": {"default": {"name": "Default", "instructions": {"system_prompt": "test"},
                   "model": {"primary": PRIMARY, "fallbacks": [FALLBACK]}},
                   **{name: {"model": {"primary": PRIMARY, "fallbacks": []}}
                      for name in ("coder", "reviewer", "researcher")}}}},
        "profiles": {name: {"extends": "context:workspace", "settings": {"model_options": value}}
            for name, value in options.items()}}
    install_user_configuration(config_root=tmp_path, profile="default", workspace_root=workspace)
    source = tmp_path / "workspace.jsonc"
    source.write_text("// 保留来源注释\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
                yield ProviderCase(client, service, source, workspace, app)
        finally:
            await service.close()


def _request(document: dict, payload: dict[str, object], key: str) -> dict:
    return {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"],
            "source_key": document["source_key"], "jsonc": "// 保留来源注释\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"], "idempotency_key": key}


async def test_source_validation_delivers_safe_issues_with_original_source_uri(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    document = await case.document()
    original, active = case.source.read_bytes(), case.service.get_snapshot()
    source_uri = next(node["source_uri"] for node in document["nodes"] if node["source_key"] == SOURCE)
    for validation_kind in ("document", "options", "extra"):
        payload = parse_source_text(document["jsonc"])
        if validation_kind == "document":
            payload["profiles"]["field.with.dot/and.slash"] = {"extends": 7, "settings": {}}
            expected = ["profiles", "field.with.dot/and.slash", "extends"]
        elif validation_kind == "options":
            payload["profiles"]["primary"]["settings"]["model_options"].pop("endpoint")
            expected = ["model_options"]
        else:
            payload["private-extra-field"] = "private-input"
            expected = ["<extra>"]
        request = _request(document, payload, "source-path-" + validation_kind)
        for method, suffix in (("POST", "/preflight"), ("PUT", "")):
            response = await case.client.request(method, URL + suffix, json=request)
            assert response.status_code == 422, response.text
            result = response.json()
            assert response.headers["X-Request-ID"] == result["request_id"] == "provider-edit"
            detail = result["detail"]
            assert detail["message"].startswith("配置节点校验失败:")
            assert detail["details"]["source_uri"] == source_uri
            assert "cause" not in detail["details"]
            issue, = detail["details"]["issues"]
            assert issue["path"] == expected
            assert issue["path_exact"] is (validation_kind == "document")
            assert "private-extra-field" not in response.text and "private-input" not in response.text
    assert case.source.read_bytes() == original and case.service.get_snapshot() is active


async def test_context_window_round_trips_source_and_removing_model_override_restores_provider(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    payload = parse_source_text((await case.document())["jsonc"])
    payload["profiles"]["primary"]["settings"]["model_options"]["api_mode"]["model_info"].update(
        max_input_tokens=64000, context_window=128000,
    )
    payload["profiles"]["capacity-model"] = {"extends": "context:provider/edit-primary", "settings": {
        "model_options": {"api_mode": {"model_info": {"context_window": 96000}}},
    }}
    payload["settings"]["workspace"]["llm"]["providers"]["edit-primary"]["models"]["main"]["config_ref"] = "#/profiles/capacity-model"
    for override, expected in ((True, 96000), (False, 128000)):
        if not override:
            payload["profiles"]["capacity-model"]["settings"]["model_options"]["api_mode"]["model_info"].pop("context_window")
        saved = await case.save(payload, f"context-window-{override}")
        assert saved["edit"]["state"] == "active"
        document = await case.document()
        persisted = parse_source_text(document["jsonc"])
        assert persisted == parse_source_text(case.source.read_text(encoding="utf-8"))
        local = persisted["profiles"]["capacity-model"]["settings"]["model_options"]["api_mode"]["model_info"]
        assert local == ({"context_window": 96000} if override else {})
        response = await case.client.post(URL + "/preflight", params=PRIMARY,
            json=_request(document, persisted, f"inspect-context-window-{override}"))
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        for projection in ("model_options", "active_model_options"):
            options = result[projection]["options"]
            assert options["api_mode"]["model_info"]["context_window"] == expected
            assert options["api_mode"]["model_info"]["max_input_tokens"] == 64000
            assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(
                options.get("request_options", {}).get("parameters", {}),
            )


async def test_agent_source_crud_and_remove_override_reveal_lower_definition(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    # deep_agent 是普通稳定 ID，不能被历史别名改绑到 default。
    payload["settings"]["workspace"]["agents"]["deep_agent"] = {
        "name": "来源 Agent", "instructions": {"system_prompt": "用户来源提示词"},
        "model": {"primary": PRIMARY, "fallbacks": []},
    }
    await case.save(payload, "agent-create")
    assert case.service.validate_agent_id("deep_agent") == "deep_agent"
    original = case.service.get_snapshot()
    runtime = case.service.get_agent_runtime_config("deep_agent")
    assert runtime["system_prompt"] == "用户来源提示词"
    assert [(model["provider_id"], model["model_id"]) for model in runtime["models"]] == [("edit-primary", "main")]

    workspace_url = "/api/v1/config/sources/workspace_root_mutable_override"
    override = {"config_version": 4, "extends": "user:workspace_local", "settings": {"workspace": {"agents": {"deep_agent": {
        "name": "本工作区显示名", "instructions": {"system_prompt": "本工作区提示词"},
    }}}}}
    saved = await case.save(override, "agent-override", url=workspace_url)
    assert saved["effective"]["agents"]["deep_agent"]["name"] == "本工作区显示名"
    assert saved["effective"]["agents"]["deep_agent"]["model"]["primary"] == PRIMARY
    assert parse_source_text(case.source.read_text())["settings"]["workspace"]["agents"]["deep_agent"]["name"] == "来源 Agent"

    # 删除仅作用于选定层，不写 tombstone，也不删除下层定义。
    override["settings"]["workspace"]["agents"].pop("deep_agent")
    restored = await case.save(override, "agent-restore-inheritance", url=workspace_url)
    assert "deep_agent" not in parse_source_text(restored["jsonc"])["settings"]["workspace"]["agents"]
    assert restored["effective"]["agents"]["deep_agent"]["instructions"]["system_prompt"] == "用户来源提示词"

    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["settings"]["workspace"]["agents"]["deep_agent"]["name"] = "可编辑显示名"
    edited = await case.save(payload, "agent-edit-name")
    assert edited["effective"]["agents"]["deep_agent"]["name"] == "可编辑显示名"
    payload = parse_source_text(edited["jsonc"])
    payload["settings"]["workspace"]["agents"].pop("deep_agent")
    removed = await case.save(payload, "agent-remove-definition")
    assert "deep_agent" not in removed["effective"]["agents"]
    for resolve in (case.service.validate_agent_id, case.service.get_agent_runtime_config,
                    case.service.resolve_new_session_agent_id):
        with pytest.raises(ValueError, match="agent deep_agent 不存在"):
            resolve("deep_agent")
    # 已持有的执行快照仍由原 owner 提供；删除不改写旧快照。
    with case.service.use_snapshot(original):
        assert case.service.get_agent_runtime_config("deep_agent")["system_prompt"] == "用户来源提示词"


@pytest.mark.parametrize("reference", ["config-default", "workspace-default", "workspace-model"])
async def test_agent_remove_rejects_current_defaults_without_writing_source(
    provider_case: ProviderCase, reference: str,
) -> None:
    case = provider_case
    payload = parse_source_text((await case.document())["jsonc"])
    payload["settings"]["workspace"]["agents"]["custom_agent"] = {
        "name": "默认值引用测试", "instructions": {"system_prompt": "独立定义"},
        "model": {"primary": PRIMARY, "fallbacks": []},
    }
    if reference == "config-default":
        payload["settings"]["workspace"]["default_agent"] = "custom_agent"
    await case.save(payload, "agent-with-default")
    if reference == "workspace-default":
        case.service.set_workspace_default_agent("custom_agent")
    elif reference == "workspace-model":
        case.service.set_workspace_default_model("custom_agent", ModelReferenceDTO.model_validate(PRIMARY))
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["settings"]["workspace"]["agents"].pop("custom_agent")
    request = _request(document, payload, "agent-remove-rejected")
    original = case.source.read_bytes()
    active = case.service.get_snapshot()
    for method, suffix in (("POST", "/preflight"), ("PUT", "")):
        response = await case.client.request(method, URL + suffix, json=request)
        assert response.status_code == 422, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
    assert case.source.read_bytes() == original and case.service.get_snapshot() is active
    assert (await case.document(key="agent-remove-rejected"))["edit"] is None


async def test_provider_reorder_create_remove_and_cas_share_original_source_writer(provider_case: ProviderCase) -> None:
    case = provider_case
    document = await case.document()
    nodes = document["nodes"]
    assert len({node["node_id"] for node in nodes}) == len(nodes)
    assert all(node["parent_id"] is None or any(parent["node_id"] == node["parent_id"] for parent in nodes) for node in nodes)
    profiles = [node for node in nodes if node["source_key"] == SOURCE and node["definition_path"].startswith("/profiles/")]
    assert {node["definition_path"] for node in profiles} == {"/profiles/primary", "/profiles/fallback", "/profiles/unused"}
    payload = parse_source_text(document["jsonc"])
    # 未展开的缺失环境凭据不应阻止定义管理。
    providers = payload["settings"]["workspace"]["llm"]["providers"]
    providers.pop("edit-unused")
    payload["settings"]["workspace"]["llm"]["providers"] = {name: providers[name] for name in ("edit-fallback", "edit-primary")}
    original = case.source.read_bytes()
    request = _request(document, payload, "provider-reorder")
    preview = await case.client.post(URL + "/preflight", json=request)
    assert preview.status_code == 200, preview.text
    assert case.source.read_bytes() == original
    saved = await case.client.put(URL, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.headers["X-Request-ID"] == saved.json()["request_id"] == "provider-edit"
    assert saved.json()["data"]["edit"]["state"] == "active"
    assert "secret:sec_" + "1" * 32 in saved.text and "${PROVIDER_SOURCE_UNSET_KEY}" in saved.text
    raw = case.source.read_text()
    assert raw.startswith("// 保留来源注释\n")
    assert list(parse_source_text(raw)["settings"]["workspace"]["llm"]["providers"]) == ["edit-fallback", "edit-primary"]
    assert parse_source_text(raw)["profiles"]["primary"]["settings"]["model_options"]["api_key"] == "secret:sec_" + "1" * 32
    repeated = await case.client.put(URL, json=request)
    assert repeated.status_code == 200 and repeated.json()["data"]["edit"] == saved.json()["data"]["edit"]
    stale = await case.client.put(URL, json={**request, "idempotency_key": "provider-stale"})
    assert stale.status_code == 409 and case.source.read_text() == raw
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["settings"]["workspace"]["llm"]["providers"]["created"] = deepcopy(payload["settings"]["workspace"]["llm"]["providers"]["edit-primary"])
    created = await case.client.put(URL, json=_request(document, payload, "provider-create"))
    assert created.status_code == 200, created.text
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    assert list(payload["settings"]["workspace"]["llm"]["providers"]) == ["edit-fallback", "edit-primary", "created"]
    payload["settings"]["workspace"]["llm"]["providers"].pop("created")
    removed = await case.client.put(URL, json=_request(document, payload, "provider-remove"))
    assert removed.status_code == 200, removed.text
    assert "created" not in parse_source_text(case.source.read_text())["settings"]["workspace"]["llm"]["providers"]


async def test_public_secret_reference_can_follow_endpoint_edit_and_source_copy(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    payload = parse_source_text((await case.document())["jsonc"])
    provider = payload["settings"]["workspace"]["llm"]["providers"]["edit-primary"]
    options = payload["profiles"]["primary"]["settings"]["model_options"]
    reference = options["api_key"]
    options["endpoint"] = "https://changed.example/v1"
    await case.save(payload, "provider-endpoint-change")
    assert case.service.get_llm_model(PRIMARY)["api_key"] == reference

    # 来源管理只保存公开引用；缺失的 vault 内容留给执行准备解析。
    copied_provider = {**deepcopy(provider), "config_ref": "#/profiles/copied"}
    copied_provider["models"]["main"]["config_ref"] = "context:provider/copied"
    copied = {"config_version": 4, "extends": "user:workspace_local", "settings": {"workspace": {
        "llm": {"providers": {"copied": copied_provider}}}},
        "profiles": {"copied": deepcopy(payload["profiles"]["primary"])}}
    workspace_url = "/api/v1/config/sources/workspace_root_mutable_override"
    override = await case.save(copied, "provider-copy-reference", url=workspace_url)
    assert parse_source_text(override["jsonc"])["settings"]["workspace"]["llm"]["providers"]["copied"] == copied_provider
    assert case.service.get_llm_model({"provider_id": "copied", "model_id": "main"})["api_key"] == reference
    assert "copied" not in parse_source_text(case.source.read_text())["settings"]["workspace"]["llm"]["providers"]


async def test_selected_model_preflight_reports_values_and_both_origins_without_saving(
    provider_case: ProviderCase,
) -> None:
    case = provider_case
    payload = parse_source_text((await case.document())["jsonc"])
    provider = payload["settings"]["workspace"]["llm"]["providers"]["edit-primary"]
    payload["profiles"]["primary"]["settings"]["model_options"]["request_options"] = {"parameters": {"temperature": 0.8, "top_p": 0.9, "max_tokens": 128},
                                                "default_headers": {"X-Route": "lower"}}
    provider["models"]["main"]["config_ref"] = "#/profiles/model-main"
    payload["profiles"]["model-main"] = {"extends": "context:provider/edit-primary", "settings": {"model_options": {
        "request_options": {"parameters": {"temperature": 0.25}},
        "api_mode": {"supports_reasoning": {}},
    }}}
    await case.save(payload, "inspection-lower-source")
    workspace_url = "/api/v1/config/sources/workspace_root_mutable_override"
    document = await case.document(url=workspace_url)
    draft = {"config_version": 4, "extends": "user:workspace_local", "settings": {"workspace": {
        "llm": {"providers": {"edit-primary": {"config_ref": "#/profiles/draft-provider",
            "models": {"main": {"config_ref": "#/profiles/draft-model"}},
        }}}}}, "profiles": {
        "draft-provider": {"extends": "user:workspace#/profiles/primary", "settings": {"model_options": {
            "request_options": {"parameters": {"temperature": 0, "max_output_tokens": 64},
                                           "default_headers": {"x-ROUTE": "draft"}}},
        }},
        "draft-model": {"extends": "user:workspace#/profiles/model-main", "settings": {"model_options": {"request_options": {"parameters": {
            "top_p": None, "extra_body": {"switch": False, "nullable": None},
        }}}}},
    }}
    original = case.source.read_bytes()
    active = case.service.get_snapshot()
    response = await case.client.post(workspace_url + "/preflight", params=PRIMARY,
        json=_request(document, draft, "inspect-model"))
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
    result = response.json()["data"]
    inspected = result["model_options"]
    assert inspected["model_ref"] == PRIMARY
    assert inspected["options"]["request_options"]["parameters"] == {
        "temperature": 0.25, "top_p": None, "max_tokens": 64,
        "extra_body": {"switch": False, "nullable": None},
    }
    header_placeholder = "boxteam-redacted:/settings/model_options/request_options/default_headers/x-route"
    assert inspected["options"]["request_options"]["default_headers"] == {"x-route": header_placeholder}
    assert inspected["options"]["api_mode"]["supports_reasoning"] == {}
    origins = {origin["path"]: origin for origin in inspected["origins"]}
    assert origins["/request_options/parameters/temperature"]["source_key"] == SOURCE
    assert origins["/request_options/parameters/temperature"]["definition"] == "node"
    assert origins["/request_options/parameters/temperature"]["definition_path"] == "/profiles/model-main/settings/model_options/request_options/parameters/temperature"
    assert origins["/request_options/parameters/temperature"]["node_id"].endswith("#/profiles/model-main")
    assert origins["/request_options/parameters/top_p"]["source_key"] == "workspace_root_mutable_override"
    assert origins["/request_options/parameters/max_tokens"]["definition_path"].endswith("/parameters/max_output_tokens")
    assert origins["/request_options/parameters/max_tokens"]["source_key"] == "workspace_root_mutable_override"
    assert origins["/request_options/default_headers/x-route"]["definition_path"].endswith("/default_headers/x-ROUTE")
    assert origins["/endpoint"]["source_key"] == SOURCE and origins["/endpoint"]["definition"] == "node"
    assert origins["/api_mode/supports_reasoning"]["definition_path"] == "/profiles/model-main/settings/model_options/api_mode/supports_reasoning"
    assert inspected["request_url"] == "https://primary.example/v1/chat/completions"
    assert inspected["catalog_url"] == "https://primary.example/v1/models"
    assert inspected["options"]["api_key"] == "secret:sec_" + "1" * 32
    running = result["active_model_options"]
    assert running["options"]["request_options"]["parameters"] == {"temperature": 0.25, "top_p": 0.9, "max_tokens": 128}
    assert running["options"]["request_options"]["default_headers"] == {"x-route": header_placeholder}
    assert case.service.get_llm_model(ModelReferenceDTO.model_validate(PRIMARY))["request_options"]["default_headers"] == {"x-route": "lower"}
    assert {origin["source_key"] for origin in running["origins"] if origin["path"].startswith("/request_options/")} == {SOURCE}
    assert case.source.read_bytes() == original and case.service.get_snapshot() is active
    assert (await case.document(url=workspace_url, key="inspect-model"))["edit"] is None


@pytest.mark.parametrize("pending", [False, True])
async def test_active_model_origins_survive_source_changes_and_service_restart(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, pending: bool,
) -> None:
    case = provider_case
    payload = parse_source_text((await case.document())["jsonc"])
    payload["profiles"]["primary"]["settings"]["model_options"]["request_options"] = {"parameters": {"temperature": 0.25}}
    await case.save(payload, "origin-initial-active")
    workspace_url = "/api/v1/config/sources/workspace_root_mutable_override"
    document = await case.document(url=workspace_url)
    original_revision = document["active_revision"]
    temperature = 0.75 if pending else 0.25
    draft = {"config_version": 4, "extends": "user:workspace_local", "settings": {"workspace": {
        "llm": {"providers": {"edit-primary": {"config_ref": "#/profiles/workspace-primary"}}}}},
        "profiles": {"workspace-primary": {"extends": "user:workspace#/profiles/primary", "settings": {
            "model_options": {"request_options": {"parameters": {"temperature": temperature}}},
        }}}}

    async def require_restart(*_args):
        raise ConfigRestartRequiredError("候选等待重启", changed_sections=("llm",))

    with monkeypatch.context() as runtime:
        if pending:
            runtime.setattr(case.service, "_candidate_applier", require_restart)
        saved = await case.save(draft, "origin-workspace-source", url=workspace_url)
    assert saved["edit"]["state"] == ("pending_restart" if pending else "active")
    assert saved["active_revision"] == original_revision
    active_source = SOURCE if pending else "workspace_root_mutable_override"
    path = "/llm/providers/edit-primary/config_ref"
    assert next(origin["source_key"] for origin in saved["active_origins"] if origin["path"] == path) == active_source
    assert case.service.get_llm_model(PRIMARY)["request_options"]["parameters"]["temperature"] == 0.25
    assert next(origin["source_key"] for origin in saved["origins"] if origin["path"] == path) == "workspace_root_mutable_override"
    old_origins = case.service.get_snapshot().source_origins()
    await case.service.close()
    restored = ConfigService(config_dir=Path.cwd() / "configs", config_path=case.source,
        workspace_root=case.workspace, workspace_state_store=case.service._workspace_state_store)
    try:
        restored.validate_workspace_config()
        assert restored.get_snapshot().source_origins() == old_origins
        with monkeypatch.context() as restarted:
            restarted.setitem(case.app.dependency_overrides, get_config_service, lambda: restored)
            document = await case.document(url=workspace_url)
            response = await case.client.post(workspace_url + "/preflight", params=PRIMARY,
                json=_request(document, parse_source_text(document["jsonc"]), "origin-after-restart"))
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
        result = response.json()["data"]
        assert result["model_options"]["options"]["request_options"]["parameters"]["temperature"] == temperature
        running = result["active_model_options"]
        assert running["options"]["request_options"]["parameters"]["temperature"] == 0.25
        assert next(origin["source_key"] for origin in running["origins"]
                    if origin["path"] == "/request_options/parameters/temperature") == active_source
        assert result["document"]["active_revision"] == original_revision
    finally:
        await restored.close()


@pytest.mark.parametrize("change", ["primary", "fallback", "provider-id-field", "model-id-field", "literal-secret", "invalid-item", "endpoint", "identity", "missing-model", "old-array", "config-default", "workspace-default"])
async def test_source_preflight_and_save_reject_invalid_current_references_and_secret_binding(
    provider_case: ProviderCase, change: str,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    providers = payload["settings"]["workspace"]["llm"]["providers"]
    if change in {"primary", "fallback"}:
        providers.pop(f"edit-{change}")
    elif change == "invalid-item":
        providers["invalid"] = None
    elif change == "provider-id-field":
        providers["edit-primary"]["id"] = "edit-primary"
    elif change == "model-id-field":
        providers["edit-primary"]["models"]["main"]["id"] = "main"
    elif change == "literal-secret":
        payload["profiles"]["primary"]["settings"]["model_options"]["api_key"] = "explicit-test-key"
    elif change == "endpoint":
        payload["profiles"]["primary"]["settings"]["model_options"]["endpoint"] = 7
    elif change == "identity":
        providers["changed"] = providers.pop("edit-primary")
    elif change == "missing-model":
        providers["edit-primary"]["models"].pop("main")
    elif change == "old-array":
        payload["settings"]["workspace"]["llm"]["providers"] = list(providers.values())
    elif change == "config-default":
        payload["settings"]["workspace"]["default_agent"] = "missing"
    else:
        case.service.set_workspace_default_model("default", ModelReferenceDTO.model_validate(PRIMARY))
        for agent in payload["settings"]["workspace"]["agents"].values():
            agent["model"]["primary"] = FALLBACK
        providers.pop("edit-primary")
    url = f'/api/v1/config/sources/{document["source_key"]}'
    original = case.source.read_bytes()
    snapshot = case.service.get_snapshot()
    request = _request(document, payload, "provider-rejected")
    for method, suffix in (("POST", "/preflight"), ("PUT", "")):
        response = await case.client.request(method, url + suffix, json=request)
        assert response.status_code == 422, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
        assert "explicit-test-key" not in response.text
        if change == "workspace-default":
            assert "field=model_by_agent" in response.text
    assert case.source.read_bytes() == original and case.service.get_snapshot() is snapshot
    assert (await case.document(key="provider-rejected"))["edit"] is None


async def test_source_retries_same_accepted_edit_after_replace_failure(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["settings"]["workspace"]["llm"]["providers"] = dict(reversed(payload["settings"]["workspace"]["llm"]["providers"].items()))
    payload["settings"]["workspace"]["llm"]["providers"]["edit-primary"]["name"] = "已编辑的服务名称"
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
    assert list(parse_source_text(case.source.read_text())["settings"]["workspace"]["llm"]["providers"]) == ["edit-fallback", "edit-primary", "edit-unused"]
    assert recovered.json()["data"]["effective"]["llm"]["providers"]["edit-primary"]["name"] == "已编辑的服务名称"


@pytest.mark.parametrize("resume", ["retry", "watcher"])
async def test_saved_candidate_rechecks_defaults_changed_after_source_replace(
    provider_case: ProviderCase, monkeypatch: pytest.MonkeyPatch, resume: str,
) -> None:
    case = provider_case
    document = await case.document()
    payload = parse_source_text(document["jsonc"])
    payload["settings"]["workspace"]["llm"]["providers"].pop("edit-fallback")
    payload["settings"]["workspace"]["agents"]["default"]["model"]["fallbacks"] = []
    request = _request(document, payload, "provider-defaults-recover")

    async def fail_apply(*_args):
        raise RuntimeError("来源已导入，应用候选前故障")

    with monkeypatch.context() as fault:
        fault.setattr(case.service, "_candidate_applier", fail_apply)
        failed = await case.client.put(URL, json=request)
    assert failed.status_code == 500, failed.text
    case.service.set_workspace_default_model("default", ModelReferenceDTO.model_validate(FALLBACK))
    snapshot = case.service.get_snapshot()
    if resume == "watcher":
        # 已拒绝来源等待显式操作；文件监听不能重放失败应用。
        pending = await case.document(key=request["idempotency_key"])
        assert pending["edit"]["state"] == "rejected"
        assert await case.service.reload() is False
        assert (await case.document(key=request["idempotency_key"]))["edit"] == pending["edit"]
    with pytest.raises(ValueError, match="field=model_by_agent"):
        await case.service.retry_source_edit(source_key=SOURCE, gateway_id=GATEWAY, idempotency_key=request["idempotency_key"])
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
    payload["settings"]["workspace"]["llm"]["providers"].pop("edit-fallback")
    payload["settings"]["workspace"]["agents"]["default"]["model"]["fallbacks"] = []
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
    case.service.set_workspace_default_model("default", ModelReferenceDTO.model_validate(FALLBACK))
    snapshot = case.service.get_snapshot()
    if resume == "startup":
        mount = MountedWorkspace(document["workspace_id"], case.workspace.resolve(),
            config_candidate_ref=pending.candidate_ref, config_generation=pending.target_generation,
            config_fencing_token=pending.fencing_token)
        restarted = ConfigService(config_dir=Path.cwd() / "configs", config_path=case.source,
            workspace_root=case.workspace, workspace_state_store=store, startup_mount=mount)
        try:
            with pytest.raises(ValueError, match="field=model_by_agent"):
                restarted.validate_workspace_config()
        finally:
            await restarted.close()
    else:
        response = await case.client.post(f"/api/v1/config/pending/{resume}", params={"candidate_ref": pending.candidate_ref},
                                         json=proof if resume == "resolve" else None)
        assert response.status_code == 409 and "field=model_by_agent" in response.text, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "provider-edit"
    assert case.service.get_snapshot() is snapshot
    assert store.get_pending_config_candidate(config_domain="workspace", idempotency_key=request["idempotency_key"]) == pending
