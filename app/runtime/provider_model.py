"""Provider 本地校验与按需模型构造，普通列表不加载 Agent 图或模型 SDK。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.agents.provider_api_mode import ProviderApiMode, parse_provider_api_mode
from app.core.failures import (
    execution_failure_message,
    public_failure_message,
    report_failure,
)

if TYPE_CHECKING:
    from app.agents.sealed_assembly_dispatch import ProviderInvocationPurpose


PROVIDER_REQUEST_OPTION_KEYS = {"overrides", "default_headers", "no_proxy"}


def _validate_provider_configuration(
    provider: dict[str, Any],
) -> tuple[ProviderApiMode, dict[str, Any]]:
    """列表与执行共用本地配置和凭据校验，不导入或构造模型 SDK。"""
    api_mode = parse_provider_api_mode(provider)
    custom_llm_provider = provider.get("custom_llm_provider")
    if not isinstance(custom_llm_provider, str) or not custom_llm_provider:
        raise ValueError(
            f"provider {provider.get('id') or provider.get('model')!r} "
            "缺少 llm.providers[].custom_llm_provider 配置"
        )

    if custom_llm_provider == "chatgpt":
        from app.runtime.chatgpt_auth import (
            configure_litellm_chatgpt_auth_directory,
            ensure_chatgpt_oauth_ready,
            is_chatgpt_oauth_provider,
        )

        if not is_chatgpt_oauth_provider(provider):
            raise ValueError(
                "ChatGPT provider 必须配置 auth.type='oauth' 和 "
                "auth.method='chatgpt'"
            )
        if api_mode.protocol != "responses":
            raise ValueError(
                "ChatGPT OAuth provider 必须配置 api_mode.protocol='responses'"
            )
        token_dir = configure_litellm_chatgpt_auth_directory()
        ensure_chatgpt_oauth_ready(token_dir)
    elif api_mode.protocol == "anthropic_messages" and custom_llm_provider != "anthropic":
        raise ValueError(
            "Anthropic Messages provider 必须配置 "
            "custom_llm_provider='anthropic'"
        )

    return api_mode, _get_provider_request_options(provider)


def build_model_from_provider(
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    prompt_cache_key: str | None = None,
    invocation_purpose: ProviderInvocationPurpose = "agent",
) -> Any:
    """从 provider 构建模型；SDK 初始化与构造错误只在执行入口报告。"""
    api_mode, request_options = _validate_provider_configuration(provider)
    if provider["custom_llm_provider"] == "chatgpt":
        from app.runtime.chatgpt_auth import ensure_litellm_chatgpt_model_capabilities

        ensure_litellm_chatgpt_model_capabilities(provider["model"])

    if api_mode.protocol == "responses":
        from app.agents.providers.openai_responses import build_openai_responses_model

        model = build_openai_responses_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
            prompt_cache_key=prompt_cache_key,
            invocation_purpose=invocation_purpose,
        )
    else:
        from app.agents.providers.litellm_chat import build_litellm_chat_model

        model = build_litellm_chat_model(
            provider=provider,
            runtime_config=runtime_config,
            request_options=request_options,
            prompt_cache_key=prompt_cache_key,
            invocation_purpose=invocation_purpose,
        )
    if api_mode.model_info.max_input_tokens is not None:
        # 容量属于模型元数据，不向上游发送，也不据此设置输出上限。
        model.profile = {
            **(model.profile or {}),
            "max_input_tokens": api_mode.model_info.max_input_tokens,
        }
    return model


def provider_configuration_error(
    provider: dict[str, Any],
) -> str | None:
    """返回本地配置及必要凭据错误；SDK 可构造性留到实际执行时校验。"""
    try:
        _validate_provider_configuration(provider)
    except Exception as error:  # noqa: BLE001 - 配置错误需要逐 provider 隔离展示
        return provider_build_failure(provider, error)
    return None


def provider_build_failure(provider: Mapping[str, object], error: Exception) -> str:
    """原生错误先分离诊断，再由列表或首选模型入口发布，保留业务拒绝说明。"""
    if (message := public_failure_message(error)) is not None:
        return message
    error.add_note(f"模型配置构建失败: provider_id={provider.get('id') or provider.get('model') or '<unknown>'}")
    return report_failure(error, detail=execution_failure_message(error) or type(error).__name__)


def _get_provider_request_options(provider: dict[str, Any]) -> dict[str, Any]:
    """读取 provider 级请求选项，并在拼错字段时直接报错。"""
    request_options = provider.get("request_options") or {}
    if not isinstance(request_options, dict):
        raise TypeError("provider.request_options 必须是对象")

    unknown_keys = sorted(set(request_options) - PROVIDER_REQUEST_OPTION_KEYS)
    if unknown_keys:
        raise ValueError(f"provider.request_options 包含不支持的字段: {', '.join(unknown_keys)}")

    overrides = request_options.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise TypeError("provider.request_options.overrides 必须是对象")
    default_headers = request_options.get("default_headers") or {}
    if not isinstance(default_headers, dict):
        raise TypeError("provider.request_options.default_headers 必须是对象")
    no_proxy = request_options.get("no_proxy", False)
    if not isinstance(no_proxy, bool):
        raise TypeError("provider.request_options.no_proxy 必须是布尔值")
    return {
        "overrides": dict(overrides),
        "default_headers": dict(default_headers),
        "no_proxy": no_proxy,
    }


