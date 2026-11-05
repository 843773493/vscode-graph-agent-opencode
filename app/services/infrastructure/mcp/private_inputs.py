"""工作区 MCP 私有输入的唯一文件 owner；持久配置只引用不可变版本。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_serializer,
    model_validator,
)

from app.core.atomic_fs import atomic_write_bytes, native_io_path
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.hashing import sha256_jcs
from app.schemas.internal_v2.mcp import (
    McpCandidateProvenanceDTO,
    McpCandidateVariantRefDTO,
    McpPrivateEnvRefDTO,
    McpPrivateEnvValue,
    McpPrivateHeaderRefDTO,
    McpPrivateHeaderValue,
)

PrivateReference = Annotated[McpPrivateHeaderRefDTO | McpPrivateEnvRefDTO, Field(discriminator="kind")]
_HEADER_VALUE = TypeAdapter(McpPrivateHeaderValue)
_ENV_VALUE = TypeAdapter(McpPrivateEnvValue)


def candidate_env_definition(source: McpCandidateProvenanceDTO, manifest_digest: str, variant: McpCandidateVariantRefDTO) -> str:
    return sha256_jcs({"kind": "candidate", "source": source.model_dump(mode="json"),
                       "manifest_digest": manifest_digest, "variant": variant.model_dump(mode="json")})


def stdio_env_definition(connection: Mapping[str, object]) -> str:
    return sha256_jcs({"kind": "stdio", "command": connection["command"],
                       "args": connection["args"], "cwd": connection["cwd"]})


class _PrivateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    reference: PrivateReference
    value: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def validate_value(self) -> _PrivateInput:
        adapter = _HEADER_VALUE if isinstance(self.reference, McpPrivateHeaderRefDTO) else _ENV_VALUE
        adapter.validate_python(self.value)
        return self

    @field_serializer("value")
    def private_value(self, value: SecretStr) -> str:
        # 此模型只负责私有文件序列化，公开回执使用独立的引用模型。
        return value.get_secret_value()


class McpPrivateInputStore:
    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._directory = native_io_path(workspace_root / ".boxteam" / "credentials")

    def require_binding(
        self, reference: McpPrivateHeaderRefDTO, *, server_id: str, header_name: str, endpoint: str,
    ) -> None:
        expected = (read_workspace_id(self._workspace_root), server_id, header_name, endpoint)
        actual = (reference.workspace_id, reference.server_id, reference.header_name, reference.endpoint)
        if actual != expected:
            raise ValueError("MCP 私有 header 引用不属于当前工作区、Server、header 或精确 endpoint")

    def _path(self, reference: McpPrivateHeaderRefDTO | McpPrivateEnvRefDTO) -> Path:
        directory = "mcp-headers" if isinstance(reference, McpPrivateHeaderRefDTO) else "mcp-inputs"
        return self._directory / directory / f"{reference.version}.json"

    def write(self, *, server_id: str, header_name: str, endpoint: str, value: SecretStr) -> McpPrivateHeaderRefDTO:
        reference = McpPrivateHeaderRefDTO(
            workspace_id=read_workspace_id(self._workspace_root), server_id=server_id,
            header_name=header_name, endpoint=endpoint, version=uuid4().hex,
        )
        self._publish(reference, value)
        return reference

    def write_env(self, *, server_id: str, input_key: str, definition_digest: str, value: SecretStr) -> McpPrivateEnvRefDTO:
        reference = McpPrivateEnvRefDTO(workspace_id=read_workspace_id(self._workspace_root), server_id=server_id,
            input_key=input_key, definition_digest=definition_digest, version=uuid4().hex)
        self._publish(reference, value)
        return reference

    def require_env_binding(self, reference: McpPrivateEnvRefDTO, *, server_id: str, input_key: str, definition_digest: str) -> None:
        expected = (read_workspace_id(self._workspace_root), server_id, input_key, definition_digest)
        actual = (reference.workspace_id, reference.server_id, reference.input_key, reference.definition_digest)
        if actual != expected:
            raise ValueError("MCP 私有 env 引用不属于当前工作区、Server、输入键或原来源／launch 定义")

    def _publish(self, reference: McpPrivateHeaderRefDTO | McpPrivateEnvRefDTO, value: SecretStr) -> None:
        credential = _PrivateInput(reference=reference, value=value)
        path = self._path(reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        restrict_private_path_access(self._directory, is_directory=True)
        restrict_private_path_access(path.parent, is_directory=True)
        if path.exists():
            raise FileExistsError("MCP 私有输入版本已存在，不能覆盖")
        # 仅私有文件保留原值；原子写完成后才向调用方交付引用。
        atomic_write_bytes(path, credential.model_dump_json().encode("utf-8"))
        require_private_file_access(path)

    def resolve(
        self, reference: McpPrivateHeaderRefDTO, *, server_id: str, header_name: str, endpoint: str,
    ) -> str:
        self.require_binding(reference, server_id=server_id, header_name=header_name, endpoint=endpoint)
        return self._load(reference)

    def resolve_env(self, reference: McpPrivateEnvRefDTO, *, server_id: str, input_key: str, definition_digest: str) -> str:
        self.require_env_binding(reference, server_id=server_id, input_key=input_key, definition_digest=definition_digest)
        return self._load(reference)

    def _load(self, reference: McpPrivateHeaderRefDTO | McpPrivateEnvRefDTO) -> str:
        path = self._path(reference)
        try:
            with path.open("rb") as file:
                require_private_file_access(path, descriptor=file.fileno())
                credential = _PrivateInput.model_validate_json(file.read())
        except FileNotFoundError as error:
            raise ValueError(f"MCP 私有输入版本不存在: version={reference.version}") from error
        if credential.reference != reference:
            raise ValueError("MCP 私有输入文件与精确引用身份不一致")
        return credential.value.get_secret_value()
