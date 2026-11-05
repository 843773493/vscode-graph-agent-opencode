"""工作区私有 HTTP header；持久配置只引用不可变版本。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

from app.core.atomic_fs import atomic_write_bytes, native_io_path
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.mcp import McpPrivateHeaderRefDTO, McpPrivateHeaderValue


class _PrivateHeader(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    reference: McpPrivateHeaderRefDTO
    value: McpPrivateHeaderValue = Field(repr=False)

    @field_serializer("value")
    def private_value(self, value: SecretStr) -> str:
        # 此模型只负责私有文件序列化，公开回执使用独立的引用模型。
        return value.get_secret_value()


class McpPrivateHeaderStore:
    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._directory = native_io_path(workspace_root / ".boxteam" / "credentials" / "mcp-headers")

    def require_binding(
        self, reference: McpPrivateHeaderRefDTO, *, server_id: str, header_name: str, endpoint: str,
    ) -> None:
        expected = (read_workspace_id(self._workspace_root), server_id, header_name, endpoint)
        actual = (reference.workspace_id, reference.server_id, reference.header_name, reference.endpoint)
        if actual != expected:
            raise ValueError("MCP 私有 header 引用不属于当前工作区、Server、header 或精确 endpoint")

    def _path(self, reference: McpPrivateHeaderRefDTO) -> Path:
        return self._directory / f"{reference.version}.json"

    def write(self, *, server_id: str, header_name: str, endpoint: str, value: SecretStr) -> McpPrivateHeaderRefDTO:
        reference = McpPrivateHeaderRefDTO(
            workspace_id=read_workspace_id(self._workspace_root), server_id=server_id,
            header_name=header_name, endpoint=endpoint, version=uuid4().hex,
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        restrict_private_path_access(self._directory.parent, is_directory=True)
        restrict_private_path_access(self._directory, is_directory=True)
        path = self._path(reference)
        if path.exists():
            raise FileExistsError("MCP 私有 header 版本已存在，不能覆盖")
        # 仅私有文件保留原值；原子写完成后才向调用方交付引用。
        credential = _PrivateHeader(reference=reference, value=value)
        atomic_write_bytes(path, credential.model_dump_json().encode("utf-8"))
        require_private_file_access(path)
        return reference

    def resolve(
        self, reference: McpPrivateHeaderRefDTO, *, server_id: str, header_name: str, endpoint: str,
    ) -> str:
        self.require_binding(reference, server_id=server_id, header_name=header_name, endpoint=endpoint)
        path = self._path(reference)
        try:
            with path.open("rb") as file:
                require_private_file_access(path, descriptor=file.fileno())
                credential = _PrivateHeader.model_validate_json(file.read())
        except FileNotFoundError as error:
            raise ValueError(f"MCP 私有 header 版本不存在: version={reference.version}") from error
        if credential.reference != reference:
            raise ValueError("MCP 私有 header 文件与精确引用身份不一致")
        return credential.value.get_secret_value()
