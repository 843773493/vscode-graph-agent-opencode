from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId
from app.schemas.internal_v2.config import (
    ConfigSourceDocumentDTO,
    ConfigSourceEditRequest,
)


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


class McpServerDTO(BaseModel):
    server_id: str
    transport: Literal["stdio", "streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready", "failed", "authorization_required"]
    tools: list[McpToolDTO] = Field(default_factory=list)
    connection_ref: str | None = Field(default=None, pattern=r"^mcp-connection:v1:[0-9a-f]{32}:[1-9][0-9]*$")


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


class _McpCandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class McpInstallationTargetDTO(_McpCandidateModel):
    """安装域来自原生 Backend 身份，不借用工作区或进程代际。"""

    kind: Literal["backend"] = "backend"
    gateway_id: CanonicalGatewayId
    backend_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class McpCandidateSourceDTO(_McpCandidateModel):
    kind: Literal["gallery", "manual"]
    name: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    endpoint: str | None = None
    protocol: Literal["v0.1", "v0"] | None = None
    entry_url: str | None = None
    entry_id: str | None = None
    definition: dict[str, object] | None = Field(default=None, repr=False,
        description="手动来源的 packages/remotes manifest，或原 stdio/streamable_http 配置；不接受秘密输入。")

    @model_validator(mode="before")
    @classmethod
    def default_gallery_protocol(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("kind") == "gallery" and "protocol" not in value:
            return {**value, "protocol": "v0.1"}
        return value

    @model_validator(mode="after")
    def validate_source(self) -> McpCandidateSourceDTO:
        if self.kind == "gallery":
            if not self.endpoint or not self.entry_url or self.protocol is None or self.definition is not None:
                raise ValueError("目录来源必须指定 endpoint、协议和精确 entry_url，不能附带手动定义")
        elif self.definition is None or any(value is not None for value in (self.endpoint, self.protocol, self.entry_url, self.entry_id)):
            raise ValueError("手动来源必须只携带明确 definition，不得借用目录身份")
        return self


class McpCandidateProvenanceDTO(_McpCandidateModel):
    kind: Literal["gallery", "manual"]
    name: str
    version: str
    endpoint: str | None = None
    protocol: Literal["v0.1", "v0"] | None = None
    entry_url: str | None = None
    entry_id: str | None = None


class McpTargetPlatformDTO(_McpCandidateModel):
    operating_system: str
    architecture: str


class McpCandidateVariantRefDTO(_McpCandidateModel):
    kind: Literal["package", "remote", "manual"]
    index: int = Field(ge=0)
    digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")


class McpCandidateVariantDTO(_McpCandidateModel):
    ref: McpCandidateVariantRefDTO
    transport: str
    registry_type: str | None = None
    identifier: str | None = None
    version: str | None = None


class McpCandidateInspectRequestDTO(_McpCandidateModel):
    target: McpInstallationTargetDTO
    source: McpCandidateSourceDTO


class McpCandidateInspectDTO(_McpCandidateModel):
    source: McpCandidateProvenanceDTO
    manifest_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    platform: McpTargetPlatformDTO
    variants: list[McpCandidateVariantDTO]


class McpCandidatePlanRequestDTO(McpCandidateInspectRequestDTO):
    manifest_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    variant: McpCandidateVariantRefDTO
    inputs: dict[str, object] = Field(default_factory=dict, repr=False)
    server_id: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")


class McpCandidatePlanDTO(_McpCandidateModel):
    source: McpCandidateProvenanceDTO
    manifest_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    variant: McpCandidateVariantRefDTO
    platform: McpTargetPlatformDTO
    plan_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    disposition: Literal["requires_artifact", "configuration_only", "explicit_command"]
    download_required: bool
    parameters: dict[str, object] = Field(description="冻结参数的脱敏展示；实际消费须由原 owner 重新验证原请求，不能保存此投影作 launch 配置。")


class McpCandidatePlanValidationRequestDTO(_McpCandidateModel):
    candidate: McpCandidatePlanRequestDTO
    plan_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")


class _McpCandidateResponse(_McpCandidateModel):
    code: int = 0
    message: str = "ok"
    request_id: str
    target: McpInstallationTargetDTO
    protocol_version: Literal[1] = 1


class McpCandidateInspectResponseDTO(_McpCandidateResponse):
    data: McpCandidateInspectDTO


class McpCandidatePlanResponseDTO(_McpCandidateResponse):
    data: McpCandidatePlanDTO


class McpManagedLaunchDTO(_McpCandidateModel):
    """工作区只引用已发布制品；可执行路径由原生安装 owner 解析。"""

    backend_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    artifact_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")


class McpStdioLaunchDefinitionDTO(_McpCandidateModel):
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = Field(default=None, min_length=1)


class McpPrivateEnvRefDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["mcp_private_env"] = "mcp_private_env"
    workspace_id: CanonicalWorkspaceId
    server_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
    input_key: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    definition_digest: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    version: str = Field(pattern=r"^[0-9a-f]{32}$")


def _private_env_value(value: SecretStr) -> SecretStr:
    if any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in value.get_secret_value()):
        raise ValueError("MCP 私有 env 值必须可编码为 UTF-8 且不得包含 NUL")
    return value


McpPrivateEnvValue = Annotated[SecretStr, Field(min_length=1), AfterValidator(_private_env_value)]


class McpPrivateEnvWriteRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    target: McpWorkspaceTargetDTO
    server_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
    input_key: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    candidate: McpCandidatePlanRequestDTO | None = None
    launch: McpStdioLaunchDefinitionDTO | None = None
    value: McpPrivateEnvValue = Field(repr=False)

    @model_validator(mode="after")
    def require_definition(self) -> McpPrivateEnvWriteRequestDTO:
        if (self.candidate is None) == (self.launch is None):
            raise ValueError("MCP 私有 env 必须明确选择原 candidate 或手动 stdio launch")
        return self


class McpPrivateEnvWriteResponseDTO(_McpResponse):
    data: McpPrivateEnvRefDTO


class McpCandidateConfigurationRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    target: McpWorkspaceTargetDTO
    server_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
    plan: McpCandidatePlanValidationRequestDTO
    managed_launch: McpManagedLaunchDTO | None = None
    source_edit: ConfigSourceEditRequest


class McpCandidateConfigurationResponseDTO(_McpResponse):
    data: ConfigSourceDocumentDTO


class McpInstallationRequestDTO(McpCandidatePlanValidationRequestDTO):
    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_:-]+$")
    entrypoint: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class McpInstalledArtifactDTO(_McpCandidateModel):
    binding: McpManagedLaunchDTO
    source: McpCandidateProvenanceDTO
    manifest_digest: str
    variant: McpCandidateVariantRefDTO
    platform: McpTargetPlatformDTO
    registry_type: Literal["pypi"] = "pypi"
    identifier: str
    version: str
    entrypoint: str
    command: str
    lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    installed_distributions: dict[str, str]


class McpInstallationOperationDTO(_McpCandidateModel):
    operation_id: str
    intent_digest: str
    status: Literal["running", "succeeded", "failed", "interrupted"]
    stage: Literal["prepare", "lock", "install", "verify", "publish", "complete"]
    created_at: float
    updated_at: float
    artifact: McpInstalledArtifactDTO | None = None
    error: str | None = None
    log: str = Field(default="", max_length=65536)


class McpInstallationOperationResponseDTO(_McpCandidateResponse):
    data: McpInstallationOperationDTO


class McpInstalledArtifactListResponseDTO(_McpCandidateResponse):
    data: list[McpInstalledArtifactDTO]


class McpInstallationCapabilitiesDTO(_McpCandidateModel):
    platform: McpTargetPlatformDTO
    pypi_wheel_available: bool
    unavailable_reason: str | None = None
    unsupported_registry_types: list[str] = Field(default_factory=lambda: ["npm", "oci", "nuget"])
    limitations: list[str] = Field(default_factory=lambda: [
        "仅使用目标已有 uv／Python，不自动安装系统 runtime",
        "只安装 wheel，不运行 sdist 构建或自定义 runtimeArguments",
        "入口须显式选择并验证为根 distribution 的 console entry point",
        "依赖使用 uv.lock 校验，暂不支持目录单文件 fileSha256",
        "安装不写入工作区配置、不等于连接 ready；物理卸载尚未接入",
    ])


class McpInstallationCapabilitiesResponseDTO(_McpCandidateResponse):
    data: McpInstallationCapabilitiesDTO
