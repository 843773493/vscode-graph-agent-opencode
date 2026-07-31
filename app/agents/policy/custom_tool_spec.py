from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re

from app.agents.builtin_tool_registry import resolve_builtin_tool_factory


_CUSTOM_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{1,63}$")
_FACTORY_PATH_PATTERN = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*:"
    r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$"
)
_SUPPORTED_FIELDS = frozenset(
    {"tool_id", "name", "factory", "options", "description"}
)


@dataclass(frozen=True, slots=True)
class ParsedCustomToolSpec:
    """完成类型校验和空白归一化后的扩展工具声明。"""

    name: str
    factory_path: str
    options: dict[str, object]
    description: str | None = None
    tool_id: str | None = None

    def to_config(self) -> dict[str, object]:
        result: dict[str, object]
        if self.tool_id is not None:
            result = {"tool_id": self.tool_id}
        else:
            result = {"name": self.name, "factory": self.factory_path}
        if self.options:
            result["options"] = dict(self.options)
        if self.description is not None:
            result["description"] = self.description
        return result


def parse_custom_tool_spec(
    raw_spec: object,
    *,
    context: str = "tools.custom 条目",
) -> ParsedCustomToolSpec:
    if isinstance(raw_spec, str):
        raise ValueError(
            f"{context} 不支持只写工具名；"
            '请配置为 {"name": "tool_name", '
            '"factory": "module.path:create_tool"}'
        )
    if not isinstance(raw_spec, Mapping):
        raise TypeError(
            f"{context} 必须是对象，实际类型: {type(raw_spec).__name__}"
        )
    unknown_fields = set(raw_spec) - _SUPPORTED_FIELDS
    if unknown_fields:
        raise ValueError(
            f"{context} 包含不支持的字段: {', '.join(sorted(unknown_fields))}"
        )

    raw_tool_id = raw_spec.get("tool_id")
    has_tool_id = raw_tool_id is not None
    has_custom_identity = "name" in raw_spec or "factory" in raw_spec
    if has_tool_id == has_custom_identity:
        raise ValueError(
            f"{context} 必须且只能声明 tool_id，或同时声明 name 与 factory"
        )

    tool_id: str | None = None
    if has_tool_id:
        tool_id = _stripped_string(raw_tool_id, field=f"{context}.tool_id")
        name = tool_id
        factory_path = resolve_builtin_tool_factory(tool_id)
    else:
        name = _stripped_string(raw_spec.get("name"), field=f"{context}.name")
        factory_path = _stripped_string(
            raw_spec.get("factory"),
            field=f"{context}.factory",
        )

    if not _CUSTOM_TOOL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"{context}.name 格式无效: {name!r}")

    if not _FACTORY_PATH_PATTERN.fullmatch(factory_path):
        raise ValueError(
            f"{context}.factory 必须使用 'module.path:factory_name' 格式，"
            f"实际值: {factory_path!r}"
        )

    options = raw_spec.get("options", {})
    if not isinstance(options, Mapping):
        raise TypeError(f"{context}[{name}].options 必须是对象")
    if not all(isinstance(key, str) for key in options):
        raise TypeError(f"{context}[{name}].options 的键必须是字符串")

    raw_description = raw_spec.get("description")
    description = (
        None
        if raw_description is None
        else _stripped_string(
            raw_description,
            field=f"{context}[{name}].description",
        )
    )
    return ParsedCustomToolSpec(
        name=name,
        factory_path=factory_path,
        options=dict(options),
        description=description,
        tool_id=tool_id,
    )


def parse_custom_tool_specs(
    raw_specs: Iterable[object],
    *,
    context: str = "tools.custom",
) -> tuple[ParsedCustomToolSpec, ...]:
    if isinstance(raw_specs, (str, bytes)):
        raise TypeError(f"{context} 必须是对象数组")
    parsed: list[ParsedCustomToolSpec] = []
    seen_names: set[str] = set()
    for index, raw_spec in enumerate(raw_specs):
        spec = parse_custom_tool_spec(
            raw_spec,
            context=f"{context}[{index}]",
        )
        if spec.name in seen_names:
            raise ValueError(f"{context} 包含重复扩展工具名: {spec.name}")
        seen_names.add(spec.name)
        parsed.append(spec)
    return tuple(parsed)


def custom_tool_spec_names(
    raw_specs: Iterable[object],
    *,
    context: str = "tools.custom",
) -> frozenset[str]:
    return frozenset(
        spec.name
        for spec in parse_custom_tool_specs(raw_specs, context=context)
    )


def _stripped_string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是非空字符串")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} 必须是非空字符串")
    return stripped
