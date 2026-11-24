"""仅跟随显式父引用，在一个 owner 候选内解析并缓存节点。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast
from urllib.parse import unquote, urlsplit

from jsonpointer import resolve_pointer
from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators
from referencing import Registry
from referencing.exceptions import Unresolvable

from app.core.config_sources import (
    StableConfigFile,
    parse_stable_config_file,
    read_stable_config_file,
)
from app.core.resource_uri import parse_resource_uri

from .merge import SettingsMerger
from .schema import config_document_schema
from .values import ConfigValue, FieldOrigin, NodeLocation, ResolvedNode, freeze

# 解析结果不可变；校验直接接受只读映射和元组，不为每个模型物化整个工作区。
_Validator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({
    "object": lambda _checker, value: isinstance(value, Mapping),
    "array": lambda _checker, value: isinstance(value, (list, tuple)),
}))


class ConfigGraph:
    def __init__(
        self,
        *,
        domain: Literal["gateway", "workspace"],
        version: int,
        owner_id: str,
        entry: Path,
        aliases: Mapping[str, Path],
        settings_schema: dict[str, object],
        options_schema: dict[str, object],
        source_uri: Callable[[Path], str],
        authorize_source: Callable[[Path], bool],
        registered_uris: Mapping[str, Path] | None = None,
        read_source: Callable[[Path], StableConfigFile] = read_stable_config_file,
        source_documents: Mapping[str, Mapping[str, ConfigValue]] | None = None,
        document_overrides: Mapping[str, Mapping[str, ConfigValue]] | None = None,
    ) -> None:
        self.domain, self.version, self.owner_id = domain, version, owner_id
        self.entry = NodeLocation(entry.expanduser().resolve())
        self._aliases = {key: path.expanduser().resolve() for key, path in aliases.items()}
        allowed_aliases = {f"builtin:{domain}", f"user:{domain}", f"user:{domain}_local"}
        if self._aliases.keys() - allowed_aliases:
            raise ValueError("配置别名不属于当前配置域")
        self._registered_uris = dict(registered_uris or {})
        for uri in self._registered_uris:
            self._require_config_uri(uri)
        self._source_uri_provider, self._authorize_source, self._read_source = source_uri, authorize_source, read_source
        # active/pending 的固定来源由原快照 owner 提供；禁止读取当前文件补齐历史定义。
        self._source_documents = source_documents
        self._document_overrides = document_overrides or {}
        self._documents: dict[Path, Mapping[str, ConfigValue]] = {}
        self._source_locations: dict[str, Path] = {}
        self._dependencies: dict[Path, StableConfigFile] = {}
        self._nodes: dict[NodeLocation, ResolvedNode] = {}
        self._scope: ResolvedNode | None = None
        self._validated_options: set[NodeLocation] = set()
        self._resolving: list[NodeLocation] = []
        self._merger = SettingsMerger(settings_schema)
        try:
            _Validator.check_schema(settings_schema)
            # Workspace 共同选项可不完整；消费者最终选项使用 owner 提供的完整 schema。
            _Validator.check_schema(options_schema)
        except SchemaError as error:
            raise ValueError("配置 schema 校验失败: stage=schema") from error
        self._settings_validator = _Validator(settings_schema, registry=Registry())
        self._options_validator = _Validator(options_schema, registry=Registry())
        self._document_validator = _Validator(
            config_document_schema(domain=domain, version=version), registry=Registry(),
        )

    @property
    def dependencies(self) -> Mapping[Path, StableConfigFile]:
        """失败候选的缺失来源也保留；后续观察和提交仍由原 owner 决定。"""
        return MappingProxyType(self._dependencies)

    @property
    def source_documents(self) -> Mapping[str, Mapping[str, ConfigValue]]:
        """原快照可保存已读定义，避免重启时拿新父链解释旧 active。"""
        return MappingProxyType({uri: self._documents[path] for uri, path in self._source_locations.items()
                                 if path in self._documents})

    @property
    def source_locations(self) -> Mapping[str, Path]:
        """原 owner 用于监听和 URI 定位；原生路径不进入来源快照。"""
        return MappingProxyType(self._source_locations)

    def resolve_scope(self) -> ResolvedNode:
        if self._scope is None:
            scope = self._resolve(self.entry)
            self._validate(self._settings_validator, scope.settings, self.entry)
            self._scope = scope
        return self._scope

    def resolve_provider(self, provider_id: str) -> ResolvedNode:
        scope = self.resolve_scope()
        path = ("workspace", "llm", "providers", provider_id, "config_ref")
        return self._selected_node(scope, path)

    def resolve_model(self, provider_id: str, model_id: str) -> ResolvedNode:
        scope = self.resolve_scope()
        path = ("workspace", "llm", "providers", provider_id, "models", model_id, "config_ref")
        return self._selected_node(scope, path)

    def _selected_node(self, scope: ResolvedNode, path: tuple[str, ...]) -> ResolvedNode:
        if self.domain != "workspace":
            raise ValueError("仅 Workspace 配置域包含模型节点")
        reference: ConfigValue = scope.settings
        for key in path:
            if not isinstance(reference, Mapping) or key not in reference:
                raise ValueError("配置对象缺少 config_ref 或稳定身份不存在")
            reference = reference[key]
        if not isinstance(reference, str) or not reference:
            raise TypeError("config_ref 必须为非空字符串")
        origin = scope.origins.locate(path)
        declaration = next(node.location for node in scope.chain if node.node_id == origin.node_id)
        return self.resolve_reference(reference, declared_at=declaration)

    def resolve_reference(self, reference: str, *, declared_at: NodeLocation) -> ResolvedNode:
        self.resolve_scope()
        result = self._resolve(self._reference_location(reference, declared_at))
        if result.location not in self._validated_options:
            options = {key: value for key, value in result.settings.items() if key in {"network", "model_options"}}
            self._validate(self._options_validator, options, result.location)
            self._validated_options.add(result.location)
        return result

    def _reference_location(self, reference: str, declared_at: NodeLocation) -> NodeLocation:
        source, separator, fragment = reference.partition("#")
        pointer = unquote(fragment) if separator else ""
        if pointer and (not pointer.startswith("/profiles/") or pointer.count("/") != 2):
            raise ValueError("配置引用只能指向根或一个 profiles 节点")
        if source.startswith("context:"):
            if pointer:
                raise ValueError("上下文引用不接受 fragment")
            if source == f"context:{self.domain}":
                return self.entry
            if source.startswith("context:provider/") and self.domain == "workspace":
                provider_id = source.removeprefix("context:provider/")
                if not provider_id or "/" in provider_id:
                    raise ValueError("Provider 上下文引用必须包含一个稳定 ID")
                return self.resolve_provider(provider_id).location
            raise ValueError("上下文引用不属于当前配置域")
        if source in self._aliases:
            path = self._aliases[source]
        elif source in self._registered_uris:
            path = self._registered_uris[source]
        elif not source:
            path = declared_at.path
        else:
            parsed = urlsplit(source)
            if parsed.scheme and not Path(source).is_absolute():
                raise ValueError("未知配置别名、未登记 URI 或不支持的网络来源")
            path = Path(source).expanduser()
            if not path.is_absolute():
                path = declared_at.path.parent / path
        return NodeLocation(path.expanduser().resolve(), pointer)

    def _document(self, path: Path) -> Mapping[str, ConfigValue]:
        if path in self._documents:
            return self._documents[path]
        if self.domain == "gateway" and ".boxteam" in path.parts:
            raise PermissionError("Gateway 禁止读取工作区配置来源")
        if not self._authorize_source(path):
            raise PermissionError("配置来源不在当前 owner 的可读范围")
        uri = self._source_uri(path)
        if uri in self._source_locations and self._source_locations[uri] != path:
            raise ValueError("不同配置文件不能使用同一来源 URI")
        self._source_locations[uri] = path
        if self._source_documents is not None:
            if uri not in self._source_documents:
                raise ValueError(f"固定配置快照缺少来源定义: source={uri}")
            document = self._source_documents[uri]
        else:
            snapshot = self._read_source(path)
            self._dependencies[path] = snapshot
            document = (self._document_overrides[uri] if uri in self._document_overrides
                        else parse_stable_config_file(snapshot))
            if document is None:
                raise FileNotFoundError("声明的配置来源不存在")
        self._validate(self._document_validator, document, NodeLocation(path))
        immutable = cast(Mapping[str, ConfigValue], freeze(document))
        self._documents[path] = immutable
        return immutable

    def _validate(self, validator: Draft202012Validator, value: object, location: NodeLocation) -> None:
        try:
            validator.validate(value)
        except Unresolvable as error:
            raise ValueError("配置 schema 引用无法解析；禁止自动下载远端 schema") from error
        except ValidationError as error:
            # 正文、原生路径及秘密仅保留在原始异常链，不拼入普通错误。
            raise ValueError(
                f"配置节点校验失败: source={self._source_uri(location.path)}, "
                f"node={location.pointer or '<root>'}, rule={error.validator}"
            ) from error

    def _resolve(self, location: NodeLocation) -> ResolvedNode:
        if location in self._nodes:
            return self._nodes[location]
        if location in self._resolving:
            chain = " → ".join(self._node_id(item) for item in (*self._resolving, location))
            raise ValueError(f"配置继承出现循环: {chain}")
        self._resolving.append(location)
        try:
            document = self._document(location.path)
            # jsonpointer 的缺键异常会包含完整文档；使用其 default API 保持秘密不外泄。
            definition = resolve_pointer(document, location.pointer, default=None)
            if definition is None:
                raise ValueError(f"配置引用未指向节点: source={self._source_uri(location.path)}, node={location.pointer}")
            definition = cast(Mapping[str, ConfigValue], definition)
            settings = definition["settings"]
            parent_ref = definition.get("extends")
            parent = self._resolve(self._reference_location(parent_ref, location)) if isinstance(parent_ref, str) else None
            origin = FieldOrigin(self._node_id(location), self._source_uri(location.path),
                                 (*self._pointer_parts(location.pointer), "settings"))
            merged, origins = self._merger.merge(
                parent.settings if parent else None, settings,
                origin=origin, inherited_origins=parent.origins if parent else None,
            )
            result = ResolvedNode(self._node_id(location), location, parent,
                                  cast(Mapping[str, ConfigValue], merged), origins)
            self._nodes[location] = result
            return result
        finally:
            self._resolving.pop()

    def _node_id(self, location: NodeLocation) -> str:
        return f"{self.owner_id}|{self._source_uri(location.path)}#{location.pointer}"

    def _source_uri(self, path: Path) -> str:
        uri = self._source_uri_provider(path)
        self._require_config_uri(uri)
        return uri

    def _require_config_uri(self, uri: str) -> None:
        parsed = parse_resource_uri(uri)
        if parsed.kind != "config":
            raise ValueError("配置来源 URI 必须为已登记的 config 资源")
        if (self.domain == "gateway" and parsed.scope == "workspace") or (self.domain == "workspace" and parsed.scope == "gateway"):
            raise PermissionError("配置来源 URI 不属于当前配置域")

    @staticmethod
    def _pointer_parts(pointer: str) -> tuple[str, ...]:
        return tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:])
