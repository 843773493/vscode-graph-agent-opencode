"""目标工作区的生成 provider 与定义校验。"""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationDefinitionDTO,
)
from app.services.business.session_generation.providers import (
    SessionGenerationProviderProtocol,
)


class GenerationProviderUnavailable(ValueError):
    """定义可以保留为 blocked；不能替换为其他 provider 或版本。"""


def resolve_generation_provider(
    providers: Mapping[str, SessionGenerationProviderProtocol],
    definition: SessionGenerationDefinitionDTO,
) -> SessionGenerationProviderProtocol:
    provider = providers.get(definition.generator_type.type_id)
    if provider is None:
        raise GenerationProviderUnavailable(f"工作区未注册会话生成器类型: {definition.generator_type.type_id}")
    if definition.generator_type.version not in provider.supported_versions:
        raise GenerationProviderUnavailable(
            "工作区不支持会话生成器版本: "
            f"type_id={provider.type_id}, version={definition.generator_type.version}"
        )
    try:
        Draft202012Validator(provider.config_schema).validate(definition.config)
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ValueError(
            "会话生成器配置不符合 provider schema: "
            f"type_id={provider.type_id}, path={location}, error={error.message}"
        ) from error
    return provider
