"""VSIX Provider 的具名控制合同；快照不包含可执行参数或秘密。"""

from __future__ import annotations

from typing import Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationViewRequest,
)
from app.schemas.gateway.extension_host import _HostValue
from app.schemas.internal_v2.mcp import require_mcp_http_url


class ExtensionMcpDefinitionChannelRequest(ExtensionConfigurationViewRequest):
    host_kind: Literal["node", "web"]


class ExtensionMcpDefinitionChannelDTO(ExtensionMcpDefinitionChannelRequest):
    client_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class McpProviderDefinitionDTO(_HostValue):
    definition_ref: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=512)
    version: str | None = Field(default=None, max_length=512)
    transport: Literal["stdio", "streamable_http"]


class McpProviderSnapshotDTO(_HostValue):
    kind: Literal["snapshot"] = "snapshot"
    extension_id: str = Field(min_length=1, max_length=256)
    extension_key: str = Field(min_length=1, max_length=512)
    collection_id: str = Field(min_length=1, max_length=512)
    provider_ref: str = Field(min_length=1, max_length=128)
    definitions: list[McpProviderDefinitionDTO]
    error_code: Literal["provide_failed"] | None = None

    @model_validator(mode="after")
    def unique_definitions(self) -> McpProviderSnapshotDTO:
        if len({item.definition_ref for item in self.definitions}) != len(self.definitions):
            raise ValueError("Provider 快照包含重复的原定义引用")
        return self


class McpProviderInvalidateDTO(_HostValue):
    kind: Literal["invalidate"] = "invalidate"
    provider_ref: str = Field(min_length=1, max_length=128)


class McpProviderStdioLaunchDTO(_HostValue):
    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str]
    env: dict[str, str]
    unset_env: list[str]
    cwd: str | None = None


class McpProviderHttpLaunchDTO(_HostValue):
    transport: Literal["streamable_http"]
    url: str = Field(min_length=1)
    headers: dict[str, str]

    @model_validator(mode="after")
    def valid_url(self) -> McpProviderHttpLaunchDTO:
        require_mcp_http_url(self.url, field="Provider HTTP endpoint")
        return self


McpProviderLaunch = Annotated[McpProviderStdioLaunchDTO | McpProviderHttpLaunchDTO, Field(discriminator="transport")]


class McpProviderResolveDTO(_HostValue):
    kind: Literal["resolve"] = "resolve"
    request_id: str = Field(min_length=1, max_length=128)
    provider_ref: str = Field(min_length=1, max_length=128)
    definition_ref: str = Field(min_length=1, max_length=128)


class McpProviderCancelDTO(_HostValue):
    kind: Literal["cancel"] = "cancel"
    request_id: str = Field(min_length=1, max_length=128)


class McpProviderResolvedDTO(_HostValue):
    kind: Literal["resolved"] = "resolved"
    request_id: str = Field(min_length=1, max_length=128)
    launch: McpProviderLaunch | None = Field(default=None, repr=False)
    error_code: Literal["resolve_undefined", "resolve_failed", "definition_invalidated", "unsupported_launch"] | None = None

    @model_validator(mode="after")
    def one_result(self) -> McpProviderResolvedDTO:
        if (self.launch is None) == (self.error_code is None):
            raise ValueError("Provider resolve 必须只返回 launch 或明确错误码")
        return self


MCP_PROVIDER_INBOUND = TypeAdapter(Annotated[McpProviderSnapshotDTO | McpProviderInvalidateDTO | McpProviderResolvedDTO,
    Field(discriminator="kind")], config=ConfigDict(hide_input_in_errors=True))
MCP_PROVIDER_OUTBOUND = TypeAdapter(Annotated[McpProviderResolveDTO | McpProviderCancelDTO, Field(discriminator="kind")],
    config=ConfigDict(hide_input_in_errors=True))
MCP_PROVIDER_CHANNEL_REQUEST = TypeAdapter(ExtensionMcpDefinitionChannelRequest)
MCP_PROVIDER_CHANNEL_BINDING = TypeAdapter(ExtensionMcpDefinitionChannelDTO)

_Frame = TypeVar("_Frame")
_FRAME_LOCATIONS = frozenset(field for model in (McpProviderSnapshotDTO, McpProviderDefinitionDTO,
    McpProviderInvalidateDTO, McpProviderResolvedDTO, McpProviderResolveDTO, McpProviderCancelDTO,
    McpProviderStdioLaunchDTO, McpProviderHttpLaunchDTO, ExtensionMcpDefinitionChannelRequest,
    ExtensionMcpDefinitionChannelDTO) for field in model.model_fields) | {
    "snapshot", "invalidate", "resolved", "resolve", "cancel", "stdio", "streamable_http"}


class McpProviderFrameError(ValueError):
    """具名 JSON 帧校验失败；只交付类别与字段位置，不持有原秘密输入。"""


def validate_provider_frame(adapter: TypeAdapter[_Frame], raw: str) -> _Frame:
    try:
        return adapter.validate_json(raw)
    except ValidationError as error:
        # hide_input 不能去掉 union discriminator 消息中的原 tag；只保留结构诊断。
        issues = "; ".join(f"{item['type']} loc={tuple(part if isinstance(part, int) or part in _FRAME_LOCATIONS else '<input-key>' for part in item['loc'])}"
            for item in error.errors(include_input=False, include_context=False, include_url=False))
        raise McpProviderFrameError(f"MCP Provider 帧校验失败: error_type=ValidationError {issues}") from None
