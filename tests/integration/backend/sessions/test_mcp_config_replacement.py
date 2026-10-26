"""真实工作区 watcher、模型封存、HTTP MCP 及兄弟工作区隔离。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import httpx
import pytest

from app import main
from app.core.path_utils import workspace_context
from app.schemas.internal_v2.common import JobStatus
from app.testing.model_stream import build_cassette, chat_completion_frames
from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    assert_closed,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 沿用当前正式测试路径的真实双挂载装具
)
from tests.integration.backend.sessions.test_runner_cancellation import (
    RunnerCase,
    _session,
    _start_job,
)
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 复用真实 HTTP/MCP 协议 Server
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


async def wait_status(service, predicate):
    async def observe():
        while True:
            status = service.get_reload_status()
            if predicate(status):
                return status
            await asyncio.sleep(0.05)
    return await asyncio.wait_for(observe(), 15)


def plan_tool_loop(replay, container, session_id, thread_id, marker):
    provider_id = container.config_service.get_workspace_default_provider_id("default")
    provider = container.config_service.get_llm_provider(provider_id)
    interactions = []
    for index, delta in enumerate((
        {"tool_calls": [{"index": 0, "id": f"call-{marker}", "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "mcp__schema__probe", "arguments": {"marker": marker}}),
        }}]},
        {"content": f"完成 {marker}"},
    )):
        cassette = build_cassette(
            asset_id=f"replacement-{marker}-{index}", model=provider["model"],
            url=provider["endpoint"].rstrip("/") + "/chat/completions",
            frames=chat_completion_frames(response_id=f"replacement-{marker}-{index}", model=provider["model"],
                deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if index == 0 else "stop"),
        )
        rows = cassette.raw["interactions"]
        assert isinstance(rows, list)
        interactions.append(rows[0])
    return replay.control.plan_model_calls(session_id, thread_id, interactions)


def mcp_servers(payload):
    mcp = payload["mcp"]
    assert isinstance(mcp, dict)
    servers = mcp["servers"]
    assert isinstance(servers, dict)
    return servers


async def test_watcher_replacement_preserves_sealed_network_target_and_publishes_new_calls(
    mounted_case: MountedCase, inprocess_model_replay, http_schema_endpoint_factory, monkeypatch,
):
    replay, controller = inprocess_model_replay
    ready, release = asyncio.Event(), asyncio.Event()
    blocked_call = None

    async def observe_phase(call_id, phase):
        assert phase == "model.response_ready"
        if call_id == blocked_call:
            ready.set()
            await release.wait()

    monkeypatch.setattr("app.testing.model_stream.transport.observe_phase", observe_phase)
    async with http_schema_endpoint_factory("old") as old, http_schema_endpoint_factory("new") as new:
        for root in mounted_case.roots.values():
            path = root / ".boxteam/workspace.jsonc"
            payload = read_jsonc_object(path) if path.exists() else {}
            payload["mcp"] = {"servers": {"schema": {"enabled": True, "transport": "streamable_http", "url": old.url}}}
            path.write_text(json.dumps(payload), encoding="utf-8")
        async with main.lifespan(main.app):
            first, sibling = mounted_case.containers
            service, owner = first.config_service, first.mcp_catalog_owner
            previous, sibling_binding = owner.binding_snapshot(), sibling.mcp_catalog_owner.binding_snapshot()
            path = first.workspace_root / ".boxteam/workspace.jsonc"
            with workspace_context(first.workspace_id, first.workspace_root, session_catalog=first.session_catalog):
                runner = RunnerCase(first, first.workspace_root)
                session_id, thread_id = await _session(runner)
                calls = plan_tool_loop(replay, first, session_id, thread_id, "sealed-old")
                blocked_call = calls[0]
                job = await _start_job(runner, "user", session_id, thread_id)
                try:
                    await asyncio.wait_for(ready.wait(), 30)
                    payload = read_jsonc_object(path)
                    mcp_servers(payload)["schema"]["url"] = new.url
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    status = await wait_status(service, lambda status: status.healthy and owner.binding_snapshot() != previous)
                    current = owner.binding_snapshot()
                    assert current.catalog_revision == previous.catalog_revision and current.generation == previous.generation
                    assert current.resolve("mcp__schema__probe").connection_ref != previous.resolve("mcp__schema__probe").connection_ref
                    assert status.state == "active" and status.applied_paths == ("/mcp/servers/schema/url",)
                    assert not status.deferred_paths and not status.restart_required
                    assert sibling.mcp_catalog_owner.binding_snapshot() == sibling_binding
                    assert sibling.config_service.get_snapshot().to_dict()["mcp"]["servers"]["schema"]["url"] == old.url
                finally:
                    release.set()
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                assert job.status == JobStatus.completed, job.error_message
                assert [json.loads(line) for line in old.receipts.read_text().splitlines()] == [{"name": "probe", "arguments": {"marker": "sealed-old"}}]
                assert not new.receipts.exists()
                plan_tool_loop(replay, first, session_id, thread_id, "current-new")
                job = await _start_job(runner, "user", session_id, thread_id)
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                assert job.status == JobStatus.completed, job.error_message
                assert [json.loads(line) for line in new.receipts.read_text().splitlines()] == [{"name": "probe", "arguments": {"marker": "current-new"}}]
                assert controller.transport.call_count == 4
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test",
                headers=LOCAL_TOKEN_HEADERS,
            ) as client:
                response = await client.get(f"/api/v1/workspaces/{first.workspace_id}/config/reload-status")
                assert response.status_code == 200, response.text
                assert response.json()["data"]["state"] == "active"
            # 同候选第一个连接可建、第二个真实 HTTP initialize 失败，不能半切换。
            mcp_servers(payload)["schema"]["url"] = old.url
            mcp_servers(payload)["zz_failure"] = {"enabled": True, "transport": "streamable_http", "url": old.url.replace("/mcp/", "/missing/")}
            path.write_text(json.dumps(payload), encoding="utf-8")
            failed = await wait_status(service, lambda status: not status.healthy)
            assert failed.reason == "rejected" and failed.state == "rejected"
            assert failed.candidate_id is not None
            event = first.workspace_activity_service.store.latest_config_attempt_event(
                config_domain="workspace", candidate_id=failed.candidate_id, attempt_id=failed.attempt_id,
            )
            assert event is not None and event.result == "apply_failed"
            assert owner.binding_snapshot() == current
            assert service.get_snapshot().to_dict()["mcp"]["servers"]["schema"]["url"] == new.url
            assert sibling.mcp_catalog_owner.binding_snapshot() == sibling_binding
            # 仍需重启的监督参数与 HTTP 变更整体延后，不能提前应用连接部分。
            mcp_servers(payload).pop("zz_failure")
            mcp_config = payload["mcp"]
            assert isinstance(mcp_config, dict)
            mcp_config["supervision"] = {"sealed_call_timeout_seconds": 300}
            path.write_text(json.dumps(payload), encoding="utf-8")
            pending = await wait_status(service, lambda status: status.restart_required)
            assert pending.state == "pending_restart" and pending.applied_paths == ()
            assert pending.deferred_paths == ("/mcp/servers/schema/url", "/mcp/supervision/sealed_call_timeout_seconds")
            assert pending.candidate_id is not None
            event = first.workspace_activity_service.store.latest_config_attempt_event(
                config_domain="workspace", candidate_id=pending.candidate_id, attempt_id=pending.attempt_id,
            )
            assert event is not None and event.result == "restart_required" and event.activation_scope == "restart_workspace"
            assert owner.binding_snapshot() == current
    assert_closed(mounted_case)


async def test_watcher_add_header_reference_replace_disable_remove_and_reenable_use_real_connections(
    mounted_case: MountedCase, inprocess_model_replay, http_schema_endpoint_factory, monkeypatch,
):
    replay, controller = inprocess_model_replay
    monkeypatch.setenv("MCP_TEST_OLD_CREDENTIAL", "test-old-credential")
    monkeypatch.setenv("MCP_TEST_NEW_CREDENTIAL", "test-new-credential")
    async with (
        http_schema_endpoint_factory("headers-old", required_headers={"Authorization": "Bearer test-old-credential"}) as old,
        http_schema_endpoint_factory("headers-new", required_headers={"Authorization": "Bearer test-new-credential"}) as new,
    ):
        root, sibling_root = mounted_case.roots.values()
        for workspace in (root, sibling_root):
            (workspace / ".boxteam/workspace.jsonc").write_text(json.dumps({"mcp": {"servers": {}}}))
        path = root / ".boxteam/workspace.jsonc"
        payload = {"mcp": {"servers": {"schema": {
            "enabled": True, "transport": "streamable_http", "url": old.url,
            "headers": {"Authorization": "Bearer ${MCP_TEST_OLD_CREDENTIAL}"},
        }}}}
        async with main.lifespan(main.app):
            first, sibling = mounted_case.containers
            service, owner = first.config_service, first.mcp_catalog_owner
            sibling_binding = sibling.mcp_catalog_owner.binding_snapshot()
            with workspace_context(first.workspace_id, root, session_catalog=first.session_catalog):
                runner = RunnerCase(first, root)
                session_id, thread_id = await _session(runner)
                for marker, endpoint, credential in (
                    ("header-added", old, "MCP_TEST_OLD_CREDENTIAL"),
                    ("header-replaced", new, "MCP_TEST_NEW_CREDENTIAL"),
                ):
                    previous = owner.binding_snapshot()
                    server = mcp_servers(payload)["schema"]
                    server["url"] = endpoint.url
                    server["headers"] = {"Authorization": "Bearer ${" + credential + "}"}
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    status = await wait_status(service, lambda status, previous=previous:
                        status.healthy and owner.binding_snapshot() != previous)
                    assert status.state == "active" and not status.deferred_paths and not status.restart_required
                    assert status.applied_paths == (("/mcp/servers/schema",) if marker == "header-added" else (
                        "/mcp/servers/schema/headers/Authorization", "/mcp/servers/schema/url",
                    ))
                    plan_tool_loop(replay, first, session_id, thread_id, marker)
                    job = await _start_job(runner, "user", session_id, thread_id)
                    await asyncio.wait_for(asyncio.shield(job.task), 30)
                    assert job.status == JobStatus.completed, job.error_message
                    assert [json.loads(line) for line in endpoint.receipts.read_text().splitlines()] == [
                        {"name": "probe", "arguments": {"marker": marker}},
                    ]
                generation = owner.binding_snapshot().resolve("mcp__schema__probe").connection_ref
                mcp_servers(payload)["schema"]["enabled"] = False
                path.write_text(json.dumps(payload), encoding="utf-8")
                status = await wait_status(service, lambda status: status.healthy and not owner.get_tool_ids())
                assert status.applied_paths == ("/mcp/servers/schema/enabled",) and not status.deferred_paths
                mcp_servers(payload).pop("schema")
                path.write_text(json.dumps(payload), encoding="utf-8")
                status = await wait_status(service, lambda status: status.healthy
                    and "schema" not in service.get_snapshot().to_dict()["mcp"]["servers"])
                assert status.applied_paths == ("/mcp/servers/schema",) and not status.deferred_paths
                mcp_servers(payload)["schema"] = {"enabled": True, "transport": "streamable_http", "url": new.url,
                    "headers": {"Authorization": "Bearer ${MCP_TEST_NEW_CREDENTIAL}"}}
                path.write_text(json.dumps(payload), encoding="utf-8")
                status = await wait_status(service, lambda status: status.healthy and bool(owner.get_tool_ids()))
                assert status.applied_paths == ("/mcp/servers/schema",) and not status.deferred_paths
                current = owner.binding_snapshot()
                assert current.resolve("mcp__schema__probe").connection_ref != generation
                # 未知字段必须在候选校验阶段拒绝，不得借 Server 根规则获得热应用。
                active_snapshot = service.get_snapshot()
                events = service.list_config_events()
                mcp_servers(payload)["schema"]["unknown"] = True
                path.write_text(json.dumps(payload), encoding="utf-8")
                invalid = await wait_status(service, lambda status: not status.healthy)
                assert invalid.reason == "invalid_config" and invalid.state == "active"
                assert invalid.active_revision == status.active_revision
                assert service.get_snapshot() is active_snapshot and service.list_config_events() == events
                assert owner.binding_snapshot() is current
                assert sibling.mcp_catalog_owner.binding_snapshot() == sibling_binding
                assert controller.transport.call_count == 4
    assert_closed(mounted_case)


@pytest.mark.parametrize("boundary", ["turn", "model_call"])
async def test_disabled_sealed_call_commits_failure_and_releases_qualification_before_next_model(
    mounted_case: MountedCase, inprocess_model_replay, http_schema_endpoint_factory, monkeypatch, boundary,
):
    replay, controller = inprocess_model_replay
    ready, release = asyncio.Event(), asyncio.Event()
    calls: Sequence[str] = ()
    first = None
    original_resource = None

    async def observe_phase(call_id, phase):
        assert phase == "model.response_ready"
        if call_id == calls[0]:
            ready.set()
            await release.wait()
        elif call_id == calls[1]:
            # 真实后继请求已经取得 canonical 拒绝结果；不等 Job 结束或资格到期。
            assert first is not None and original_resource is not None
            leases = first.external_resource_leases.leases_for_resource(original_resource)
            assert not any(lease.lease_id.startswith("mcp-prepared:") for lease in leases)
            assert not any(lease.lease_id.startswith("mcp-invocation:") for lease in leases)

    monkeypatch.setattr("app.testing.model_stream.transport.observe_phase", observe_phase)
    async with http_schema_endpoint_factory(f"disabled-{boundary}") as endpoint:
        root, sibling_root = mounted_case.roots.values()
        path = root / ".boxteam/workspace.jsonc"
        payload = {
            "mcp": {"servers": {"schema": {"enabled": True, "transport": "streamable_http", "url": endpoint.url}}},
            "context": {"resource_activation": {"overrides": {"mcp_tool_catalog": boundary}}},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        (sibling_root / ".boxteam/workspace.jsonc").write_text(json.dumps({"mcp": {"servers": {}}}), encoding="utf-8")
        async with main.lifespan(main.app):
            first, _sibling = mounted_case.containers
            owner = first.mcp_catalog_owner
            original_resource = owner.binding_snapshot().resolve("mcp__schema__probe").connection_ref
            assert original_resource is not None
            with workspace_context(first.workspace_id, root, session_catalog=first.session_catalog):
                runner = RunnerCase(first, root)
                session_id, thread_id = await _session(runner)
                marker = f"disabled-{boundary.replace('_', '-')}"
                calls = plan_tool_loop(replay, first, session_id, thread_id, marker)
                job = await _start_job(runner, "user", session_id, thread_id)
                try:
                    await asyncio.wait_for(ready.wait(), 30)
                    assert any(lease.lease_id.startswith("mcp-prepared:")
                               for lease in first.external_resource_leases.leases_for_resource(original_resource))
                    mcp_servers(payload)["schema"]["enabled"] = False
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    await wait_status(first.config_service, lambda status: status.healthy and not owner.get_tool_ids())
                finally:
                    release.set()
                await asyncio.wait_for(asyncio.shield(job.task), 30)
                assert job.status == JobStatus.completed, job.error_message
                results = [item for item in first.checkpointer._storage.storage.read_items(session_id, thread_id)
                           if item.semantic_kind == "tool_result" and item.tool_call_id is not None
                           and item.tool_call_id.endswith(f"call-{marker}")]
                assert results
                for item in results:
                    assert isinstance(item.payload, dict)
                    assert item.payload["tool_outcome"] == "failure"
                    assert item.payload["execution_confirmed"] is True
                    assert "原连接" in item.payload["content"]
                assert not endpoint.receipts.exists()
                assert not first.external_resource_leases.leases_for_resource(original_resource)
                assert controller.transport.call_count == 2
    assert_closed(mounted_case)
