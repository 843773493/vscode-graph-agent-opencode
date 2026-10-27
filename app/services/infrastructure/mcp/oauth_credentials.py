"""工作区所属的 SDK OAuth 凭据；与普通业务状态、会话正文分开。"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.atomic_fs import atomic_write_bytes, native_io_path
from app.core.file_permissions import (
    require_private_file_access,
    restrict_private_path_access,
)
from app.domain.itemized.hashing import sha256_jcs
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: int = 1
    tokens: OAuthToken | None = Field(default=None, repr=False)
    expires_at: float | None = None
    client_info: OAuthClientInformationFull | None = Field(default=None, repr=False)
    resource_metadata: ProtectedResourceMetadata | None = None
    authorization_metadata: OAuthMetadata | None = None


class McpOAuthCredentialStore:
    """实现官方 TokenStorage；同名 Server 换 endpoint 或回调时不借用旧凭据。"""

    def __init__(self, *, workspace_root: Path, server_id: str, server_url: str, redirect_uri: str) -> None:
        identity = sha256_jcs([server_id, server_url, redirect_uri]).rsplit(":", 1)[-1]
        self._path = native_io_path(workspace_root / ".boxteam" / "credentials" / "mcp-oauth" / f"{identity}.json")

    def _load(self) -> _Credentials:
        try:
            with self._path.open("rb") as file:
                require_private_file_access(self._path, descriptor=file.fileno())
                result = _Credentials.model_validate_json(file.read())
        except FileNotFoundError:
            return _Credentials()
        if result.schema_version != 1:
            raise ValueError("MCP OAuth 凭据版本不受支持")
        return result

    def _save(self, value: _Credentials) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        restrict_private_path_access(self._path.parent, is_directory=True)
        # 原子文件原语在私有目录创建临时文件，继承 Windows ACL；POSIX 为0600。
        atomic_write_bytes(self._path, value.model_dump_json().encode("utf-8"))
        require_private_file_access(self._path)

    async def get_tokens(self) -> OAuthToken | None:
        value = self._load()
        if value.tokens is None or value.expires_at is None:
            return value.tokens
        # SDK 的 expires_in 是相对期限；冷启动不能重新发放原有效期。
        return value.tokens.model_copy(update={"expires_in": max(0, int(value.expires_at - time.time()))})

    async def set_tokens(self, tokens: OAuthToken) -> None:
        value = self._load()
        value.tokens = tokens
        value.expires_at = None if tokens.expires_in is None else time.time() + tokens.expires_in
        self._save(value)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._load().client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        value = self._load()
        value.client_info = client_info
        self._save(value)

    def save_metadata(self, resource: ProtectedResourceMetadata, authorization: OAuthMetadata) -> None:
        value = self._load()
        value.resource_metadata, value.authorization_metadata = resource, authorization
        self._save(value)

    def load_metadata(self) -> tuple[ProtectedResourceMetadata | None, OAuthMetadata | None]:
        value = self._load()
        return value.resource_metadata, value.authorization_metadata
