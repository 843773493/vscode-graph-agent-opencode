"""按 schema 合并值和来源树，禁止业务路径特例。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from jsonpointer import resolve_pointer

from .values import ConfigValue, FieldOrigin, OriginTree


class SettingsMerger:
    def __init__(self, schema: Mapping[str, object]) -> None:
        self.schema = schema
        self._references: dict[str, Mapping[str, object]] = {}

    def _resolve_schema(self, schema: Mapping[str, object]) -> Mapping[str, object]:
        visited: set[str] = set()
        while (reference := schema.get("$ref")) is not None:
            if not isinstance(reference, str) or not (reference == "#" or reference.startswith("#/")):
                raise ValueError("合并 schema 仅支持本地 JSON Pointer 引用")
            if reference in visited:
                raise ValueError("合并 schema 引用形成无结构循环")
            visited.add(reference)
            if reference not in self._references:
                target = resolve_pointer(self.schema, reference[1:], default=None)
                if not isinstance(target, Mapping):
                    raise TypeError("合并 schema 引用目标必须为对象")
                # 引用的递归由实际 JSON 值深度驱动，不在这里递归展开整个 schema。
                self._references[reference] = target
            schema = {**self._references[reference], **{key: value for key, value in schema.items() if key != "$ref"}}
        return schema

    @staticmethod
    def _child_schema(schema: Mapping[str, object], key: str) -> Mapping[str, object]:
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping) and key in properties:
            child = properties[key]
        else:
            child = schema.get("additionalProperties", {})
        return child if isinstance(child, Mapping) else {}

    def merge(
        self,
        base: ConfigValue,
        local: ConfigValue,
        *,
        origin: FieldOrigin,
        inherited_origins: OriginTree | None = None,
        schema: Mapping[str, object] | None = None,
    ) -> tuple[ConfigValue, OriginTree]:
        schema = self._resolve_schema(self.schema if schema is None else schema)
        replace = schema.get("x-merge") == "replace"
        if not isinstance(local, Mapping):
            return local, self._definition(local, origin)

        if replace or not isinstance(base, Mapping):
            base = {}
            inherited_origins = None
        values = dict(base)
        children = dict(inherited_origins.children) if inherited_origins else {}
        aliases = schema.get("x-key-aliases", {})
        fold_case = schema.get("x-key-casefold") is True
        seen: set[str] = set()
        for raw_key, value in local.items():
            key = str(aliases.get(raw_key, raw_key)) if isinstance(aliases, Mapping) else raw_key
            if fold_case:
                key = key.casefold()
            if key in seen:
                raise ValueError(f"配置节点含重复逻辑键（别名重复或大小写重复）: {origin.pointer}")
            seen.add(key)
            child_origin = origin.child(raw_key)
            values[key], children[key] = self.merge(
                values.get(key), value,
                origin=child_origin,
                inherited_origins=children.get(key),
                schema=self._child_schema(schema, key),
            )
        return MappingProxyType(values), OriginTree(origin, MappingProxyType(children))

    def _definition(self, value: ConfigValue, origin: FieldOrigin) -> OriginTree:
        if isinstance(value, Mapping):
            children = {key: self._definition(item, origin.child(key)) for key, item in value.items()}
        elif isinstance(value, tuple):
            children = {str(index): self._definition(item, origin.child(str(index))) for index, item in enumerate(value)}
        else:
            children = {}
        return OriginTree(origin, MappingProxyType(children))
