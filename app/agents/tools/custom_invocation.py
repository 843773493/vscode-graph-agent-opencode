from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
    validate_model_tool_arguments,
)
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.agents.tool_invocation_context import ToolInvocationContext
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
    ExtensionTargetBinding,
    ExtensionTargetBindingInput,
    ExtensionTargetResolutionError,
    build_extension_catalog_binding,
)


class CustomToolInvocationInput(BaseModel):
    """固定扩展工具入口参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tool_name: str = Field(
        min_length=1,
        description="要调用的目标扩展工具名称；名称来自当前已生效的扩展目录或指引。",
    )
    arguments: dict[str, Any] = Field(
        description="传给目标扩展工具的参数对象；目标工具不需要参数时传空对象 {}。",
    )


def _normalize_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise ToolException(
            f"arguments 必须是 object，实际类型: {type(arguments).__name__}"
        )
    return arguments


def _validate_target_arguments(
    target_tool: BaseTool,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_arguments = _normalize_arguments(arguments)
    schema = export_model_tool_json_schema(target_tool)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        unknown_fields = sorted(set(normalized_arguments) - set(properties))
        if unknown_fields:
            raise ToolException(
                f"扩展工具 {target_tool.name} 包含未知参数: "
                f"{', '.join(unknown_fields)}"
            )
    try:
        return validate_model_tool_arguments(target_tool, normalized_arguments)
    except (TypeError, ValueError) as error:
        raise ToolException(
            f"扩展工具 {target_tool.name} 参数校验失败: {error}"
    ) from error


# E4 第三段 typed resolve 端口：返回当前 model-call 封存的扩展目录 binding ref。
ExtensionCatalogBindingResolver = Callable[[], ExtensionCatalogBindingRef]


def sealed_extension_catalog_binding_resolver(
    binding: ExtensionCatalogBindingRef,
) -> ExtensionCatalogBindingResolver:
    """把已封存的 binding ref 适配为 invoker 的 typed resolve 端口。"""

    def resolve() -> ExtensionCatalogBindingRef:
        return binding

    return resolve


def seal_extension_catalog_binding_from_tools(
    tools: Sequence[BaseTool],
) -> ExtensionCatalogBindingResolver:
    """从工具快照封存 binding ref 并适配为 typed resolve 端口。

    装配期 interim owner：在 McpCatalogActivationBinder 生产接线前，工厂用
    本函数把当次装配解析出的扩展工具集冻结为 sealed binding；revision 由
    快照内容确定性导出，目录后续变化不影响已封存 ref。
    TODO(E4 owner 接线): 激活边界 owner 落地后改由真实 catalog revision/generation 封存。
    """
    entries: list[dict[str, object]] = []
    for tool_item in tools:
        metadata = getattr(tool_item, "metadata", None)
        server_id = (
            metadata.get("mcp_server_id") if isinstance(metadata, dict) else None
        )
        schema = export_model_tool_json_schema(tool_item)
        properties = schema.get("properties")
        entries.append(
            {
                "target_id": tool_item.name,
                "origin": "mcp" if server_id else "custom",
                "server_id": server_id,
                "args": properties if isinstance(properties, dict) else {},
            }
        )
    entries.sort(key=lambda entry: str(entry["target_id"]))
    revision_payload = json.dumps(
        entries, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:"
        + hashlib.sha256(revision_payload.encode()).hexdigest(),
        generation=1,
        targets=[
            ExtensionTargetBindingInput(
                target_id=str(entry["target_id"]),
                origin=str(entry["origin"]),
                args=entry["args"],
                server_id=entry["server_id"],
            )
            # dict 字面量键集合固定，类型收窄安全。
            for entry in entries
        ],
    )
    return sealed_extension_catalog_binding_resolver(binding)


def _assert_sealed_target_matches_registration(
    sealed_target: ExtensionTargetBinding,
    target_tool: BaseTool,
) -> None:
    """封存 target 与可执行注册身份不一致时显式失败，不回退 live 目录。"""
    metadata = getattr(target_tool, "metadata", None)
    registered_server_id = (
        metadata.get("mcp_server_id") if isinstance(metadata, dict) else None
    )
    expected_origin = "mcp" if sealed_target.server_id else "custom"
    registered_origin = "mcp" if registered_server_id else "custom"
    if expected_origin != registered_origin or (
        sealed_target.server_id is not None
        and registered_server_id != sealed_target.server_id
    ):
        raise ExtensionTargetResolutionError(
            "sealed target 与可执行注册身份不一致: "
            f"tool_name={sealed_target.target_id!r} "
            f"sealed_origin={sealed_target.origin!r} "
            f"sealed_server_id={sealed_target.server_id!r} "
            f"registered_origin={registered_origin!r} "
            f"registered_server_id={registered_server_id!r}"
        )


async def _invoke_target_tool_without_nested_callbacks(
    target_tool: BaseTool,
    arguments: dict[str, Any] | None,
) -> Any:
    call_arguments = _validate_target_arguments(target_tool, arguments)

    coroutine = getattr(target_tool, "coroutine", None)
    if callable(coroutine):
        return await coroutine(**call_arguments)

    func = getattr(target_tool, "func", None)
    if callable(func):
        result = func(**call_arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    # TODO: 支持没有暴露 func/coroutine 的自定义 BaseTool 时，仍会经过 LangChain 回调。
    return await target_tool.ainvoke(call_arguments)


def create_extension_tool_invoker_tool(
    custom_tools: Sequence[BaseTool],
    *,
    is_tool_execution_enabled: Callable[[BaseTool], bool] | None = None,
    catalog_binding_resolver: ExtensionCatalogBindingResolver | None = None,
    invocation_context: ToolInvocationContext | None = None,
) -> BaseTool:
    """创建固定扩展工具入口，通过参数分发到工作区配置的自定义工具。"""
    tools_by_name: dict[str, BaseTool] = {}
    for custom_tool in custom_tools:
        if custom_tool.name in tools_by_name:
            raise ValueError(f"重复的扩展工具: {custom_tool.name}")
        tools_by_name[custom_tool.name] = custom_tool
    if catalog_binding_resolver is None:
        raise ValueError(
            "extension invoker 必须显式提供 sealed catalog binding resolver；"
            "无 binding 时 fail closed，禁止回退 live 目录解析"
        )

    async def invoke_extension_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        resolved_tool_name = tool_name.strip()
        if not resolved_tool_name:
            raise ValueError("tool_name 不能为空")

        # E4 第三段：旧 tool call 只按封存 ref 解析；live 目录后续增删改
        # 不参与解析，缺失 target 显式 paired failure，不回退 live 目录。
        binding_ref = catalog_binding_resolver()
        try:
            sealed_target = binding_ref.resolve(resolved_tool_name)
        except ExtensionTargetResolutionError as error:
            raise ToolException(str(error)) from error
        target_tool = tools_by_name.get(resolved_tool_name)
        if target_tool is None:
            raise ToolException(
                "sealed extension catalog binding 中的 target 缺少可执行注册: "
                f"tool_name={resolved_tool_name!r} "
                f"binding_id={binding_ref.binding_id}"
            )
        try:
            _assert_sealed_target_matches_registration(sealed_target, target_tool)
        except ExtensionTargetResolutionError as error:
            raise ToolException(str(error)) from error
        if is_tool_execution_enabled is not None and not is_tool_execution_enabled(
            target_tool
        ):
            # 权限撤销在执行点拒绝，并按 envelope 合同返回真实 paired result。
            raise ToolException(f"扩展工具 {resolved_tool_name!r} 已被策略禁用")

        binding_token = (
            invocation_context.set_extension_catalog_binding(binding_ref)
            if invocation_context is not None
            else None
        )
        try:
            return await _invoke_target_tool_without_nested_callbacks(
                target_tool,
                arguments,
            )
        finally:
            if invocation_context is not None and binding_token is not None:
                invocation_context.reset_extension_catalog_binding(binding_token)

    description = (
        "调用工作区配置的扩展工具。"
        "当需要执行未直接出现在 tools 列表中的目标扩展工具时，必须真实调用本工具，"
        "不要在正文里复述或伪造调用。"
        "目标工具名称和参数来自当前已生效的扩展目录、AGENTS.md、SKILL.md 或普通说明文档。"
        '调用参数只有 tool_name 和 arguments，例如 {"tool_name": "<目标工具名>", "arguments": {}}。'
    )
    return StructuredTool.from_function(
        coroutine=invoke_extension_tool,
        name=EXTENSION_TOOL_INVOKER_NAME,
        description=description,
        args_schema=CustomToolInvocationInput,
        handle_tool_error=True,
    )
