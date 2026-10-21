"""本地 Provider 桩 + 真实 Web/Gateway/Saver 的 Integration，不是 E2E。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.records import (
    CanonicalItemRecord,
    TurnScope,
)
from app.services.business.session_context_resource import context_assembly_identity
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    EMPTY_TOOL_SET,
    dispatch_execution_scope,
    inspection_resource_registry,
    invoke_provider_dispatch,
    native_http_server,
    seed_dispatch_history,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    register_projection_draft,
)
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import close_backend_process, start_backend_process

__all__ = ["inspection_resource_registry", "native_http_server"]


def _require_free_port(port: int) -> None:
    # 先验证测试专用端口，绝不借共享 harness 清理未知服务。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@pytest.fixture
def browser_runtime(
    request,
    integration_workspace_root_path,
    integration_workspace_config_path,
    setup_test_config,
):
    del setup_test_config
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    assert workspace == context.workspace_root
    assert Path(integration_workspace_config_path).is_file()
    ports = integration_port_block_for_file(Path(request.node.path))
    _require_free_port(ports.backend_port)
    _require_free_port(ports.port(20))
    runtime_home = context.boxteam_home_for_node(request.node.nodeid)
    credential = runtime_home / "state/gateway/credentials/local-token"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("local-dev-token\n", encoding="utf-8")
    credential.chmod(0o600)
    backend = start_backend_process(
        workspace_root=str(workspace),
        port=ports.backend_port,
        log_name="itemized-web-backend",
        env_overrides={"BOXTEAM_HOME": str(runtime_home)},
    )
    try:
        gateway = start_gateway_process(
            workspace_root=workspace,
            default_backend_url=f"http://127.0.0.1:{backend.port}",
            port=ports.port(20),
            extra_env={
                "BOXTEAM_HOME": str(runtime_home),
                "BOXTEAM_WEB_ASSETS": str(Path.cwd() / "src/clients/web/dist"),
            },
        )
        try:
            (context.artifacts_dir / "process-owner.json").write_text(
                json.dumps(
                    {
                        "classification": "Integration",
                        "workspace": str(workspace),
                        "boxteam_home": str(runtime_home),
                        "backend_pid": backend.process.pid,
                        "backend_port": backend.port,
                        "gateway_pid": gateway.process.pid,
                        "gateway_port": gateway.port,
                        "web_assets": str(Path.cwd() / "src/clients/web/dist"),
                        "web_main_sha256": hashlib.sha256(
                            (
                                Path.cwd() / "src/clients/web/dist/assets/main2.js"
                            ).read_bytes()
                        ).hexdigest(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            yield context, f"http://127.0.0.1:{gateway.port}"
        finally:
            close_gateway_process(gateway)
    finally:
        close_backend_process(backend)


def _projection_evidence(saver, session_id, snapshot):
    plan = snapshot.as_sealed_plan()
    projection = saver.project_context_plan_to_history_with_diagnostics(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    history, history_loss = projection.messages, projection.losses
    _, langchain_loss = saver.project_context_plan_with_diagnostics(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    native = saver.project_context_plan_to_native(
        session_id, saver._storage.storage.main_thread_id(session_id), plan
    )
    selection = [entry.to_dict() for entry in snapshot.selection]
    assert native["selection"] == selection
    assert tuple(native["losses"]) == langchain_loss == history_loss
    return {
        "assembly_id": snapshot.assembly_id,
        "resource_refs": [resource.model_dump(mode="json") for resource in project_sealed_resource_refs(projection.resource_activation)] if projection.resource_activation is not None else [],
        "selection": selection,
        "history": [
            {"id": message.id, "text": str(message.text)} for message in history
        ],
        "losses": list(snapshot.loss) + list(history_loss),
        "native_request": native["request"],
    }


@pytest.fixture
async def browser_source(browser_runtime, native_http_server, inspection_resource_registry):
    context, base_url = browser_runtime
    state, endpoint = native_http_server
    async with httpx.AsyncClient(
        base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=30
    ) as client:
        guest = await client.post(
            "/api/gateway/users/guest",
            json={"tracking": {"source": "itemized-context-integration"}},
        )
        assert guest.status_code == 200, guest.text
        workspace_response = await client.get("/api/gateway/workspaces")
        assert workspace_response.status_code == 200, workspace_response.text
        workspace_id = workspace_response.json()["data"]["active_workspace_id"]
        headers = {"X-BoxTeam-Workspace-Id": workspace_id}
        created = await client.post(
            "/api/v1/sessions", headers=headers, json={"title": "冻结上下文浏览器合同"}
        )
        assert created.status_code == 200, created.text
        session = created.json()["data"]
        session_id = session["session_id"]
        other = await client.post(
            "/api/v1/sessions", headers=headers, json={"title": "空会话隔离对照"}
        )
        assert other.status_code == 200, other.text
        other_session = other.json()["data"]
        other_id = other_session["session_id"]
    with RolloutCheckpointSaver(context.workspace_root / ".boxteam/sessions") as saver:
        turn_id = seed_dispatch_history(saver, session_id, registry=inspection_resource_registry)
        seed_dispatch_overlay(saver, session_id)
        await invoke_provider_dispatch(saver, session_id, turn_id, endpoint, state.api_key, target_format="responses")
        with dispatch_execution_scope(saver, session_id, turn_id):
            (first,) = saver.list_context_assemblies(
                session_id, saver._storage.storage.main_thread_id(session_id)
            )
            # 本装具直接调用模型图，没有产品event adapter；按真实已封存call登记控制事实。
            assert first.model_call_id is not None
            saver.register_model_call(
                session_id, thread_id=first.thread_id, execution_id=first.execution_id,
                model_call_id=first.model_call_id, attempt=1, provider="openai",
                assembly_id=first.assembly_id, dispatch_state="dispatched",
            )
            saver.update_model_call_outcome(
                session_id, thread_id=first.thread_id,
                model_call_id=first.model_call_id, outcome="completed", dispatch_state="completed",
            )
            first_evidence = _projection_evidence(saver, session_id, first)
            assert (
                state.requests[0]["body"]["input"]
                == first_evidence["native_request"]["input"]
            )
            source_items = saver.read_canonical_items(session_id)
            final_item = next(
                item
                for item in reversed(source_items)
                if item.semantic_kind == "assistant_output"
            )
            saver.append_items(
                session_id,
                (
                    CanonicalItemRecord.create(
                        item_id="browser-opaque",
                        item_sequence=max(item.item_sequence for item in source_items)
                        + 1,
                        semantic_kind="reasoning",
                        payload_kind="opaque",
                        status="completed",
                        producer_ref={
                            "producer_kind": "provider",
                            "producer_id": "browser-contract",
                        },
                        payload={
                            "encoding": "base64",
                            "wire_type": "encrypted_reasoning",
                            "schema_version": "v1",
                            "value": "c2VjcmV0",
                            "protection": {"encrypted": True},
                        },
                        turn_id=turn_id,
                        turn_scope="turn_member",
                    ),
                ),
                thread_id=saver._storage.storage.main_thread_id(session_id),
            )
            draft = saver.compose_committed_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                plan_id=f"browser-{uuid4().hex}",
                tool_set=ToolSetSnapshot(
                    selection_revision=sha256_jcs({"inspect_file": True}),
                    tools=(
                        {
                            "tool_id": "inspect_file",
                            "name": "inspect_file",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    ),
                ),
            )
            canonical = tuple(
                ref for ref in draft.refs if ref.ref_type == "canonical_item"
            )
            requests = tuple(
                ref for ref in draft.refs if ref.ref_type == "request_only"
            )
            plain_body = [{"type": "text", "text": "BROWSER_PRIVATE_REQUEST_BODY"}]
            plain = ContextRef.request_only_ref(
                "browser-plain",
                session_id=session_id,
                thread_id=draft.thread_id,
                plan_id=draft.plan_id,
                content=plain_body,
                payload_kind="structured_content",
                source_revision="browser-v1",
                source_ref="browser-plain-source",
            )
            draft = replace(draft, refs=(*reversed(canonical), *requests, plain))
            draft = register_projection_draft(saver, session_id, draft)
            execution_id = saver.execution_for_turn(
                session_id,
                turn_id=turn_id,
                thread_id=saver._storage.storage.main_thread_id(session_id),
            )
            rich = saver.seal_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                draft,
                wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{draft.plan_id}",
                turn_id=turn_id,
                execution_id=execution_id,
                provider_version="browser-contract",
                omitted_ref_identities=(
                    selection_ref_identity(canonical[1]),
                    selection_ref_identity(next(ref for ref in requests if ref.ref_id == "a-delta")),
                ),
                request_only_content={plain.ref_id: plain_body},
            )
            # 第三个真实封存快照用于通过 UI 翻页找到最早请求。
            third_plan = saver.compose_committed_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                plan_id=f"browser-{uuid4().hex}",
                tool_set=EMPTY_TOOL_SET,
            )
            third_plan = register_projection_draft(saver, session_id, third_plan)
            saver.seal_context_plan(
                session_id,
                saver._storage.storage.main_thread_id(session_id),
                third_plan,
                wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{third_plan.plan_id}",
                turn_id=turn_id,
                execution_id=execution_id,
                provider_version="browser-contract",
            )
            rich_evidence = _projection_evidence(saver, session_id, rich)
            # da9291b 起 completed terminal convergence 要求 final item 与终态指针
            # 在同一 item-bearing 提交内落地；checkpoint 投影已提交的 canonical item
            # 不能直接作为 final 指针（其 message identity 属于既有提交）。这里以
            # 同一正文、全新 identity 新建 final item 收敛。
            turn_final = CanonicalItemRecord.create(
                item_id="browser-turn-final",
                item_sequence=max(item.item_sequence for item in source_items) + 2,
                semantic_kind=final_item.semantic_kind,
                payload_kind=final_item.payload_kind,
                status=final_item.status,
                producer_ref={
                    "producer_kind": "provider",
                    "producer_id": "browser-contract",
                },
                payload=final_item.payload,
                projection_identity=ItemProjectionIdentity(
                    projection_message_id="browser-turn-final-message"
                ),
                turn_id=turn_id,
                turn_scope=TurnScope.TURN_MEMBER,
                message_group_id=f"message-{turn_id}-final",
                wire_role=final_item.wire_role,
            )
            saver.converge_execution(
                session_id,
                turn_id=turn_id,
                execution_id=execution_id,
                outcome="completed",
                turn_status="completed",
                items=(turn_final,),
                final_item_id=turn_final.item_id,
                thread_id=saver._storage.storage.main_thread_id(session_id),
            )
    first_evidence["assembly_ref"] = context_assembly_identity(
        session["resource_identity"], first_evidence["assembly_id"]
    )
    rich_evidence["assembly_ref"] = context_assembly_identity(
        session["resource_identity"], rich_evidence["assembly_id"]
    )
    fixture = {
        "session_resource": {
            key: session[key] for key in ("resource_identity", "uri", "scope")
        },
        "other_session_resource": {
            key: other_session[key] for key in ("resource_identity", "uri", "scope")
        },
        "workspace_id": workspace_id,
        "session_id": session_id,
        "other_session_id": other_id,
        "first": first_evidence,
        "rich": rich_evidence,
    }
    (context.artifacts_dir / "source-fixture.json").write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return fixture


@pytest.mark.asyncio
async def test_itemized_context_through_real_web_gateway_with_native_http_stub(
    browser_runtime, browser_source
):
    context, base_url = browser_runtime
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if not chromium_path:
        pytest.fail("冻结上下文浏览器 Integration 需要 Chromium")
    env = {
        **os.environ,
        "BOXTEAM_BROWSER_BASE_URL": base_url,
        "BOXTEAM_BROWSER_FIXTURE": json.dumps(browser_source, ensure_ascii=False),
        "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
    }
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "bun",
            "tests/integration/clients/web/itemized_context/itemized_context_browser.mjs",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    (context.artifacts_dir / "browser.stdout.log").write_text(
        result.stdout, encoding="utf-8"
    )
    (context.artifacts_dir / "browser.stderr.log").write_text(
        result.stderr, encoding="utf-8"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(
        (context.artifacts_dir / "browser-result.json").read_text(encoding="utf-8")
    )
    assert evidence["classification"] == "Integration"
    assert evidence["selection_order_equal"] and evidence["loss_equal"]
    assert evidence["reloaded_same_assembly"] and evidence["active_history_preserved"]
    assert evidence["session_isolation"] and evidence["network_routes_verified"]
    assert evidence["page_errors"] == []
