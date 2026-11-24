"""请求参数按层归一；SDK 正式字段与扩展正文不允许互相覆盖。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.agents.provider_api_mode import ApiProtocol
from configs.inheritance.merge import SettingsMerger
from configs.inheritance.values import FieldOrigin, freeze, thaw

# 这些字段由连接、封存正文或执行生命周期持有，不是模型采样参数。
CONFIGURATION_OWNED_PARAMETERS = frozenset({
    "model", "messages", "input", "instructions", "tools", "functions",
    "api_key", "api_base", "base_url", "base_model", "custom_llm_provider",
    "organization", "api_version", "deployment_id", "headers", "extra_headers",
    "default_headers", "timeout", "request_timeout", "max_retries", "num_retries",
    "stream", "stream_options", "include", "store", "previous_response_id",
    "prompt", "client", "async_client", "litellm_session_id",
})


def normalize_request_parameters(
    parameters: Mapping[str, Any], *, protocol: ApiProtocol,
) -> dict[str, Any]:
    """同一层先映射已有输出长度别名，再检查歧义；null 留给最终合并。"""
    aliases = (
        {"max_tokens": "max_output_tokens"}
        if protocol == "responses" else {"max_output_tokens": "max_tokens"}
    )
    result: dict[str, Any] = {}
    for key, value in parameters.items():
        canonical = aliases.get(key, key)
        if canonical in result:
            raise ValueError(f"请求参数别名重复: {key} 与 {canonical}")
        result[canonical] = value
    return result


def merge_request_parameters(
    *layers: Mapping[str, Any], protocol: ApiProtocol,
) -> dict[str, Any]:
    """临时参数子节点复用配置合并；完成逻辑键覆盖后才按叶子协议映射并省略 null。"""
    merger = SettingsMerger({
        "x-key-aliases": {"max_tokens": "max_output_tokens"},
        "additionalProperties": {"x-merge": "replace"},
    })
    result, origins = freeze({}), None
    for index, layer in enumerate(layers):
        result, origins = merger.merge(result, freeze(layer),
            origin=FieldOrigin(f"request:{index}", "", ()), inherited_origins=origins)
    values = normalize_request_parameters(thaw(result), protocol=protocol)
    return {key: value for key, value in values.items() if value is not None}


def normalize_request_headers(headers: Mapping[str, object]) -> dict[str, str]:
    """同层大小写重复直接拒绝，跨层按同一 HTTP 名称覆盖。"""
    result: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("request_options.default_headers 必须是字符串映射")
        folded = name.casefold()
        if folded in result:
            raise ValueError(f"request_options.default_headers 大小写重复: {name}")
        if folded in {"authorization", "proxy-authorization", "api-key", "x-api-key"}:
            raise ValueError(f"request_options.default_headers 不能提供认证凭据: {name}")
        result[folded] = value
    return result


def validate_configured_parameters(parameters: Mapping[str, Any]) -> None:
    """纯本地预检不加载模型 SDK，也不解析凭据。"""
    forbidden = sorted(parameters.keys() & CONFIGURATION_OWNED_PARAMETERS)
    if forbidden:
        raise ValueError("request_options.parameters 不允许覆盖: " + ", ".join(forbidden))
    extra = parameters.get("extra_body")
    if extra is not None and not isinstance(extra, Mapping):
        raise TypeError("request_options.parameters.extra_body 必须是对象")
    if isinstance(extra, Mapping):
        forbidden = sorted(extra.keys() & CONFIGURATION_OWNED_PARAMETERS)
        if forbidden:
            raise ValueError("request_options.parameters.extra_body 不允许覆盖: " + ", ".join(forbidden))


def formal_request_parameter_names(protocol: ApiProtocol) -> set[str]:
    """迁移和运行时校验复用 SDK 的正式字段，不维护第二套参数清单。"""
    from app.runtime.litellm_runtime import _import_litellm

    # 本地字段校验只读 SDK 声明，不启动模型目录刷新或等待其网络结果。
    litellm = _import_litellm()

    if protocol == "responses":
        return set(litellm.types.llms.openai.ResponsesAPIRequestParams.__annotations__)
    return set(litellm.OPENAI_CHAT_COMPLETION_PARAMS)


def validate_extra_body(parameters: Mapping[str, Any], *, protocol: ApiProtocol) -> None:
    """复用当前 SDK 的正式参数声明，不维护另一套厂商参数注册表。"""
    extra = parameters.get("extra_body")
    if extra is None:
        return
    if not isinstance(extra, Mapping):
        raise TypeError("request_options.parameters.extra_body 必须是对象")
    formal = formal_request_parameter_names(protocol)
    conflicts = sorted(extra.keys() & (formal | CONFIGURATION_OWNED_PARAMETERS | parameters.keys()))
    if conflicts:
        raise ValueError(
            "request_options.parameters.extra_body 重复正式参数，请改用 parameters: "
            + ", ".join(conflicts)
        )
