"""命名密钥的管理元数据；普通响应永不携带密钥值。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

SecretId = Annotated[str, StringConstraints(pattern=r"^sec_[0-9a-f]{32}$")]


class SecretMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: SecretId
    name: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class GatewaySecretsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    gateway_id: str
    items: list[SecretMetadataDTO]


class GatewaySecretDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    gateway_id: str
    item: SecretMetadataDTO


class CreateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: SecretId
    name: str = Field(min_length=1, max_length=128, pattern=r"\S")
    value: SecretStr = Field(min_length=1, max_length=65536)


class UpdateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128, pattern=r"\S")
    value: SecretStr | None = Field(default=None, min_length=1, max_length=65536)


class ResolveSecretsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[SecretId] = Field(min_length=1, max_length=256)


class ResolvedSecret(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    revision: int = Field(ge=1)
    value: SecretStr
