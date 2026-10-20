"""原生输入解析、归属解析和实际文件执行必须消费同一个总期限。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware

from app.agents import workspace_filesystem_tools
from app.agents.workspace_backend import build_workspace_backend
from app.services.infrastructure.resource_platform.sources import resource_navigation


@pytest.fixture
def deadline_tools(configure_filesystem_tools, tmp_path: Path, monkeypatch):
    middleware = FilesystemMiddleware(backend=build_workspace_backend(tmp_path), tool_token_limit_before_evict=None)
    owner = configure_filesystem_tools(middleware, workspace_root=tmp_path)
    clock = [100.0]
    time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(workspace_filesystem_tools, "time", time)
    monkeypatch.setattr(resource_navigation, "time", time)
    original_input, original_parse = owner.input_uri, owner._parse_target

    def resolve_input(value):
        clock[0] += 0.4
        return original_input(value)

    def parse_target(request, gateway_id):
        assert request.timeout_ms == 1000, "执行预算不能改写分页查询合同"
        clock[0] += 0.7
        return original_parse(request, gateway_id)

    def forbidden_root(*args, **kwargs):
        pytest.fail("总期限已耗尽，不得重新取得预算后执行文件操作")

    monkeypatch.setattr(owner, "input_uri", resolve_input)
    monkeypatch.setattr(owner, "_parse_target", parse_target)
    monkeypatch.setattr(owner, "_root", forbidden_root)
    return {tool.name: tool for tool in middleware.tools}


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("name", ["ls", "read_file", "glob", "grep"])
async def test_tool_resolution_cannot_renew_owner_budget(deadline_tools, synchronous, name):
    arguments = {"path": "src", "timeout_ms": 1000, "runtime": SimpleNamespace(tool_call_id="shared-deadline")}
    if name in {"glob", "grep"}:
        arguments["pattern"] = "needle"
    call = {"name": name, "args": arguments, "id": "shared-deadline", "type": "tool_call"}
    message = deadline_tools[name].invoke(call) if synchronous else await deadline_tools[name].ainvoke(call)
    assert message.status == "error"
    assert json.loads(message.content) == {
        "owner": "resource_file_operations", "code": "output_limit_exceeded",
        "reason": "deadline", "message": "文件导航超过本次总期限",
    }
