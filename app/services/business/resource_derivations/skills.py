"""Skill 的纯业务派生；来源接受、发布和激活分别由既有 owner 负责。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal

from app.services.business.resource_derivations.skill_frontmatter import (
    SkillFrontmatterError,
    parse_skill_frontmatter,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    SemanticInput,
    SemanticPayload,
)


@dataclass(frozen=True, slots=True)
class SkillDerivationEntry:
    """来源 owner 冻结的业务身份；不包含私有 locator 或正文。"""

    name: str
    layer: str
    entry_identity: str
    source_id: str
    display_uri: str
    priority: int = 0

    def resource_id(self, facet: str) -> str:
        return f"{self.entry_identity}:{facet}"


@dataclass(frozen=True, slots=True)
class SkillFacetLoader:
    """同一 accepted raw revision 分别派生 metadata 和精确正文。"""

    version: ClassVar[str] = "skill-frontmatter:v1"
    entry: SkillDerivationEntry
    facet: Literal["metadata", "activation"]

    def __call__(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload:
        content = inputs[self.entry.source_id].content
        if not isinstance(content, str):
            raise TypeError("Skill accepted 来源正文必须是字符串")
        try:
            parsed = parse_skill_frontmatter(content)
        except SkillFrontmatterError as error:
            logging.getLogger(__name__).error(
                "Skill frontmatter 解析失败: uri=%s error_code=%s error=%s",
                self.entry.display_uri,
                error.error_code,
                error,
            )
            return SemanticPayload(
                available=False, error=f"Skill frontmatter 无效: {error.error_code}"
            )
        if parsed.name != self.entry.name:
            logging.getLogger(__name__).error(
                "Skill name 与目录名不一致: uri=%s name=%r entry=%r",
                self.entry.display_uri,
                parsed.name,
                self.entry.name,
            )
            return SemanticPayload(
                available=False,
                error=f"Skill name 必须与 entry 目录名一致: entry={self.entry.name!r}",
            )
        if self.facet == "activation":
            return SemanticPayload(payload={"body": content[parsed.body_offset :]})
        return SemanticPayload(
            payload={"name": parsed.name, "description": parsed.description}
        )


@dataclass(frozen=True, slots=True)
class SkillCatalogLoader:
    """按四层及显式来源优先级选择，同层同优先级的名称冲突明确失败。"""

    version: ClassVar[str] = "skill-catalog:v2"
    entries: tuple[SkillDerivationEntry, ...]

    def __call__(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload:
        layer_priority = {"inline": 0, "network": 1, "gateway": 2, "workspace": 3}
        effective_by_name: dict[str, dict[str, str]] = {}
        priority_by_name: dict[str, tuple[int, int]] = {}
        candidates: set[tuple[str, int, str]] = set()
        for entry in self.entries:
            layer = layer_priority.get(entry.layer)
            if layer is None:
                return SemanticPayload(available=False, error=f"Skill catalog 来源层未登记: layer={entry.layer}")
            name = entry.name.strip()
            candidate = (entry.layer, entry.priority, name)
            if candidate in candidates:
                return SemanticPayload(available=False, error=f"Skill catalog source conflict: layer={entry.layer}, priority={entry.priority}, name={name}")
            candidates.add(candidate)
            priority = (layer, entry.priority)
            if name in priority_by_name and priority_by_name[name] > priority:
                continue
            metadata_id = entry.resource_id("metadata")
            activation_id = entry.resource_id("activation")
            effective_by_name[name] = {
                "name": name,
                "layer": entry.layer,
                "entry_identity": entry.entry_identity,
                "display_uri": entry.display_uri,
                "metadata_resource_id": metadata_id,
                "activation_resource_id": activation_id,
                "metadata_revision": inputs[metadata_id].revision,
                "activation_revision": inputs[activation_id].revision,
            }
            priority_by_name[name] = priority
        return SemanticPayload(
            payload={
                "entries": [
                    effective_by_name[name] for name in sorted(effective_by_name)
                ]
            }
        )
