"""原配置 owner 的一次性模型发现；草稿不会进入保存、候选或正式 runtime。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import anyio
import jsonschema
from boxteam_plugin_sdk.failures import failure_message

from app.core.workspace_identity import read_workspace_id
from app.runtime.provider_model import (
    build_model_from_provider,
    validate_provider_configuration,
)
from app.schemas.internal_v2.llm_probe import (
    LlmCredentialObservationDTO,
    LlmDiscoverRequest,
    LlmDiscoverResultDTO,
    LlmProbeErrorDTO,
    LlmProbeObservationDTO,
    LlmTestDeltaDTO,
    LlmTestRequest,
    LlmTestResultDTO,
    LlmTestStartDTO,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config.llm_catalog import (
    probe_secret_values,
    public_endpoint,
    read_model_catalog,
)
from app.services.infrastructure.config.source_document import (
    parse_source_text,
    restore_source_text,
)
from app.services.infrastructure.config.state import ConfigConflictError
from configs.runtime import merge_json_objects, require_current_workspace_config

if TYPE_CHECKING:
    from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
    from app.services.infrastructure.config_service import ConfigService


@dataclass(frozen=True)
class LlmTestInvocation:
    model: BoxteamLiteLLMChatModel = field(repr=False)
    observation: LlmProbeObservationDTO
    model_ref: ModelReferenceDTO
    streaming: bool
    secrets: tuple[str, ...] = field(repr=False)


def _public_test_text(text: str, secrets: tuple[str, ...], *, final: bool) -> tuple[str, str]:
    """隐藏完整秘密，保留可能跨 chunk 的秘密前缀，正常结束才释放未匹配正文。"""
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    retained = 0
    if not final:
        for secret in secrets:
            if len(secret) <= 1:
                continue
            # KMP 前缀表在线性时间找出末尾未完成匹配；不会因超长 Key 产生平方扫描。
            sequence: list[str | None] = [*secret, None, *text[-(len(secret) - 1):]]
            prefix = [0] * len(sequence)
            for index in range(1, len(sequence)):
                matched = prefix[index - 1]
                while matched and sequence[index] != sequence[matched]:
                    matched = prefix[matched - 1]
                if sequence[index] == sequence[matched]:
                    matched += 1
                prefix[index] = matched
            retained = max(retained, prefix[-1])
    return (text[:-retained], text[-retained:]) if retained else (text, "")


class ConfigLlmProbeMixin:
    async def _prepare_llm_probe(
        self, request: LlmDiscoverRequest, *, gateway_id: str, deadline: float,
        model_id: str | None = None, streaming: bool | None = None,
    ) -> tuple[dict[str, Any], LlmProbeObservationDTO]:
        service = cast("ConfigService", self)
        async with asyncio.timeout_at(deadline):
            _, root, store = service._source_target(request.source_key or "inline")
            if request.gateway_id != gateway_id or request.workspace_id != read_workspace_id(root):
                raise ConfigConflictError("模型探测目标与请求的 Gateway／工作区不一致")
            baseline = {}
            active_revision = None
            active_digest = None
            with service.publication_lock:
                if request.mode == "active":
                    active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
                    if active is None or (active.active_revision, active.effective_digest) != (
                        request.expected_active_revision, request.expected_active_digest,
                    ):
                        raise ConfigConflictError("active 版本已改变，请重新读取原目标后显式重试")
                    snapshot = service._require_snapshot()
                    if snapshot.revision != active.effective_digest:
                        raise ConfigConflictError("active 发布正在交接，请重新读取原目标")
                    config = snapshot.to_dict()
                    active_revision, active_digest = active.active_revision, active.effective_digest
                else:
                    _, layers, files, baseline = service._read_saved_source_view()
                    if request.expected_source_baseline != baseline:
                        raise ConfigConflictError("参与来源的文件或版本已改变，请重新读取原来源后显式重试")
                    assert request.source_key is not None and request.jsonc is not None
                    if request.source_key not in files:
                        raise ValueError("草稿探测必须选择实际文件来源")
                    file = files[request.source_key]
                    original = file.raw_bytes.decode("utf-8") if file.raw_bytes is not None else "{}\n"
                    payload = parse_source_text(restore_source_text(request.jsonc, original))
                    require_current_workspace_config(payload, partial=True)
                    layers[request.source_key] = payload
                    config = {}
                    for layer in layers.values():
                        config = merge_json_objects(config, layer, schema=service._load_schema())
            connection = (service._llm_resolution.resolve_provider_defaults(config, request.provider_id)
                if model_id is None else service._llm_resolution.resolve_model_reference(config, ModelReferenceDTO(
                    provider_id=request.provider_id, model_id=model_id,
                )))
            if streaming is not None:
                connection["api_mode"] = {**connection["api_mode"], "streaming": streaming}
            # 仍使用原 schema；只校验当前连接，不要求 Model、Agent 或工具目录完整。
            schema = service._load_schema()
            option_fields = schema["$defs"]["modelOptions"]["properties"]
            options = {key: value for key, value in connection.items() if key in option_fields}
            try:
                jsonschema.Draft202012Validator({
                    "$ref": "#/$defs/resolvedModelOptions", "$defs": schema["$defs"],
                }).validate(options)
            except jsonschema.ValidationError as error:
                raise ValueError(f"当前 Provider 连接配置无效: field={'/'.join(str(part) for part in error.absolute_path)}, rule={error.validator}") from error
            temporary = request.credential_input
            if model_id is None and connection.get("auth") is not None:
                raise ValueError("当前 OAuth 服务未提供模型目录合同，请手工添加模型；此结果不代表模型不可调用")
            if model_id is not None:
                # ChatGPT 的非流式拒绝也在读取凭据之前，与正式构造共用规则。
                validate_provider_configuration(connection)
            if temporary is not None:
                if temporary.provider_id != request.provider_id or (temporary.scope == "model" and temporary.model_id != model_id):
                    raise ValueError("临时凭据的 Provider／Model 归属与本次探测不一致")
                if connection.get("auth") is not None:
                    raise ValueError("OAuth 连接不能使用临时 API Key")
                if temporary.scope == "provider" and model_id is not None:
                    overrides = config["llm"]["providers"][request.provider_id]["models"][model_id].get("overrides", {})
                    if "api_key" in overrides:
                        raise ValueError("临时 Provider 凭据被 Model overrides.api_key 覆盖；请对实际 Model 凭据字段提供输入")
                # API Key 是原子字段，在解析引用前替换；旧引用不会被 resolve。
                connection["api_key"] = temporary.value.get_secret_value()
                connection["credential_source"] = "temporary"
                credential = LlmCredentialObservationDTO(source="temporary", scope=temporary.scope)
            else:
                credentials = await service.prepare_llm_credentials([connection])
                source = connection.get("api_key", "")
                with service.use_llm_credentials(credentials):
                    connection = service._llm_resolution.expand_model(connection)
                if source.startswith("secret:"):
                    secret_id = source.removeprefix("secret:")
                    credential = LlmCredentialObservationDTO(source="vault", secret_id=secret_id,
                        revision=credentials[secret_id].revision)
                elif source.startswith("${"):
                    credential = LlmCredentialObservationDTO(source="environment")
                else:
                    credential = LlmCredentialObservationDTO(source="oauth" if connection.get("auth") else "none")
            observation = LlmProbeObservationDTO(
                gateway_id=gateway_id, workspace_id=request.workspace_id, mode=request.mode,
                provider_id=request.provider_id, edit_version=request.edit_version, source_key=request.source_key,
                source_baseline=baseline, active_revision=active_revision, active_digest=active_digest,
                credential=credential, protocol=connection["api_mode"]["protocol"],
                endpoint=public_endpoint(connection["endpoint"]), observed_at=datetime.now(UTC).isoformat(),
            )
        return connection, observation

    async def discover_llm_models(self, request: LlmDiscoverRequest, *, gateway_id: str) -> LlmDiscoverResultDTO:
        deadline = asyncio.get_running_loop().time() + 15
        connection, observation = await self._prepare_llm_probe(request, gateway_id=gateway_id, deadline=deadline)
        models, complete, error = await read_model_catalog(connection, deadline=deadline)
        return LlmDiscoverResultDTO(observation=observation, models=models, complete=complete, error=error)

    async def prepare_llm_test(self, request: LlmTestRequest, *, gateway_id: str, deadline: float) -> LlmTestInvocation:
        service = cast("ConfigService", self)
        if request.timeout_seconds > service.get_agent_run_timeout_seconds():
            raise ValueError("模型测试期限不能超过当前执行期限")
        connection, observation = await self._prepare_llm_probe(
            request, gateway_id=gateway_id, deadline=deadline, model_id=request.model_id, streaming=request.streaming,
        )
        async with asyncio.timeout_at(deadline):
            model = await asyncio.to_thread(build_model_from_provider, connection, {},
                invocation_purpose="model_test", gateway_id=gateway_id)
        return LlmTestInvocation(
            model=model, observation=observation,
            model_ref=ModelReferenceDTO(provider_id=request.provider_id, model_id=request.model_id),
            streaming=connection["api_mode"].get("streaming", True),
            secrets=probe_secret_values(connection),
        )

    async def stream_llm_test(
        self, invocation: LlmTestInvocation, *, request_id: str, started_at: float, deadline: float,
    ) -> AsyncIterator[LlmTestStartDTO | LlmTestDeltaDTO | LlmTestResultDTO]:
        """只消费原 adapter；每次等待共用同一绝对期限，不跨 yield 持有超时上下文。"""
        loop = asyncio.get_running_loop()
        identity = {"request_id": request_id, "observation": invocation.observation,
                    "model_ref": invocation.model_ref, "streaming": invocation.streaming}
        yield LlmTestStartDTO(**identity)
        iterator = invocation.model.astream("Reply with a short greeting.")
        preview = ""
        preview_bytes = 0
        preview_truncated = False
        pending = ""
        first_content_ms = None
        usage: dict[str, object] | None = None
        error = None
        try:
            while True:
                async with asyncio.timeout_at(deadline):
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        chunk = None
                if chunk is not None and chunk.usage_metadata is not None:
                    if usage is None:
                        usage = {}
                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                        if key in chunk.usage_metadata:
                            usage[key] = int(usage.get(key, 0)) + chunk.usage_metadata[key]
                raw_text = chunk.text if chunk is not None else ""
                if raw_text and first_content_ms is None:
                    first_content_ms = (loop.time() - started_at) * 1000
                text, pending = _public_test_text(pending + raw_text, invocation.secrets, final=chunk is None)
                encoded = text.encode("utf-8")
                remaining = 64 * 1024 - preview_bytes
                preview_truncated |= len(encoded) > remaining
                visible = encoded[:remaining].decode("utf-8", errors="ignore")
                if visible:
                    preview += visible
                    preview_bytes += len(visible.encode("utf-8"))
                    yield LlmTestDeltaDTO(request_id=request_id, text=visible)
                if chunk is None:
                    break
            if first_content_ms is None:
                error = LlmProbeErrorDTO(category="protocol", message="模型未返回有效文本；响应头、心跳或空正文不表示测试通过")
        except Exception as failure:  # noqa: BLE001 - 发头后的 SDK 异常必须转为一次明确失败终态
            status_code = getattr(failure, "status_code", None)
            if not isinstance(status_code, int):
                status_code = None
            category = "timeout" if isinstance(failure, TimeoutError) else {
                401: "authentication", 403: "authentication", 404: "model_or_path", 429: "rate_limit",
            }.get(status_code, "upstream")
            error = LlmProbeErrorDTO(category=category, message=failure_message(
                failure, secrets=invocation.secrets, max_bytes=4096,
            ), status_code=status_code)
        finally:
            # Starlette 用 AnyIO scope 连续取消响应；关闭自己持有的连接必须完成。
            with anyio.CancelScope(shield=True):
                try:
                    await iterator.aclose()
                finally:
                    await invocation.model.aclose()
        yield LlmTestResultDTO(**identity, status="passed" if error is None else "failed",
            first_content_ms=first_content_ms, elapsed_ms=(loop.time() - started_at) * 1000,
            preview=preview, preview_truncated=preview_truncated, usage=usage, error=error)
