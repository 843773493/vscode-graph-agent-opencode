"""已生成的 Anthropic Messages 正文直接进入 SDK 传输和响应解析。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from app.runtime.litellm_runtime import _import_litellm


def complete_anthropic_messages(
    body: dict[str, Any], *, params: Mapping[str, Any], client: Any, asynchronous: bool,
) -> Any:
    """跳过 Chat 输入转换；连接池、流解析和非流式输出继续归现有 SDK。"""
    sdk = _import_litellm()
    from litellm.anthropic_beta_headers_manager import update_headers_with_filtered_beta
    from litellm.litellm_core_utils.litellm_logging import Logging
    from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    from litellm.litellm_core_utils.url_utils import ensure_api_route
    from litellm.llms.anthropic.chat.handler import AnthropicChatCompletion, make_sync_call
    from litellm.llms.anthropic.common_utils import process_anthropic_headers
    from litellm.llms.anthropic.chat.transformation import ANTHROPIC_TOOL_NAME_REVERSE_MAP_KEY
    from litellm.secret_managers.main import get_secret_bool
    from litellm.utils import get_optional_params

    model, messages = body["model"], body["messages"]
    stream = bool(body.get("stream"))
    config = sdk.AnthropicConfig()
    # TODO: LiteLLM 提供接收最终 Messages body 的公开 completion API 后，替换此内部传输入口。
    # 仅还原响应解码选项，不将历史正文再次交给 Chat→Messages 转换。
    options = get_optional_params(
        model=model, messages=[], custom_llm_provider="anthropic",
        **{key: value for key, value in params.items() if key not in {
            "model", "messages", "custom_llm_provider", "api_key", "api_base", "client",
        }},
    )
    json_mode = bool(options.get("json_mode", False))
    api_base = params["api_base"]
    if not get_secret_bool("LITELLM_ANTHROPIC_DISABLE_URL_SUFFIX"):
        api_base = ensure_api_route(api_base, "v1/messages")
    api_key = params.get("api_key")
    headers = config.validate_environment(
        headers=dict(params.get("extra_headers") or {}), model=model, messages=messages,
        optional_params=body, litellm_params={}, api_key=api_key, api_base=api_base,
    )
    headers = update_headers_with_filtered_beta(headers=headers, provider="anthropic", api_base=api_base)
    _, reverse_names = config._build_request_tool_name_maps(params.get("tools") or [])
    response_params = {ANTHROPIC_TOOL_NAME_REVERSE_MAP_KEY: reverse_names}
    call_id = str(uuid4())
    logging = Logging(
        model=model, messages=messages, stream=stream,
        call_type="acompletion" if asynchronous else "completion",
        start_time=datetime.now(UTC), litellm_call_id=call_id, function_id=call_id,
    )
    logging.update_environment_variables(
        litellm_params={"custom_llm_provider": "anthropic"}, optional_params=options,
        custom_llm_provider="anthropic",
    )
    logging.pre_call(input=messages, api_key=api_key, additional_args={
        "complete_input_dict": body, "api_base": api_base, "headers": headers,
    })
    timeout = params.get("timeout") or sdk.request_timeout
    common = dict(
        model=model, messages=messages, api_base=api_base, custom_prompt_dict={},
        model_response=sdk.ModelResponse(), print_verbose=sdk.print_verbose, encoding=None,
        api_key=api_key, logging_obj=logging, stream=stream, _is_function_call=False,
        data=body, json_mode=json_mode, optional_params=options, litellm_params=response_params,
        headers=headers, timeout=timeout, client=client,
    )
    handler = AnthropicChatCompletion()
    if asynchronous:
        async def send():
            try:
                if stream:
                    return await handler.acompletion_stream_function(**common)
                return await handler.acompletion_function(provider_config=config, **common)
            except Exception as error:
                raise _provider_error(model, error)
        return send()
    try:
        if stream:
            from litellm.llms.anthropic.chat.transformation import JsonModeRequest

            iterator, response_headers = make_sync_call(
                client=client, api_base=api_base, headers=headers, data=json.dumps(body),
                model=model, messages=messages, logging_obj=logging, timeout=timeout,
                json_mode=json_mode, speed=options.get("speed"),
                tool_name_reverse_map=reverse_names,
                response_format_schema=JsonModeRequest.model_validate(options).tool_schema() if json_mode else None,
            )
            return CustomStreamWrapper(
                completion_stream=iterator, model=model, custom_llm_provider="anthropic",
                logging_obj=logging, _response_headers=process_anthropic_headers(response_headers),
            )
        response = client.post(api_base, headers=headers, data=json.dumps(body), timeout=timeout, logging_obj=logging)
        return config.transform_response(
            model=model, raw_response=response, model_response=common["model_response"],
            logging_obj=logging, api_key=api_key, request_data=body, messages=messages,
            optional_params=options, litellm_params=response_params, encoding=None, json_mode=json_mode,
        )
    except Exception as error:
        raise _provider_error(model, error)


def _provider_error(model: str, error: Exception) -> Exception:
    """保持 Chat 入口原有的 SDK 错误分类，供重试与上下文溢出策略消费。"""
    sdk = _import_litellm()
    if isinstance(error, httpx.HTTPStatusError):
        from litellm.llms.anthropic.common_utils import AnthropicError

        error = AnthropicError(status_code=error.response.status_code, message=error.response.text, headers=error.response.headers)
    return sdk.exception_type(model=model, original_exception=error, custom_llm_provider="anthropic")
