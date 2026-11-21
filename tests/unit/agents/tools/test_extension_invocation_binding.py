"""固定扩展入口对封存目录绑定、注册身份和撤权的执行合同。"""

from __future__ import annotations

import hashlib
import weakref

import pytest
from langchain_core.messages import ToolMessage

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tools.extension_invocation import (
    create_extension_tool_invoker_tool,
    sealed_extension_catalog_binding_resolver,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
)


def _extension_binding_inputs(tools):
    """从工具列表构建 sealed binding 输入；origin/server_id 与注册元数据一致。"""
    inputs = []
    for tool_item in tools:
        metadata = getattr(tool_item, "metadata", None)
        server_id = (
            metadata.get("mcp_server_id") if isinstance(metadata, dict) else None
        )
        inputs.append(
            ExtensionTargetBindingInput(
                target_id=tool_item.name,
                origin="mcp" if server_id else "custom",
                input_schema=export_model_tool_json_schema(tool_item),
                server_id=server_id,
                connection_ref=metadata.get("mcp_connection_ref") if server_id else None,
            )
        )
    return inputs


def _sealed_binding_resolver(tools):
    """按工具快照封存 binding ref，并适配为 invoker 的 typed resolve 端口。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:"
        + hashlib.sha256(b"test-catalog-rev").hexdigest(),
        generation=1,
        targets=_extension_binding_inputs(tools),
    )
    return sealed_extension_catalog_binding_resolver(binding), binding


@pytest.mark.asyncio
async def test_sealed_binding_resolver_resolves_sealed_custom_target():
    from langchain_core.tools import tool

    @tool
    def sealed_custom(value: str) -> str:
        """sealed binding 端口下的 custom target。"""
        return f"sealed:{value}"

    resolver, _binding = _sealed_binding_resolver([sealed_custom])
    invoker = create_extension_tool_invoker_tool(
        [sealed_custom],
        catalog_binding_resolver=resolver,
    )

    result = await invoker.ainvoke(
        {"tool_name": "sealed_custom", "arguments": {"value": "ok"}},
    )

    assert result == "sealed:ok"


@pytest.mark.asyncio
async def test_sealed_resolution_is_isolated_from_live_catalog_changes():
    """live 目录新增 target 不影响封存解析；未封存 target 显式失败。"""
    from langchain_core.tools import tool

    @tool
    def sealed_alpha() -> str:
        """封存时存在的 target。"""
        return "alpha"

    @tool
    def live_beta() -> str:
        """封存后才加入 live 目录的 target。"""
        return "beta"

    # binding 只封存 sealed_alpha 快照；live 注册表随后包含额外 target。
    resolver, binding = _sealed_binding_resolver([sealed_alpha])
    invoker = create_extension_tool_invoker_tool(
        [sealed_alpha, live_beta],
        catalog_binding_resolver=resolver,
    )

    alpha_result = await invoker.ainvoke(
        {"tool_name": "sealed_alpha", "arguments": {}},
    )
    assert alpha_result == "alpha"
    assert binding.resolve("sealed_alpha").target_id == "sealed_alpha"
    live_beta_result = await invoker.ainvoke(
        {"tool_name": "live_beta", "arguments": {}},
    )
    assert isinstance(live_beta_result, str)
    assert "live_beta" in live_beta_result
    assert "不存在 target" in live_beta_result


@pytest.mark.asyncio
async def test_sealed_target_missing_registration_fails_closed():
    """sealed target 在注册表中已不存在时 fail closed，不回退 live 目录。"""
    from langchain_core.tools import tool

    @tool
    def sealed_gamma() -> str:
        """只在封存 ref 中存在的 target。"""
        return "gamma"

    resolver, _binding = _sealed_binding_resolver([sealed_gamma])
    invoker = create_extension_tool_invoker_tool(
        [],
        catalog_binding_resolver=resolver,
    )

    missing_result = await invoker.ainvoke(
        {"tool_name": "sealed_gamma", "arguments": {}},
    )
    assert isinstance(missing_result, str)
    assert "缺少可执行注册" in missing_result


@pytest.mark.asyncio
@pytest.mark.parametrize("server_id", ["server-a", "server-b"])
async def test_sealed_mcp_target_requires_matching_server_and_connection_identity(server_id: str):
    """同名同 schema 目标也必须匹配原 Server 和精确连接。"""
    from langchain_core.tools import tool

    @tool
    def sealed_mcp_status(value: str) -> str:
        """MCP target stub。"""
        return f"status:{value}"

    registered = sealed_mcp_status.model_copy(
        update={"metadata": {"mcp_server_id": "server-a", "mcp_connection_ref": "mcp-connection:v1:" + "a" * 32 + ":1"}}
    )
    resolver, _binding = _sealed_binding_resolver([registered])
    matching_invoker = create_extension_tool_invoker_tool(
        [registered],
        catalog_binding_resolver=resolver,
    )
    drifted_invoker = create_extension_tool_invoker_tool(
        [
            sealed_mcp_status.model_copy(
                update={"metadata": {"mcp_server_id": server_id, "mcp_connection_ref": "mcp-connection:v1:" + "b" * 32 + ":1"}}
            )
        ],
        catalog_binding_resolver=resolver,
    )

    matching_result = await matching_invoker.ainvoke(
        {"tool_name": "sealed_mcp_status", "arguments": {"value": "ready"}},
    )
    assert matching_result == "status:ready"
    drifted_result = await drifted_invoker.ainvoke(
        {"tool_name": "sealed_mcp_status", "arguments": {"value": "ready"}},
    )
    assert isinstance(drifted_result, str)
    assert "身份不一致" in drifted_result


def test_owner_resolved_invoker_does_not_retain_initial_tools():
    from langchain_core.tools import tool

    @tool
    def initial_target(value: str) -> str:
        """检查 Agent 信封不再长期捕获初始注册。"""
        return value

    resolver, _binding = _sealed_binding_resolver([initial_target])
    initial_ref = weakref.ref(initial_target)

    def reject_resolution(_binding, _use):
        raise AssertionError("本测试不执行工具")

    invoker = create_extension_tool_invoker_tool(
        [initial_target], catalog_binding_resolver=resolver,
        target_resolver=reject_resolution,
    )
    del initial_target
    assert initial_ref() is None
    assert invoker.name == EXTENSION_TOOL_INVOKER_NAME


@pytest.mark.asyncio
async def test_sealed_resolution_failure_keeps_tool_call_id_pairing():
    """tool_call 输入下，封存解析失败返回按 tool_call_id 配对的错误 ToolMessage。"""
    from langchain_core.tools import tool

    @tool
    def sealed_only() -> str:
        """唯一封存 target。"""
        return "only"

    resolver, _binding = _sealed_binding_resolver([sealed_only])
    invoker = create_extension_tool_invoker_tool(
        [sealed_only],
        catalog_binding_resolver=resolver,
    )

    result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_sealed_miss",
            "name": invoker.name,
            "args": {"tool_name": "not_in_sealed_ref", "arguments": {}},
        }
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call_sealed_miss"
    assert "not_in_sealed_ref" in result.text


@pytest.mark.asyncio
async def test_execution_revocation_fails_at_sealed_target_execution_point():
    """封存调用在执行点重验最新策略，撤权时返回配对失败。"""
    from langchain_core.tools import tool

    @tool
    def revocable_target() -> str:
        """可撤权的 sealed target。"""
        return "should-not-run"

    resolver, _binding = _sealed_binding_resolver([revocable_target])
    enabled = True
    invoker = create_extension_tool_invoker_tool(
        [revocable_target],
        catalog_binding_resolver=resolver,
        is_tool_execution_enabled=lambda _target: enabled,
    )

    enabled_result = await invoker.ainvoke(
        {"tool_name": "revocable_target", "arguments": {}}
    )
    assert enabled_result == "should-not-run"

    enabled = False
    revoked_result = await invoker.ainvoke(
        {
            "type": "tool_call",
            "id": "call_revoked_target",
            "name": invoker.name,
            "args": {"tool_name": "revocable_target", "arguments": {}},
        }
    )
    assert isinstance(revoked_result, ToolMessage)
    assert revoked_result.status == "error"
    assert revoked_result.tool_call_id == "call_revoked_target"
    assert "已被策略禁用" in revoked_result.text
