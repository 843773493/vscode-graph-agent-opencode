"""真实配置 owner、SQLite 与 ASGI 状态的故障注入；不代替运行时启动验收。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Request, Response
from pydantic_core import PydanticSerializationError

from app.api import config as config_api
from app.api.deps import get_config_service, verify_local_token
from app.core import config_sources
from app.core.mounted_workspaces import MountedWorkspace
from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.schemas.internal_v2.config import ConfigSourcesDTO
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.config_service import (
    config_source_edit as source_edit_module,
)
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


_SOURCE_KEY = "workspace_root_mutable_override"
_SOURCE_URL = f"/api/v1/config/sources/{_SOURCE_KEY}"
_SOURCE_GATEWAY = "gateway_" + "7" * 32


@pytest.fixture
async def source_edit_case(config_case: ConfigCase) -> AsyncIterator[tuple[ConfigCase, AsyncClient]]:
    case = config_case
    source = case.workspace / ".boxteam/workspace.jsonc"
    llm = json.loads(json.dumps(case.payload["llm"]))
    llm["providers"][0]["api_key"] = "literal-source-secret"
    source.write_text("// 原来源注释\n" + json.dumps({
        "ui": {"auto_summarize": True, "ignored_paths": ["source-only"]}, "llm": llm,
        "mcp": {"servers": {"private": {"enabled": False, "transport": "streamable_http",
            "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer source-secret"}}}},
    }, ensure_ascii=False, indent=2).replace('"literal-source-secret"', '"literal-source-secr\\u0065t"') + "\n", encoding="utf-8")
    await case.service.reload()
    async with AsyncClient(transport=ASGITransport(app=case.app), base_url="http://test",
                           headers={"X-BoxTeam-Gateway-Id": _SOURCE_GATEWAY, "X-Request-ID": "source-edit"}) as client:
        yield case, client


def _source_edit_request(document: dict[str, object], text: str, key: str) -> dict[str, object]:
    return {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"],
            "source_key": document["source_key"], "jsonc": text, "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": key}


def _source_rows(case: ConfigCase) -> tuple[str, ...]:
    with closing(case.store.connection()) as connection:
        return tuple(connection.iterdump())


async def test_source_preflight_is_read_only_and_save_preserves_secrets_comments_and_layer_deletion(source_edit_case) -> None:
    case, client = source_edit_case
    source = case.workspace / ".boxteam/workspace.jsonc"
    original, rows, snapshot = source.read_bytes(), _source_rows(case), case.service.get_snapshot()
    readiness = (dict(case.service._instruction_inputs._paths), dict(case.service._instruction_inputs._failures),
                 case.service._instruction_inputs._candidate_uris)
    response = await client.get(_SOURCE_URL)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "source-edit"
    assert "literal-source-secret" not in response.text and "Bearer source-secret" not in response.text
    assert "原来源注释" in document["jsonc"] and document["writable"] is True
    assert document["json_schema"]["x-boxteam-config-domain"] == "workspace"
    text = document["jsonc"].replace('"auto_summarize": true', '"auto_summarize": false').replace(
        ',\n    "ignored_paths": [\n      "source-only"\n    ]', "")
    request = _source_edit_request(document, text, "source-write-real")
    checked = await client.post(_SOURCE_URL + "/preflight", json=request)
    assert checked.status_code == 200, checked.text
    assert checked.json()["data"]["candidate"]["ui"]["auto_summarize"] is False
    assert source.read_bytes() == original and _source_rows(case) == rows and case.service.get_snapshot() is snapshot
    assert readiness == (case.service._instruction_inputs._paths, case.service._instruction_inputs._failures,
                         case.service._instruction_inputs._candidate_uris)
    saved = await client.put(_SOURCE_URL, json=request)
    assert saved.status_code == 200, saved.text
    result = saved.json()["data"]
    assert result["status"]["state"] == "active" and result["effective"]["ui"]["auto_summarize"] is False
    assert result["edit"]["source_saved"] and result["edit"]["layer_imported"]
    assert "ignored_paths" not in json.loads(source.read_text().split("\n", 1)[1])["ui"]
    assert result["effective"]["ui"]["ignored_paths"] == []
    assert "原来源注释" in source.read_text() and "literal-source-secr\\u0065t" in source.read_text()
    assert "Bearer source-secret" in source.read_text() and "boxteam-redacted:" not in source.read_text()
    assert source.read_text() == original.decode("utf-8").replace(
        '"auto_summarize": true', '"auto_summarize": false').replace(
        ',\n    "ignored_paths": [\n      "source-only"\n    ]', "")
    events, active = case.service.list_config_events(), case.store.get_active_config_snapshot("workspace")
    replay = await client.put(_SOURCE_URL, json=request)
    assert replay.status_code == 200 and replay.json()["data"]["edit"] == result["edit"]
    assert case.service.list_config_events() == events and case.store.get_active_config_snapshot("workspace") == active
    queried = await client.get(_SOURCE_URL, params={"idempotency_key": request["idempotency_key"]})
    assert queried.json()["data"]["edit"] == result["edit"]


@pytest.mark.parametrize("mutation", ["file", "layer", "active", "gateway", "workspace", "source", "invalid", "provider_endpoint", "provider_id"])
async def test_source_edit_rejects_stale_or_misbound_input_without_writing(source_edit_case, mutation) -> None:
    case, client = source_edit_case
    source = case.workspace / ".boxteam/workspace.jsonc"
    document = (await client.get(_SOURCE_URL)).json()["data"]
    request = _source_edit_request(document, document["jsonc"], "source-rejected")
    if mutation == "file":
        source.write_text(source.read_text() + "// 外部并发编辑\n")
    elif mutation == "layer":
        request["base_layer_revision"] = document["layer_revision"] + 1
    elif mutation == "active":
        request["expected_active_revision"] = document["active_revision"] + 1
    elif mutation == "gateway":
        request["gateway_id"] = "gateway_" + "8" * 32
    elif mutation == "workspace":
        request["workspace_id"] = "00000000-0000-4000-8000-000000000001"
    elif mutation == "source":
        request["source_key"] = "workspace_mutable_override"
    elif mutation == "invalid":
        request["jsonc"] = '{"unknown_source_setting":true}'
    else:
        request["jsonc"] = document["jsonc"].replace(
            '"endpoint": "https://example.com/v1"' if mutation == "provider_endpoint" else '"id": "primary"',
            '"endpoint": "https://other.example/v1"' if mutation == "provider_endpoint" else '"id": "other"')
    original, rows, snapshot = source.read_bytes(), _source_rows(case), case.service.get_snapshot()
    response = await client.put(_SOURCE_URL, json=request)
    assert response.status_code == (422 if mutation in {"invalid", "provider_endpoint", "provider_id"} else 409), response.text
    assert source.read_bytes() == original and _source_rows(case) == rows and case.service.get_snapshot() is snapshot
    assert "literal-source-secret" not in response.text and "Bearer source-secret" not in response.text


@pytest.mark.parametrize("masked", [False, True])
async def test_source_noop_keeps_active_and_events_and_uses_original_candidate_for_idempotency(source_edit_case, masked) -> None:
    case, client = source_edit_case
    if masked:
        from app.schemas.internal_v2.config import ConfigUpdateRequest

        await case.service.update(ConfigUpdateRequest(
            config_layer="runtime_override", scope="workspace", base_layer_revision=None, base_layer_digest=None,
            expected_active_revision=case.store.get_active_config_snapshot("workspace").active_revision,
            expected_active_digest=case.service.get_snapshot().revision, idempotency_key="mask-source", auto_summarize=False,
        ))
    document = (await client.get(_SOURCE_URL)).json()["data"]
    text = document["jsonc"].replace("原来源注释", "只改来源注释")
    if masked:
        text = text.replace('"auto_summarize": true', '"auto_summarize": false')
    active, events = case.store.get_active_config_snapshot("workspace"), case.service.list_config_events()
    request = _source_edit_request(document, text, f"source-unchanged-{masked}")
    response = await client.put(_SOURCE_URL, json=request)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["edit"]["state"] == "active"
    assert case.store.get_active_config_snapshot("workspace") == active and case.service.list_config_events() == events
    assert (await client.put(_SOURCE_URL, json=request)).status_code == 200
    assert case.store.get_active_config_snapshot("workspace") == active and case.service.list_config_events() == events


async def test_source_saved_pending_is_observable_and_not_replayed(source_edit_case) -> None:
    case, client = source_edit_case
    attempts = []

    async def require_restart(previous, candidate, scope, record):
        attempts.append(candidate.revision)
        raise ConfigRestartRequiredError("测试字段要求重启", changed_sections=("ui",))

    case.service._candidate_applier = require_restart
    document = (await client.get(_SOURCE_URL)).json()["data"]
    request = _source_edit_request(document, document["jsonc"].replace('"auto_summarize": true', '"auto_summarize": false'), "source-pending")
    active = case.store.get_active_config_snapshot("workspace")
    response = await client.put(_SOURCE_URL, json=request)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["edit"]["source_saved"] and data["edit"]["state"] == "pending_restart"
    assert data["status"]["restart_required"] and data["effective"]["ui"]["auto_summarize"] is True
    assert data["source_effective"]["ui"]["auto_summarize"] is False
    pending_before = case.store.get_pending_config_candidate(config_domain="workspace", idempotency_key="source-pending")
    events = case.service.list_config_events()
    with closing(case.store.connection()) as connection:
        candidate_count = connection.execute("SELECT count(*) FROM config_pending_candidate").fetchone()[0]
    await case.service.start_watching(candidate_applier=require_restart)
    assert case.store.get_pending_config_candidate(config_domain="workspace", idempotency_key="source-pending") == pending_before
    assert case.service.list_config_events() == events
    with closing(case.store.connection()) as connection:
        assert connection.execute("SELECT count(*) FROM config_pending_candidate").fetchone()[0] == candidate_count
    assert (await client.put(_SOURCE_URL, json=request)).status_code == 200
    assert len(attempts) == 1 and case.store.get_active_config_snapshot("workspace") == active
    assert pending_before is not None and pending_before.candidate_ref is not None
    await case.service.stop_watching()
    mount = MountedWorkspace(document["workspace_id"], case.workspace.resolve(),
        config_candidate_ref=pending_before.candidate_ref, config_generation=pending_before.target_generation,
        config_fencing_token=pending_before.fencing_token)
    restarted = ConfigService(config_dir=Path.cwd() / "configs", config_path=case.config_path,
        workspace_root=case.workspace, workspace_state_store=case.store, startup_mount=mount)
    try:
        restarted.validate_workspace_config()
        assert restarted.get_snapshot().revision == pending_before.candidate_digest
        await restarted.start_watching(candidate_applier=require_restart)
        assert restarted._snapshot_store.status().healthy
        proof = restarted.get_loaded_config_proof()
        assert proof["loaded_source"] == "pending" and proof["candidate_id"] == pending_before.candidate_id
        assert len(attempts) == 1 and case.store.get_active_config_snapshot("workspace") == active
        assert case.store.get_pending_config_candidate(config_domain="workspace", idempotency_key="source-pending") == pending_before
        assert case.service.list_config_events() == events
    finally:
        await restarted.close()


async def test_source_preflight_reads_real_instruction_file_without_changing_readiness(source_edit_case, monkeypatch) -> None:
    case, client = source_edit_case
    document = (await client.get(_SOURCE_URL)).json()["data"]
    original_prompt, preview_prompt = case.workspace / "active-prompt.txt", case.workspace / "preview-prompt.txt"
    original_prompt.write_text("当前已生效的提示词正文", encoding="utf-8")
    preview_prompt.write_text("只供预检读取的提示词正文", encoding="utf-8")
    root_uri = resource_display_uri(scope="workspace", scope_id=document["workspace_id"], kind="files", tail_segments=("root",))
    original_uri, preview_uri = root_uri + "/active-prompt.txt", root_uri + "/preview-prompt.txt"
    paths = {root_uri: case.workspace, original_uri: original_prompt, preview_uri: preview_prompt}

    def resolve_file(uri: str) -> tuple[Path, str]:
        return paths[uri], uri

    inputs = case.service._instruction_inputs
    monkeypatch.setattr(inputs, "_resolve_file", resolve_file)
    payload = json.loads(document["jsonc"].split("\n", 1)[1])
    payload["agents"] = {"default": {"instructions": {"system_prompt_file": original_uri}}}
    response = await client.put(_SOURCE_URL, json=_source_edit_request(document, json.dumps(payload), "source-instruction-active"))
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["effective"]["agents"]["default"]["instructions"]["system_prompt"] == original_prompt.read_text()
    readiness = (dict(inputs._paths), dict(inputs._failures), inputs._candidate_uris)
    source = case.workspace / ".boxteam/workspace.jsonc"
    original, rows, snapshot = source.read_bytes(), _source_rows(case), case.service.get_snapshot()
    request = _source_edit_request(document, document["jsonc"].replace(original_uri, preview_uri), "source-instruction-preview")
    preview = await client.post(_SOURCE_URL + "/preflight", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["candidate"]["agents"]["default"]["instructions"]["system_prompt"] == preview_prompt.read_text()
    preview_prompt.unlink()
    failed = await client.post(_SOURCE_URL + "/preflight", json=request)
    assert failed.status_code == 500 and failed.json()["request_id"] == failed.headers["X-Request-ID"] == "source-edit"
    assert "StableSourceReadError" in failed.text and str(case.workspace) not in failed.text
    assert (inputs._paths, inputs._failures, inputs._candidate_uris) == readiness
    case.service.require_instruction_sources_available("default")
    assert source.read_bytes() == original and _source_rows(case) == rows and case.service.get_snapshot() is snapshot


async def test_source_list_keys_open_original_documents_without_changing_diagnostics(source_edit_case) -> None:
    case, client = source_edit_case
    rows, snapshot = _source_rows(case), case.service.get_snapshot()
    diagnostics = case.service.get_source_diagnostics()[2]
    assert diagnostics[0].source_key is None
    listed = await client.get("/api/v1/config/sources")
    assert listed.status_code == 200, listed.text
    assert listed.json()["request_id"] == listed.headers["X-Request-ID"] == "source-edit"
    sources = listed.json()["data"]["sources"]
    assert [source["source_key"] for source in sources] == [
        "inline", "workspace_mutable_override", "workspace_local_mutable_override", _SOURCE_KEY, "workspace_runtime_override",
    ]
    for source in sources:
        response = await client.get(f'/api/v1/config/sources/{source["source_key"]}')
        assert response.status_code == 200, response.text
        document = response.json()["data"]
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "source-edit"
        assert document["source_key"] == source["source_key"] and document["layer"] == source["layer"]
        assert document["writable"] is (source["source_key"] == _SOURCE_KEY)
        assert document["gateway_id"] == _SOURCE_GATEWAY
    assert _source_rows(case) == rows and case.service.get_snapshot() is snapshot
    assert case.service.get_source_diagnostics()[2] == diagnostics


@pytest.mark.parametrize("source_key", ["inline", "workspace_mutable_override", "workspace_local_mutable_override", "workspace_runtime_override"])
async def test_source_layers_without_writer_are_readable_but_reject_save(source_edit_case, source_key) -> None:
    case, client = source_edit_case
    url = f"/api/v1/config/sources/{source_key}"
    response = await client.get(url)
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    assert document["writable"] is False
    rows, snapshot = _source_rows(case), case.service.get_snapshot()
    response = await client.put(url, json=_source_edit_request(document, document["jsonc"], "no-writer"))
    assert response.status_code == 422, response.text
    assert _source_rows(case) == rows and case.service.get_snapshot() is snapshot


async def test_source_normal_text_with_redaction_prefix_is_saved_literally(source_edit_case) -> None:
    _case, client = source_edit_case
    document = (await client.get(_SOURCE_URL)).json()["data"]
    text = document["jsonc"].replace('"source-only"', '"boxteam-redacted:ordinary-path"')
    response = await client.put(_SOURCE_URL, json=_source_edit_request(document, text, "source-normal-prefix"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["effective"]["ui"]["ignored_paths"] == ["boxteam-redacted:ordinary-path"]


@pytest.mark.parametrize("phase", ["before_replace", "after_replace"])
async def test_source_intent_survives_file_boundary_failure_and_reopens_in_same_candidate(source_edit_case, monkeypatch, phase) -> None:
    case, client = source_edit_case
    source = case.workspace / ".boxteam/workspace.jsonc"
    document = (await client.get(_SOURCE_URL)).json()["data"]
    request = _source_edit_request(document, document["jsonc"].replace('"auto_summarize": true', '"auto_summarize": false'), "source-interrupted")
    active, events, original = case.store.get_active_config_snapshot("workspace"), case.service.list_config_events(), source.read_bytes()

    def fail_before_replace(path: Path, content: bytes) -> NoReturn:
        raise OSError(5, "原子替换前故障", str(path))

    def fail_after_replace() -> NoReturn:
        raise RuntimeError("原子替换后、来源导入前故障")

    with monkeypatch.context() as fault:
        if phase == "before_replace":
            fault.setattr(source_edit_module, "atomic_write_bytes", fail_before_replace)
        else:
            fault.setattr(case.service._snapshot_store, "_candidate_builder", fail_after_replace)
        response = await client.put(_SOURCE_URL, json=request)
    assert response.status_code == 500, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "source-edit"
    assert "literal-source-secret" not in response.text and str(case.workspace) not in response.text
    pending = case.store.get_pending_config_candidate(config_domain="workspace", idempotency_key="source-interrupted")
    assert pending is not None and pending.state == "candidate_validated" and pending.source_edit is not None
    assert pending.source_edit["stage"] == "accepted" and "jsonc" not in pending.source_edit
    assert (source.read_bytes() == original) == (phase == "before_replace")
    assert case.store.get_active_config_snapshot("workspace") == active and case.service.list_config_events() == events

    # 同库重建配置 owner 核验持久恢复；该证据不替代操作系统硬重启。
    reopened = ConfigService(config_dir=Path.cwd() / "configs", config_path=case.config_path,
                             workspace_root=case.workspace, workspace_state_store=case.store)
    applications = []

    async def apply_once(previous, candidate, scope, record):
        applications.append(candidate.revision)
        return lambda: None

    try:
        reopened.validate_workspace_config()
        case.app.dependency_overrides[get_config_service] = lambda: reopened
        queried = await client.get(_SOURCE_URL, params={"idempotency_key": "source-interrupted"})
        assert queried.status_code == 200, queried.text
        edit = queried.json()["data"]["edit"]
        assert edit["candidate_id"] == pending.candidate_id and edit["source_saved"] == (phase == "after_replace")
        assert edit["layer_imported"] is False
        await reopened.start_watching(candidate_applier=apply_once)
        assert applications == ([pending.candidate_digest] if phase == "after_replace" else [])
        after_start = (await client.get(_SOURCE_URL, params={"idempotency_key": "source-interrupted"})).json()["data"]
        assert after_start["edit"]["candidate_id"] == pending.candidate_id
        assert after_start["edit"]["state"] == ("active" if phase == "after_replace" else "candidate_validated")
        if phase == "before_replace":
            assert source.read_bytes() == original and case.store.get_active_config_snapshot("workspace") == active
        recovered = await client.put(_SOURCE_URL, json=request)
        assert recovered.status_code == 200, recovered.text
        result = recovered.json()["data"]
        assert result["edit"]["candidate_id"] == pending.candidate_id and result["edit"]["state"] == "active"
        assert result["edit"]["source_saved"] and result["edit"]["layer_imported"]
        assert result["effective"]["ui"]["auto_summarize"] is False
        assert applications == [pending.candidate_digest]
        with closing(case.store.connection()) as connection:
            assert connection.execute("SELECT count(*) FROM config_pending_candidate WHERE idempotency_key=?", ("source-interrupted",)).fetchone()[0] == 1
    finally:
        case.app.dependency_overrides[get_config_service] = lambda: case.service
        await reopened.close()


@pytest.mark.parametrize("mutation", ["file", "layer", "active"])
async def test_source_interrupted_intent_rejects_changed_original_cas(source_edit_case, monkeypatch, mutation) -> None:
    case, client = source_edit_case
    source = case.workspace / ".boxteam/workspace.jsonc"
    document = (await client.get(_SOURCE_URL)).json()["data"]
    baseline_bytes = source.read_bytes()
    request = _source_edit_request(document, document["jsonc"].replace('"auto_summarize": true', '"auto_summarize": false'), "source-interrupted-conflict")

    def fail_before_replace(path: Path, content: bytes) -> NoReturn:
        raise OSError(5, "原子替换前故障", str(path))

    with monkeypatch.context() as fault:
        fault.setattr(source_edit_module, "atomic_write_bytes", fail_before_replace)
        assert (await client.put(_SOURCE_URL, json=request)).status_code == 500
    source.write_text(source.read_text().replace("原来源注释", "外部已变更的来源注释"))
    if mutation in {"layer", "active"}:
        if mutation == "active":
            source.write_text(source.read_text().replace('"auto_summarize": true', '"auto_summarize": false'))
        await case.service.reload()
        if mutation == "layer":
            source.write_bytes(baseline_bytes)
    original, rows, snapshot = source.read_bytes(), _source_rows(case), case.service.get_snapshot()
    response = await client.put(_SOURCE_URL, json=request)
    assert response.status_code == 409, response.text
    assert source.read_bytes() == original and _source_rows(case) == rows and case.service.get_snapshot() is snapshot


async def test_source_same_key_concurrent_requests_share_candidate_and_reject_different_intent(source_edit_case) -> None:
    case, client = source_edit_case
    document = (await client.get(_SOURCE_URL)).json()["data"]
    request = _source_edit_request(document, document["jsonc"].replace('"auto_summarize": true', '"auto_summarize": false'), "source-concurrent")
    responses = await asyncio.gather(client.put(_SOURCE_URL, json=request), client.put(_SOURCE_URL, json=request))
    assert [response.status_code for response in responses] == [200, 200], [response.text for response in responses]
    candidate_ids = {response.json()["data"]["edit"]["candidate_id"] for response in responses}
    assert len(candidate_ids) == 1
    with closing(case.store.connection()) as connection:
        assert connection.execute("SELECT count(*) FROM config_pending_candidate WHERE idempotency_key=?", ("source-concurrent",)).fetchone()[0] == 1
    before = _source_rows(case)
    conflict = await client.put(_SOURCE_URL, json={**request, "jsonc": str(request["jsonc"]) + "\n// 同键另一意图\n"})
    assert conflict.status_code == 409, conflict.text
    assert _source_rows(case) == before


@pytest.mark.parametrize("field", ["schema_uri", "source_uri"])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.asyncio
async def test_copied_source_projection_rejects_native_uri_without_changing_owner(
    config_case: ConfigCase, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, field: str, sink_fails: bool,
) -> None:
    case = config_case
    config_bytes = case.config_path.read_bytes()
    snapshot = case.service.get_snapshot()

    def stored_rows() -> tuple[str, ...]:
        with closing(case.store.connection()) as connection:
            return tuple(connection.iterdump())

    rows = stored_rows()
    originals: list[BaseException] = []

    def fail_log(*_args: object, exc_info: BaseException, **_kwargs: object) -> NoReturn:
        originals.append(exc_info)
        raise OSError(5, "诊断写入失败", "/private-config/log-sink")

    async with AsyncClient(transport=ASGITransport(app=case.app), base_url="http://test") as client:
        baseline = await client.get("/api/v1/config/sources", headers={"X-Request-ID": "source-baseline"})
        assert baseline.status_code == 200, baseline.text
        original_data = baseline.json()["data"]
        assert original_data["sources"] and original_data["revision"] == snapshot.revision
        assert all(source["uri"] is None or source["uri"].startswith("boxteam://") for source in original_data["sources"])
        assert baseline.headers["X-Request-ID"] == baseline.json()["request_id"] == "source-baseline"

        for native in (
            "/private-config/source.jsonc", r"C:\private-config\source.jsonc",
            r"\\server\private-config\source.jsonc", "file:///private-config/source.jsonc",
        ):
            def corrupt_projection(*, _native: str = native, **payload: object) -> ConfigSourcesDTO:
                result = ConfigSourcesDTO.model_validate(payload)
                if field == "schema_uri":
                    return result.model_copy(update={"schema_uri": _native})
                result.sources[0] = result.sources[0].model_copy(update={"uri": _native})
                return result

            caplog.clear()
            originals.clear()
            with monkeypatch.context() as fault:
                fault.setattr(config_api, "ConfigSourcesDTO", corrupt_projection)
                if sink_fails:
                    fault.setattr(config_sources.logger, "error", fail_log)
                response = await client.get("/api/v1/config/sources", headers={"X-Request-ID": "source-rejected"})
            assert response.status_code == 500, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == "source-rejected"
            assert "private-config" not in response.text
            failures = [record.exc_info[1] for record in caplog.records if record.exc_info]
            assert any(isinstance(error, PydanticSerializationError) for error in failures)
            if sink_fails:
                assert originals and all(isinstance(error, ValueError) for error in originals)
                assert all(any("诊断日志输出失败" in note and "log-sink" in note
                               for note in getattr(error, "__notes__", ())) for error in originals)
            else:
                assert any(isinstance(error, ValueError) for error in failures)
                assert any(repr(native) in record.getMessage() for record in caplog.records)
            assert case.service.get_snapshot() is snapshot
            assert case.config_path.read_bytes() == config_bytes and stored_rows() == rows
            recovered = await client.get("/api/v1/config/sources", headers={"X-Request-ID": "source-recovered"})
            assert recovered.status_code == 200, recovered.text
            assert recovered.headers["X-Request-ID"] == recovered.json()["request_id"] == "source-recovered"
            assert recovered.json()["data"] == original_data
            assert stored_rows() == rows


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
                WorkspaceTarget(gateway_id=registry.gateway_id, workspace_id=workspace_id, name="Member", root_path=str(case.workspace),
                                backend_url="http://workspace-backend", connection_kind="local"),
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
