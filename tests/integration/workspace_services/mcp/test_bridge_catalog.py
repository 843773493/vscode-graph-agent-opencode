"""正式 stdio 桥转送分页与变化通知，目录仍由唯一工作区 owner 发布。"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx_sse import EventSource
from mcp import types
from mcp.shared._httpx_utils import create_mcp_http_client

from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from tests.support.mcp.http_endpoint import sdk_http_session_factory


@pytest.mark.parametrize("progress_token", ["client-progress", 73])
async def test_progress_precedes_result_without_standalone_get_stream(
    bridge_endpoint_factory, progress_token: str | int,
):
    async with bridge_endpoint_factory() as (endpoint, token), create_mcp_http_client(
        headers={"X-Local-Token": token, "Accept": "application/json, text/event-stream"},
    ) as client:
        initialize = types.JSONRPCRequest(
            jsonrpc="2.0", id="init", method="initialize", params={
                "protocolVersion": types.LATEST_PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "post-stream-test", "version": "1"},
            },
        )
        async with client.stream("POST", endpoint.url, json=initialize.model_dump()) as response:
            response.raise_for_status()
            session_id = response.headers["mcp-session-id"]
            initialized = [types.JSONRPCMessage.model_validate_json(event.data).root
                           async for event in EventSource(response).aiter_sse()]
        assert len(initialized) == 1 and isinstance(initialized[0], types.JSONRPCResponse)
        client.headers.update({"mcp-session-id": session_id,
                               "mcp-protocol-version": initialized[0].result["protocolVersion"]})
        response = await client.post(endpoint.url, json=types.JSONRPCNotification(
            jsonrpc="2.0", method="notifications/initialized",
        ).model_dump(exclude_none=True))
        response.raise_for_status()
        call = types.JSONRPCRequest(jsonrpc="2.0", id="tool-call", method="tools/call", params={
            "name": "probe", "arguments": {"call_mode": "progress"},
            "_meta": {"progressToken": progress_token},
        })
        async with client.stream("POST", endpoint.url, json=call.model_dump()) as response:
            response.raise_for_status()
            messages = [types.JSONRPCMessage.model_validate_json(event.data).root
                        async for event in EventSource(response).aiter_sse()]
        assert len(messages) == 3
        for message, progress in zip(messages[:2], (1, 2), strict=True):
            assert isinstance(message, types.JSONRPCNotification)
            assert message.method == "notifications/progress"
            assert message.params is not None
            assert (message.params["progressToken"], message.params["progress"], message.params["total"]) == (progress_token, progress, 2)
        assert isinstance(messages[2], types.JSONRPCResponse) and messages[2].id == call.id
        (await client.delete(endpoint.url)).raise_for_status()


async def test_paginated_catalog_and_notification_publish_complete_revision(bridge_endpoint_factory):
    async with bridge_endpoint_factory(page_size=1, notify_changes=True) as (endpoint, token):
        initial = {name: {"type": "object"} for name in ("probe", "second", "third")}
        endpoint.definitions.write_text(json.dumps(initial), encoding="utf-8")
        owner = McpCatalogOwner(
            leases=ExternalResourceLeaseLedger(state_path=endpoint.context.workspace_root / ".boxteam/resources.json"),
            raw_config={"servers": {"schema": {
                "enabled": True, "transport": "streamable_http", "url": endpoint.url,
                "headers": {"X-Local-Token": token},
            }}},
            workspace_root=endpoint.context.workspace_root, event_service=EventChannelService(),
            session_factory=sdk_http_session_factory,
        )
        try:
            await owner.start()
            assert owner.get_tool_ids() == frozenset(f"mcp__schema__{name}" for name in initial)
            before = owner.catalog_revision()
            runtime, = owner._runtimes.values()
            assert runtime.session is not None and runtime.session.supports_tool_list_changed()
            # 只改服务端资产，不代替产品发布目录；由真实通知触发原 owner 重读全部分页。
            updated = {name: {"type": "object"} for name in ("probe", "fourth", "fifth")}
            endpoint.definitions.write_text(json.dumps(updated), encoding="utf-8")
            probe = next(tool for tool in owner.get_tools() if tool.name == "mcp__schema__probe")
            await probe.ainvoke({"call_mode": "notify_tools_changed"})
            async with asyncio.timeout(5):
                while owner.catalog_revision() == before:
                    await asyncio.sleep(0.01)
            assert owner.get_tool_ids() == frozenset(f"mcp__schema__{name}" for name in updated)
            assert [json.loads(line) for line in endpoint.receipts.read_text(encoding="utf-8").splitlines()] == [
                {"name": "probe", "arguments": {"call_mode": "notify_tools_changed"}},
            ]
        finally:
            await owner.shutdown()


@pytest.mark.parametrize(
    ("capabilities", "expect_error"),
    [({"roots": {}}, True), ({}, False)],
)
async def test_initialize_rejects_unhandled_client_capability_and_accepts_empty_capabilities(
    bridge_endpoint_factory, capabilities: dict[str, object], expect_error: bool,
):
    async with bridge_endpoint_factory() as (endpoint, token), create_mcp_http_client(
        headers={"X-Local-Token": token, "Accept": "application/json, text/event-stream"},
    ) as client:
        initialize = types.JSONRPCRequest(
            jsonrpc="2.0", id="capabilities", method="initialize", params={
                "protocolVersion": types.LATEST_PROTOCOL_VERSION, "capabilities": capabilities,
                "clientInfo": {"name": "capability-contract-test", "version": "1"},
            },
        )
        async with client.stream("POST", endpoint.url, json=initialize.model_dump()) as response:
            response.raise_for_status()
            session_id = response.headers["mcp-session-id"]
            messages = [
                types.JSONRPCMessage.model_validate_json(event.data).root
                async for event in EventSource(response).aiter_sse()
            ]
        assert len(messages) == 1
        if expect_error:
            assert isinstance(messages[0], types.JSONRPCError)
            assert messages[0].id == initialize.id
            assert messages[0].error.code == -32600
            assert messages[0].error.message
        else:
            assert isinstance(messages[0], types.JSONRPCResponse)
            assert messages[0].id == initialize.id
        client.headers.update({"mcp-session-id": session_id})
        (await client.delete(endpoint.url)).raise_for_status()
