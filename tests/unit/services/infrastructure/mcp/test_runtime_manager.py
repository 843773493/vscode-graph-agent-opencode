from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.services.infrastructure.mcp.runtime_manager import McpRuntimeManager


class _EchoInput(BaseModel):
    text: str


@pytest.fixture
def fake_remote_tool() -> StructuredTool:
    async def echo(text: str) -> str:
        return f"remote:{text}"

    return StructuredTool.from_function(
        coroutine=echo,
        name="echo",
        description="回显文本",
        args_schema=_EchoInput,
    )


@pytest.fixture
def fake_client_class(fake_remote_tool: StructuredTool):
    class _FakeClient:
        callbacks = None
        session_closed = False

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        @asynccontextmanager
        async def session(self, server_name: str):
            assert server_name == "mini"
            try:
                yield object()
            finally:
                self.session_closed = True

    return _FakeClient


@pytest.mark.asyncio
async def test_runtime_manager_discovers_namespaces_and_calls_tool(
    tmp_path: Path,
    fake_client_class,
    fake_remote_tool: StructuredTool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.infrastructure.mcp.runtime_manager.MultiServerMCPClient",
        fake_client_class,
    )
    async def fake_load_mcp_tools(session, **kwargs):
        assert session is not None
        assert kwargs["server_name"] == "mini"
        return [fake_remote_tool]

    monkeypatch.setattr(
        "app.services.infrastructure.mcp.runtime_manager.load_mcp_tools",
        fake_load_mcp_tools,
    )
    manager = McpRuntimeManager(
        raw_config={
            "servers": {
                "mini": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "mini-server",
                }
            }
        },
        workspace_root=tmp_path,
    )

    await manager.start()

    tools = manager.get_tools()
    assert [tool.name for tool in tools] == ["mcp__mini__echo"]
    assert tools[0].metadata["mcp_server_id"] == "mini"
    snapshots = manager.list_servers()
    assert snapshots[0].status == "ready"
    assert snapshots[0].tools[0].remote_name == "echo"
    assert await tools[0].ainvoke({"text": "hello"}) == "remote:hello"
    assert manager.get_tool_ids() == frozenset({"mcp__mini__echo"})

    await manager.shutdown()
    assert manager._clients == {}
