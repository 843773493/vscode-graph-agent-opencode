"""目标工作区的生成 provider 与定义校验。"""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.core.failures import failure_detail
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
        detail = failure_detail(
            error, stage="session-generation-config",
            label=f"会话生成器配置不符合 provider schema: type_id={provider.type_id}",
        )
        path = list(error.absolute_path)
        # 动态键与输入正文留在原始诊断，公开位置只保留顶层参数名及深度。
        parameter = path[0] if path and isinstance(path[0], str) and path[0].isidentifier() else None
        raise ValueError(
            f"{detail}, parameter={parameter}, depth={len(path)}, rule={error.validator}"
        ) from error
    return provider
