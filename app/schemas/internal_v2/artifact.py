from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri


class ArtifactDTO(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    artifact_id: str
    job_id: str
    type: str
    name: str
    path: str
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def require_resource_uri(cls, value: str) -> str:
        """产物地址沿现有资源合同校验，未实现的存储不获得原生路径输出许可。"""
        try:
            parse_resource_uri(value)
        except ResourceUriGrammarError as error:
            raise ValueError(f"产物引用必须是资源 URI: code={error.reason_code}") from error
        return value
