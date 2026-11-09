from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
)

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId


class McpInvocationReceipt(BaseModel):
    """本地等待结束不代表远端副作用已停止；不保存异常自由正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["error"] = "error"
    code: Literal["mcp_outcome_unknown"] = "mcp_outcome_unknown"
    effect: Literal["unknown"] = "unknown"
    retryable: Literal[False] = False
    tool_name: str
    error_type: str
    cancellation_delivery: Literal["not_requested", "sent", "failed"] = "not_requested"
    recovery: Literal["verify_remote_state_before_new_action"] = "verify_remote_state_before_new_action"

    def as_artifact(self) -> dict[str, object]:
        return {"mcp_outcome": self.model_dump()}

    @classmethod
    def from_artifact(cls, artifact: object) -> McpInvocationReceipt | None:
        if not isinstance(artifact, dict) or "mcp_outcome" not in artifact:
            return None
        return cls.model_validate(artifact["mcp_outcome"])


class McpToolDTO(BaseModel):
    tool_id: str
    server_id: str
    remote_name: str
    description: str


class McpServiceReferenceDTO(BaseModel):
    """MCP 配置中的稳定插件服务定位，不包含运行实例或网络地址。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gateway_id: CanonicalGatewayId
    workspace_id: CanonicalWorkspaceId
    package_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)


class McpServerDTO(BaseModel):
    server_id: str
    transport: Literal["streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready", "failed", "authorization_required", "unavailable"]
    tools: list[McpToolDTO] = Field(default_factory=list)
    connection_ref: str | None = Field(default=None, pattern=r"^mcp-connection:v1:[0-9a-f]{32}:[1-9][0-9]*$")
    service: McpServiceReferenceDTO | None = None
    unavailable_reason: str | None = None


class McpConnectionOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    connection_id: str
    server_id: str
    action: Literal["connect", "disconnect"]
    status: Literal["pending", "waiting_user", "verifying", "succeeded", "failed", "expired", "cancelled"]
    stage: Literal["queued", "authorization", "initialize", "catalog", "disconnect", "complete", "cleanup", "reconcile"]
    created_at: float
    deadline: float
    updated_at: float
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "expired", "cancelled"}


class McpConnectionRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_:-]+$")
    server_id: str = Field(min_length=1, max_length=64)
    action: Literal["connect", "disconnect"]


class McpAuthorizationDTO(BaseModel):
    """仅供已认证用户读取；不得用于普通状态、工具结果或会话正文。"""

    authorization_url: str = Field(repr=False)


class McpAuthorizationCallbackDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: str = Field(min_length=1, max_length=8192, repr=False)
    state: str = Field(min_length=1, max_length=1024, repr=False)


class McpWorkspaceTargetDTO(BaseModel):
    """原生 Backend 可直接读取；经 Gateway 使用时必须核对完整 owner。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["workspace"] = "workspace"
    gateway_id: CanonicalGatewayId | None
    workspace_id: CanonicalWorkspaceId


class _McpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: int = 0
    message: str = "ok"
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: Literal[1] = 1


def require_mcp_http_url(url: str, *, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} 必须是有效 HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"{field} 仅允许 loopback 地址使用 HTTP")


def _private_header_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.username is not None or parsed.password is not None or "#" in endpoint:
        raise ValueError("MCP 私有 header 的公开 endpoint 不得包含凭据或 fragment")
    if endpoint != endpoint.strip():
        raise ValueError("MCP 私有 header endpoint 不得包含首尾空白")
    require_mcp_http_url(endpoint, field="MCP 私有 header endpoint")
    return endpoint


McpPrivateHeaderEndpoint = Annotated[str, Field(min_length=1), AfterValidator(_private_header_endpoint)]


class McpPrivateHeaderRefDTO(BaseModel):
    """可公开往返的身份；不包含秘密值，也不接受路径或秘密 URI。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["mcp_private_header"] = "mcp_private_header"
    workspace_id: CanonicalWorkspaceId
    server_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
    header_name: str = Field(min_length=1, max_length=256, pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
    endpoint: McpPrivateHeaderEndpoint
    version: str = Field(pattern=r"^[0-9a-f]{32}$")


def _private_header_value(value: SecretStr) -> SecretStr:
    text = value.get_secret_value()
    if not text.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("私有 header 值必须是无控制字符的 ASCII 文本")
    if text != text.strip():
        raise ValueError("私有 header 值不得包含首尾空格")
    return value


McpPrivateHeaderValue = Annotated[
    SecretStr, Field(min_length=1, max_length=8192), AfterValidator(_private_header_value),
]


class McpPrivateHeaderWriteRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    target: McpWorkspaceTargetDTO
    server_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
    header_name: str = Field(min_length=1, max_length=256, pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
    endpoint: McpPrivateHeaderEndpoint
    value: McpPrivateHeaderValue = Field(repr=False)


class McpPrivateHeaderWriteResponseDTO(_McpResponse):
    data: McpPrivateHeaderRefDTO


class McpServerListResponseDTO(_McpResponse):
    data: list[McpServerDTO]
    catalog_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    catalog_generation: int = Field(ge=1)


class McpConnectionOperationListResponseDTO(_McpResponse):
    data: list[McpConnectionOperationDTO]


class McpConnectionOperationResponseDTO(_McpResponse):
    data: McpConnectionOperationDTO


class McpAuthorizationResponseDTO(_McpResponse):
    data: McpAuthorizationDTO
