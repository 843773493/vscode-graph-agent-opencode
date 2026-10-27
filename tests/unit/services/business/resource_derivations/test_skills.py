"""四层名称解析与同层优先级不依赖来源遍历顺序。"""

from itertools import permutations

import pytest

from app.services.business.resource_derivations.skills import (
    SkillCatalogLoader,
    SkillDerivationEntry,
)
from app.services.infrastructure.resource_platform.derivation.types import SemanticInput


@pytest.fixture
def entries() -> tuple[SkillDerivationEntry, ...]:
    return tuple(SkillDerivationEntry(
        name="demo", layer=layer, entry_identity=f"entry:{layer}", source_id=f"source:{layer}",
        display_uri=f"boxteam://workspace/owner/resources/skills/{layer}/SKILL.md",
        priority=100 if layer == "network" else 0,
    ) for layer in ("inline", "network", "gateway", "workspace"))


def _load(entries: tuple[SkillDerivationEntry, ...]):
    inputs = {entry.resource_id(facet): SemanticInput(entry.resource_id(facet), "revision", 1, {})
              for entry in entries for facet in ("metadata", "activation")}
    return SkillCatalogLoader(entries)(inputs)


def test_each_layer_overrides_all_lower_layers_in_any_order(entries: tuple[SkillDerivationEntry, ...]) -> None:
    for count in range(1, len(entries) + 1):
        for order in permutations(entries[:count]):
            result = _load(order)
            assert result.available
            assert result.payload["entries"][0]["entry_identity"] == entries[count - 1].entry_identity


def test_higher_priority_wins_within_network_layer(entries: tuple[SkillDerivationEntry, ...]) -> None:
    base = entries[1]
    lower = SkillDerivationEntry(name="demo", layer="network", priority=-1, entry_identity="lower", source_id="lower", display_uri=base.display_uri)
    for order in permutations((base, lower)):
        assert _load(order).payload["entries"][0]["entry_identity"] == base.entry_identity


def test_equal_priority_duplicate_is_error_even_when_shadowed(entries: tuple[SkillDerivationEntry, ...]) -> None:
    base = entries[1]
    duplicate = SkillDerivationEntry(name="demo", layer="network", priority=base.priority, entry_identity="duplicate", source_id="duplicate", display_uri=base.display_uri)
    for order in permutations((base, duplicate, entries[-1])):
        result = _load(order)
        assert not result.available and "source conflict" in result.error
