from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AddRemoteGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, description="远程 Gateway 显示名称")
    connection_workspace_id: str | None = Field(
        default=None,
        description="复用已连接远程 Gateway 的 SSH 主机和凭据",
    )
    ssh_config_host: str | None = Field(
        default=None,
        description="复用用户 ~/.ssh/config 中的 Host 别名",
    )
    host: str | None = Field(default=None, description="手动连接的 SSH 主机")
    port: int = Field(default=22, ge=1, le=65535, description="手动连接的 SSH 端口")
    username: str | None = Field(default=None, description="手动连接的 SSH 用户名")
    private_key_path: str | None = Field(
        default=None,
        description="手动连接使用的 SSH 私钥路径",
    )
    remote_gateway_port: int = Field(
        default=8014,
        ge=1,
        le=65535,
        description="远端 Gateway loopback 端口",
    )

    @model_validator(mode="after")
    def validate_connection_source(self) -> AddRemoteGatewayRequest:
        explicit_values = (self.host, self.username, self.private_key_path)
        has_explicit_source = any(bool(value) for value in explicit_values)
        connection_source_count = sum(
            bool(value)
            for value in (
                self.connection_workspace_id,
                self.ssh_config_host,
                has_explicit_source,
            )
        )
        if connection_source_count != 1:
            raise ValueError(
                "必须且只能选择一个已注册 SSH 连接、~/.ssh/config Host 或手动 SSH 连接"
            )
        if has_explicit_source and not all(bool(value) for value in explicit_values):
            raise ValueError("手动 SSH 连接必须提供主机、用户名和私钥路径")
        return self


class SshConnectionOptionDTO(BaseModel):
    connection_id: str
    source: Literal["boxteam", "ssh_config"]
    label: str
    host: str
    port: int
    username: str
    workspace_id: str | None = None
    ssh_config_host: str | None = None
    initial_path: str | None = None


class SshConnectionOptionListDTO(BaseModel):
    items: list[SshConnectionOptionDTO] = Field(default_factory=list)
