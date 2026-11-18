"""私有 reasoning 的完整来源事实；不记录秘密或密钥修订号。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields

MODEL_SOURCE_EXTENSION = "boxteam.model-source"


@dataclass(frozen=True, slots=True)
class ModelSource:
    gateway_id: str
    workspace_id: str
    provider_id: str
    model_id: str
    protocol: str
    endpoint: str
    model: str
    authentication: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> ModelSource | None:
        """历史没有完整来源时保持未知，禁止用旧 Provider ID 补齐。"""
        names = {field.name for field in fields(cls)}
        if not isinstance(value, Mapping) or value.keys() != names:
            return None
        if not all(isinstance(child, str) and child for child in value.values()):
            return None
        return cls(**value)

    @classmethod
    def from_extensions(cls, extensions: Mapping[str, object]) -> ModelSource | None:
        envelope = extensions.get(MODEL_SOURCE_EXTENSION)
        if not isinstance(envelope, Mapping) or envelope.get("extension_version") != "1":
            return None
        return cls.from_dict(envelope.get("value"))
