"""配置图的不可变值和定义来源。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
ConfigValue: TypeAlias = JsonScalar | Mapping[str, "ConfigValue"] | tuple["ConfigValue", ...]


def freeze(value: object) -> ConfigValue:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("配置值必须为 JSON 类型")


def thaw(value: ConfigValue, *, origins: OriginTree | None = None) -> object:
    """在 DTO 边界物化；来源文档投影可保留胜出定义的原文键。"""
    if isinstance(value, Mapping):
        return {
            origins.children[key].origin.definition_path[-1] if origins else key:
            thaw(item, origins=origins.children[key] if origins else None)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [thaw(item, origins=origins.children[str(index)] if origins else None)
                for index, item in enumerate(value)]
    return value


def json_pointer(path: tuple[str, ...]) -> str:
    return "".join("/" + key.replace("~", "~0").replace("/", "~1") for key in path)


@dataclass(frozen=True, slots=True)
class NodeLocation:
    path: Path
    pointer: str = ""


@dataclass(frozen=True, slots=True)
class FieldOrigin:
    node_id: str
    source_uri: str
    definition_path: tuple[str, ...]

    @property
    def pointer(self) -> str:
        return json_pointer(self.definition_path)

    def child(self, key: str) -> FieldOrigin:
        return FieldOrigin(self.node_id, self.source_uri, (*self.definition_path, key))


@dataclass(frozen=True, slots=True)
class OriginTree:
    origin: FieldOrigin
    children: Mapping[str, OriginTree]

    def locate(self, path: tuple[str, ...]) -> FieldOrigin:
        current = self
        for key in path:
            if key not in current.children:
                raise KeyError(json_pointer(path))
            current = current.children[key]
        return current.origin


@dataclass(frozen=True, slots=True)
class ResolvedNode:
    node_id: str
    location: NodeLocation
    parent: ResolvedNode | None
    settings: Mapping[str, ConfigValue]
    origins: OriginTree

    @property
    def chain(self) -> tuple[ResolvedNode, ...]:
        nodes: list[ResolvedNode] = []
        current: ResolvedNode | None = self
        while current is not None:
            nodes.append(current)
            current = current.parent
        return tuple(reversed(nodes))
