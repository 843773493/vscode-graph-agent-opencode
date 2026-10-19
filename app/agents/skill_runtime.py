from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, NotRequired, cast

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    PrivateStateAttr,
)
from langchain_core.runnables import RunnableConfig

from app.agents.instruction_producers import (
    InstructionProducerSpec,
    register_instruction_producer,
)
from app.agents.middleware_prompts import SKILLS_SYSTEM_PROMPT
from app.agents.workspace_agents import (
    publish_workspace_agents,
    register_workspace_agents_source,
)
from app.core.distribution_identity import load_distribution_id
from app.core.env import get_project_root
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import get_workspace_root
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.business.resource_derivations.skill_frontmatter import (
    parse_skill_frontmatter,
)
from app.services.business.resource_derivations.skills import (
    SkillCatalogLoader,
    SkillDerivationEntry,
    SkillFacetLoader,
)
from app.services.infrastructure.resource_platform.derivation.graph import (
    ResourceDerivationGraph,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
    PublishedSourceObservationRef,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    PublishedContextSourceRevision,
)

BUNDLED_SKILL_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

class WorkspaceSkillsState(AgentState):
    """Skill metadata 的私有请求状态;模型可见内容只有 name/description。"""

    skills_metadata: Annotated[
        NotRequired[list[dict[str, Any]]],
        PrivateStateAttr,
    ]


class WorkspaceSkillsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Skill catalog metadata/activation 的 CSM 消费中间件。
    Skill metadata 来自 ResourceRegistry 发布的 immutable catalog revision;
    AGENTS 链由 Saver 消费冻结的语义资源，不经原始文件 reactor。
    """

    state_schema = WorkspaceSkillsState

    def __init__(
        self,
        *,
        catalog: PublishedSkillCatalog | None,
        system_prompt: str | None = SKILLS_SYSTEM_PROMPT,
        context_source_manager: ContextSourceManager | None = None,
        source_registry: WorkspaceFileResourceRegistry | None = None,
        context_source_reactor: ContextSourceReactor | None = None,
        execution_owned_catalog: bool = False,
    ) -> None:
        if source_registry is not None and context_source_manager is None:
            raise RuntimeError("Skill 上下文来源注册需要 ContextSourceManager")
        self._catalog = catalog
        self._execution_owned_catalog = execution_owned_catalog
        self._context_source_manager = context_source_manager
        self._source_registry = source_registry
        self._context_source_reactor = context_source_reactor
        self.system_prompt_template = system_prompt
        if context_source_manager is not None and (context_source_reactor is not None or execution_owned_catalog):
            context_source_manager.bind_published_revision_supplier(self._freeze_published_sources)

    def _freeze_published_sources(
        self,
        descriptors: tuple[ContextSourceDescriptor, ...],
    ) -> tuple[PublishedContextSourceRevision, ...]:
        """在真实 prepare 边界冻结 Registry 正文，再按来源语义提取完整注入载荷。"""
        reactor = self._context_source_reactor
        skill_descriptors = tuple(source for source in descriptors if source.source_kind == "skill")
        ordinary = tuple(source for source in descriptors if source.source_kind != "skill")
        if not self._execution_owned_catalog:
            ordinary = descriptors
            skill_descriptors = ()
        if ordinary and reactor is None:
            raise RuntimeError("tracked-source-unavailable: 未绑定 published Registry owner")
        frozen = [
            PublishedContextSourceRevision(source.source_id, source.revision, self._source_body(source.source_id, source.content))
            for source in (reactor.freeze_published_sources(ordinary) if ordinary else ())
        ]
        if skill_descriptors:
            catalog = self._context_source_manager.frozen_skill_catalog()
            for descriptor in skill_descriptors:
                binding = catalog.sources.get(descriptor.resource_uri)
                if binding is None:
                    raise RuntimeError("tracked-source-unavailable: 原来源不在冻结 Skill facets: " + descriptor.source_id)
                frozen.append(PublishedContextSourceRevision(descriptor.source_id, binding.activation_revision, binding.body))
        return tuple(frozen)

    def _format_skills_list(self, skills: list[dict[str, Any]]) -> str:
        if not skills:
            return "(No skills available yet.)"

        lines: list[str] = []
        for skill in skills:
            name = skill.get("name")
            description = skill.get("description")
            if not isinstance(name, str) or not isinstance(description, str):
                raise TypeError("Skill metadata 必须包含 name/description")
            lines.append(f"- **{name}**: {description}")
            lines.append("  -> 匹配后调用 `skill_load(name=\"...\")` 激活正文")
        return "\n".join(lines)

    def _register_context_sources(
        self,
        update: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._context_source_manager is None:
            return dict(update)
        for entry in self._catalog.entries if self._catalog is not None else ():
            name, description, resource_uri = entry.name, entry.description, entry.display_uri
            if self._source_registry is not None:
                source_path = self._catalog.source_path(name)
                if source_path is not None:
                    source = self._source_registry.source_descriptor_for_uri(resource_uri)
                    self._source_registry.register_file(
                        uri=resource_uri,
                        path=source_path,
                        source_id=source.source_id,
                        entry_identity=source.entry_identity,
                    )

            self._context_source_manager.register(
                ContextSourceDescriptor(
                    source_id=f"skill:{name}",
                    source_kind="skill",
                    name=name,
                    description=description,
                    internal_locator=resource_uri,
                    resource_uri=resource_uri,
                ),
                # 4.1-A:Registry-owned provider binding ref 即 immutable
                # SkillCatalog revision;binding 与内容 revision 分离。
                binding_revision=self._catalog.catalog_revision,
            )
        normalized = dict(update)
        normalized["skills_metadata"] = [
            entry.metadata_view() for entry in self._catalog.entries
        ] if self._catalog is not None else []
        return normalized

    def _install_activation_snapshot(self) -> None:
        """把 D1 发布的 immutable catalog 冻结成 activation snapshot。

        snapshot/tracked 的 skill_load 只从这个 exact binding 解析；
        catalog 后续新 revision 不改变已冻结调用。
        """
        if self._context_source_manager is None or self._catalog is None:
            return
        bindings = {
            entry.name: SkillCatalogBinding(
                name=entry.name,
                resource_id=entry.activation_resource_id,
                entry_identity=entry.entry_identity,
                display_uri=entry.display_uri,
                activation_revision=entry.activation_revision,
                body=self._catalog.activation_bodies[entry.name],
            )
            for entry in self._catalog.entries
        }
        self._context_source_manager.install_skill_activation_snapshot(
            SkillCatalogActivationSnapshot(
                catalog_revision=self._catalog.catalog_revision,
                entries=bindings,
            )
        )

    def before_agent(self, state: Any, runtime: Any, config: RunnableConfig) -> dict[str, Any] | None:
        del state, runtime, config
        return self._register_workspace_context_sources()

    async def abefore_agent(
        self,
        state: Any,
        runtime: Any,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any] | None:
        del state, runtime, config
        return self._register_workspace_context_sources()

    def _register_workspace_context_sources(self) -> dict[str, Any]:
        """登记 Skill catalog 来源，注册完成后统一建立订阅。
        顺序契约：descriptor 全部注册完成后才 sync_sources，reactor 才能一次
        性为 Skill 文件建立订阅并播下首帧观察种子。
        """
        update: dict[str, Any] = {
            "skills_metadata": [
                entry.metadata_view() for entry in self._catalog.entries
            ]
            if self._catalog is not None
            else [],
        }
        if self._execution_owned_catalog:
            # 正式 runtime 的 catalog/metadata/activation 只在 Saver 的真实冻结边界消费。
            update = {"skills_metadata": []}
        else:
            update = self._register_context_sources(update)
        self._sync_source_bindings()
        if not self._execution_owned_catalog:
            self._install_activation_snapshot()
            self._register_catalog_instructions()
        return update

    def _register_catalog_instructions(self) -> None:
        """R05 只发布 catalog 正文与权威语义 revision，不编辑模型请求。"""
        if (
            self._context_source_manager is None or self._catalog is None
            or not self._catalog.entries or self.system_prompt_template is None
        ):
            return
        content = self.system_prompt_template.format(
            skills_list=self._format_skills_list([
                entry.metadata_view() for entry in self._catalog.entries
            ]),
        )
        register_instruction_producer(
            self._context_source_manager,
            InstructionProducerSpec(
                producer_id="R05", source_id="instruction:skill-catalog",
                source_kind="skill_catalog_metadata", name="Skill catalog",
                root_placement="root_eligible", content=content,
                revision=self._catalog.catalog_revision,
            ),
        )

    def _sync_source_bindings(self) -> None:
        """在请求边界做零 I/O 的订阅对账（新注册来源、tracking 状态变化）。"""
        if self._context_source_reactor is not None:
            descriptors = None
            if self._execution_owned_catalog:
                descriptors = tuple(
                    source for source in self._context_source_manager.descriptors()
                    if source.source_kind != "skill"
                )
            self._context_source_reactor.sync_sources(descriptors=descriptors)

    def _observe_pending_sources(self) -> None:
        """只消费已排队的 observation；不遍历 descriptor、不读磁盘。"""
        reactor = self._context_source_reactor
        manager = self._context_source_manager
        if reactor is None or manager is None:
            return
        for observation in reactor.drain():
            typed_observation: PublishedSourceObservationRef = observation
            raw_content = reactor.content_for(typed_observation)
            manager.observe(
                typed_observation.source_id,
                self._source_body(typed_observation.source_id, raw_content),
                revision=typed_observation.revision,
            )

    def _activation_body(self, raw_content: str) -> str:
        """activation/tracked 只消费 frontmatter 之后的精确正文。"""
        parsed = parse_skill_frontmatter(raw_content)
        return raw_content[parsed.body_offset :]

    def _source_body(self, source_id: str, raw_content: str) -> str:
        """按来源语义提取注入正文；Skill 取 frontmatter 后正文，其余取原文。"""
        descriptor = self._descriptor_for(source_id)
        if descriptor is None:
            raise RuntimeError(f"观察来源缺少 descriptor: source_id={source_id}")
        if descriptor.source_kind == "skill":
            return self._activation_body(raw_content)
        return raw_content

    def _descriptor_for(self, source_id: str) -> ContextSourceDescriptor | None:
        for descriptor in self._context_source_manager.descriptors():
            if descriptor.source_id == source_id:
                return descriptor
        return None

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        del state, runtime
        if self._context_source_manager is None:
            return None
        self._sync_source_bindings()
        if self._context_source_reactor is not None:
            self._observe_pending_sources()
        # rebuild 由唯一 Saver preparation 在受控事务内恢复；此处只观察 desired。
        return None


def install_frozen_skill_catalog(
    manager: ContextSourceManager, snapshot: SkillCatalogActivationSnapshot, *,
    include_instructions: bool,
) -> None:
    """只消费本次 Saver 已冻结的 exact facets；tracked 更新不读取 live Registry。"""
    manager.install_skill_activation_snapshot(snapshot)
    for binding in snapshot.sources.values():
        manager.register_skill_binding(binding, catalog_revision=snapshot.catalog_revision)
    metadata = []
    for binding in snapshot.entries.values():
        manager.register_skill_binding(binding, catalog_revision=snapshot.catalog_revision)
        metadata.append({"name": binding.name, "description": binding.description})
    manager.observe_frozen_tracked_skills()
    if not include_instructions or (not metadata and not any(
        descriptor.source_id == "instruction:skill-catalog" for descriptor in manager.descriptors()
    )):
        return
    lines = "\n".join(
        f'- **{entry["name"]}**: {entry["description"]}\n  -> 匹配后调用 `skill_load(name="...")` 激活正文'
        for entry in metadata
    ) or "(No skills available yet.)"
    register_instruction_producer(manager, InstructionProducerSpec(
        producer_id="R05", source_id="instruction:skill-catalog", source_kind="skill_catalog_metadata",
        name="Skill catalog", root_placement="root_eligible",
        content=SKILLS_SYSTEM_PROMPT.format(skills_list=lines), revision=snapshot.catalog_revision,
    ))


def resolve_bundled_skill_groups(
    configured_groups: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """解析 Gateway 传入的发行包 Skill 组，并拒绝不安全的路径标识。"""
    raw_groups: object = configured_groups
    if configured_groups is None:
        raw_value = os.environ.get("BOXTEAM_DEFAULT_SKILL_GROUPS")
        if raw_value is None:
            return ()
        try:
            raw_groups = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise ValueError(
                "BOXTEAM_DEFAULT_SKILL_GROUPS 必须是 JSON 数组"
            ) from error
    if not isinstance(raw_groups, list | tuple):
        raise TypeError("默认 Skill 组必须是字符串数组")
    normalized: list[str] = []
    for group in raw_groups:
        if not isinstance(group, str) or not BUNDLED_SKILL_GROUP_PATTERN.fullmatch(group):
            raise ValueError(f"默认 Skill 组 ID 无效: {group!r}")
        if group in normalized:
            raise ValueError(f"默认 Skill 组 ID 重复: {group}")
        normalized.append(group)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """有效 catalog entry 的已发布事实;模型可见内容只有 metadata_view。"""

    name: str
    description: str
    layer: str
    entry_identity: str
    display_uri: str
    metadata_resource_id: str
    activation_resource_id: str
    metadata_revision: str
    activation_revision: str

    def metadata_view(self) -> dict[str, Any]:
        """Skill metadata 只暴露名称和描述。"""
        return {
            "name": self.name,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SkillCatalogSource:
    """当前 accepted 候选；保留被高层遮蔽的来源及其冻结身份。"""

    layer: str
    name: str
    path: Path | None
    display_uri: str
    entry_identity: str
    source_id: str


@dataclass(frozen=True, slots=True)
class PublishedSkillCatalog:
    """ResourceRegistry 发布的 immutable SkillCatalog revision 快照。

    source_paths 是服务端装配数据,用于把 entry 绑定到 C1 文件来源的
    稳定读取/监视链;它不是模型可见 catalog 的一部分。
    """

    catalog_snapshot: ResourceSnapshot
    entries: tuple[SkillCatalogEntry, ...]
    source_paths: Mapping[str, Path]
    activation_bodies: Mapping[str, str]
    sources: tuple[SkillCatalogSource, ...]
    directory_ids: Mapping[str, str]
    gateway_id: str | None
    agents_file_uris: tuple[str, ...]

    @property
    def catalog_revision(self) -> str:
        return self.catalog_snapshot.revision

    def source_path(self, name: str) -> Path | None:
        return self.source_paths.get(name)


class _SkillSourceError(RuntimeError):
    """已登记 Skill 来源尚不能派生新的语义快照。"""


@dataclass(frozen=True, slots=True)
class _AcceptedSkillRevision:
    """一次发布读取的 owner accepted 事实；没有读取或重试能力。"""

    revision: str
    content: str
    available: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _SkillSourceBatch:
    """固定来源向量，供同一次 graph 求值消费；不是第二个来源 registry。"""

    revisions: Mapping[str, _AcceptedSkillRevision]
    sources: tuple[SkillCatalogSource, ...]
    diagnostic: str | None

    def observed_revision(self, source_id: str) -> _AcceptedSkillRevision:
        return self.revisions[source_id]


def build_workspace_skill_catalog(
    workspace_root: Path | None = None,
    *,
    registry: ResourceRegistry,
    file_registry: WorkspaceFileResourceRegistry,
    bundled_skill_groups: Sequence[str] | None = None,
    project_root: Path | None = None,
    gateway_id: str | None = None,
    gateway_sources: GatewaySkillSources | None = None,
) -> PublishedSkillCatalog:
    """构建三层权威 SkillCatalog 并向 ResourceRegistry 发布 immutable revision。

    按 workspace > gateway-global > inline 解析唯一 entry;同名高优先级
    层覆盖低优先级层,origin provenance 保留在 entry identity 中。
    """
    resolved_workspace_root = workspace_root or get_workspace_root()
    resolved_groups = resolve_bundled_skill_groups(bundled_skill_groups)
    workspace_id = load_or_create_workspace_id(resolved_workspace_root)
    agents_file_uris = register_workspace_agents_source(file_registry, workspace_id=workspace_id)
    publish_workspace_agents(registry=registry, file_registry=file_registry, file_uris=agents_file_uris)
    roots: dict[str, tuple[Path, str | None, tuple[str, ...] | None]] = {
        "workspace": (resolved_workspace_root / ".boxteam" / "skills", workspace_id, None),
    }
    if resolved_groups:
        roots["inline"] = (
            (project_root or get_project_root()) / "resources" / "skills",
            load_distribution_id(), tuple(resolved_groups),
        )
    directory_ids = {
        layer: file_registry.register_skill_directory(
            root=root, scope=layer, scope_id=scope_id, declared_entries=names,
        ).directory_id
        for layer, (root, scope_id, names) in roots.items()
    }
    resolved_gateway_id = gateway_id if gateway_id is not None else get_current_gateway_id()
    if gateway_sources is not None and resolved_gateway_id is None:
        raise _SkillSourceError("Gateway Skill source 缺少请求级 Gateway 身份绑定")
    if resolved_gateway_id is not None:
        if gateway_sources is None:
            raise _SkillSourceError("Gateway Skill 来源必须由认证 Gateway source owner 发布")
        directory_ids["gateway"] = gateway_sources.directory_id(gateway_id=resolved_gateway_id)
    batch = _accepted_skill_sources(
        directory_ids, file_registry=file_registry, gateway_sources=gateway_sources, gateway_id=resolved_gateway_id,
    )
    catalog = _publish_workspace_skill_catalog(
        batch,
        registry=registry, directory_ids=directory_ids, gateway_id=resolved_gateway_id,
        agents_file_uris=agents_file_uris,
    )
    if not catalog.catalog_snapshot.available:
        raise _SkillSourceError(batch.diagnostic or catalog.catalog_snapshot.error)
    return catalog


def _accepted_skill_sources(
    directory_ids: Mapping[str, str], *, file_registry: WorkspaceFileResourceRegistry,
    gateway_sources: GatewaySkillSources | None = None,
    gateway_id: str | None = None,
    previous_sources: tuple[SkillCatalogSource, ...] = (),
) -> _SkillSourceBatch:
    """一次性捕获 typed owner 已接受的目录和 raw revision；零 I/O、零解析。"""
    revisions: dict[str, _AcceptedSkillRevision] = {}
    sources: list[SkillCatalogSource] = []
    diagnostics: list[str] = []
    for layer, directory_id in directory_ids.items():
        if layer == "gateway":
            if gateway_sources is None or gateway_id is None or gateway_sources.directory_id(gateway_id=gateway_id) != directory_id:
                raise _SkillSourceError("Gateway Skill source owner mismatch")
            try:
                remote = gateway_sources.snapshot(gateway_id=gateway_id)
            except RuntimeError as error:
                diagnostics.append(str(error))
                revisions[directory_id] = _AcceptedSkillRevision("", "", False, "Gateway Skill source unavailable")
            else:
                revisions[directory_id] = _AcceptedSkillRevision(remote.revision, "", remote.available, remote.error_code)
                for entry in remote.entries:
                    source = SkillCatalogSource(
                        layer, entry.name, None, entry.display_uri, entry.entry_identity, entry.source_id,
                    )
                    sources.append(source)
                    revisions[source.source_id] = _AcceptedSkillRevision(
                        entry.revision or "", entry.content, entry.available and entry.revision is not None,
                        f"Skill 来源不可用: uri={entry.display_uri} error={entry.error_code}" if not entry.available or entry.revision is None else None,
                    )
        else:
            directory = file_registry.skill_directory_snapshot(directory_id)
            if not directory.available:
                diagnostics.append(f"Skill 固定目录不可用: layer={layer} error={directory.error}")
            revisions[directory_id] = _AcceptedSkillRevision(
                directory.revision, "", directory.available,
                f"Skill 固定目录不可用: layer={layer}" if not directory.available else None,
            )
            for entry in directory.entries:
                source = SkillCatalogSource(
                    layer, entry.name, entry.path, entry.uri,
                    entry.entry_identity, entry.source_id,
                )
                sources.append(source)
                raw = file_registry.snapshot(entry.uri)
                if not raw.available or raw.revision is None:
                    diagnostics.append(f"Skill 来源不可用: uri={entry.uri} error={raw.error}")
                revisions[source.source_id] = _AcceptedSkillRevision(
                    raw.revision or "", raw.content, raw.available and raw.revision is not None,
                    f"Skill 来源不可用: uri={entry.uri}" if not raw.available or raw.revision is None else None,
                )
        boundary = revisions[directory_id]
        if not boundary.available:
            # 目录失败不能把 partial entries 或上一批正文冒充当前有效来源。
            known = {source.source_id: source for source in (*previous_sources, *sources) if source.layer == layer}
            for source in known.values():
                if source not in sources:
                    sources.append(source)
                revisions[source.source_id] = _AcceptedSkillRevision("", "", False, boundary.error)
    return _SkillSourceBatch(MappingProxyType(revisions), tuple(sources), "; ".join(diagnostics) or None)


def _publish_workspace_skill_catalog(
    batch: _SkillSourceBatch,
    *,
    registry: ResourceRegistry,
    directory_ids: Mapping[str, str],
    gateway_id: str | None,
    agents_file_uris: tuple[str, ...],
) -> PublishedSkillCatalog:
    """唯一 Skill 发布链：accepted vector → graph facets → catalog。"""
    derivations = tuple(SkillDerivationEntry(
        source.name, source.layer, source.entry_identity, source.source_id, source.display_uri,
    ) for source in batch.sources)
    catalog_id = "skill-entry:workspace:catalog:catalog"
    with registry.publication():
        graph = ResourceDerivationGraph(reconciler=batch, registry=registry)
        for entry in derivations:
            for facet in ("metadata", "activation"):
                graph.register(SemanticResourceDescriptor(
                    resource_id=entry.resource_id(facet), resource_kind="skill", facet=facet,
                    display_uri=entry.display_uri, source_ids=(entry.source_id,),
                    loader_version=SkillFacetLoader.version,
                ), SkillFacetLoader(entry, facet))
        current_ids = {spec.resource_id for spec in graph.specs()}
        for descriptor in registry.descriptors():
            if descriptor.resource_kind == "skill" and descriptor.facet in {"metadata", "activation"} and descriptor.resource_id not in current_ids:
                registry.withdraw(descriptor.resource_id)
        try:
            previous_descriptor = registry.descriptor(catalog_id)
        except KeyError:
            previous_descriptor = None
        graph.register(SemanticResourceDescriptor(
            resource_id=catalog_id, resource_kind="skill", facet="catalog", display_uri=None,
            source_ids=tuple(sorted(directory_ids.values())),
            dependency_resource_ids=tuple(sorted(current_ids)),
            loader_version=SkillCatalogLoader.version,
        ), SkillCatalogLoader(derivations), expected=previous_descriptor)
        graph.publish_all()
        catalog_snapshot = registry.snapshot(catalog_id)
        entries: list[SkillCatalogEntry] = []
        source_paths: dict[str, Path] = {}
        activation_bodies: dict[str, str] = {}
        if catalog_snapshot.available:
            payload = cast(dict[str, list[dict[str, str]]], catalog_snapshot.payload)
            paths = {source.entry_identity: source.path for source in batch.sources}
            for item in payload["entries"]:
                metadata = cast(dict[str, str], registry.snapshot(item["metadata_resource_id"]).payload)
                activation = cast(dict[str, str], registry.snapshot(item["activation_resource_id"]).payload)
                entries.append(SkillCatalogEntry(description=metadata["description"], **item))
                path = paths[item["entry_identity"]]
                if path is not None:
                    source_paths[item["name"]] = path
                activation_bodies[item["name"]] = activation["body"]
    return PublishedSkillCatalog(
        catalog_snapshot=catalog_snapshot, entries=tuple(entries),
        source_paths=source_paths, activation_bodies=activation_bodies,
        sources=batch.sources, directory_ids=directory_ids, gateway_id=gateway_id,
        agents_file_uris=agents_file_uris,
    )


def start_workspace_skill_catalog_publication(
    catalog: PublishedSkillCatalog,
    *,
    registry: ResourceRegistry,
    file_registry: WorkspaceFileResourceRegistry,
    lifetime_scope: LifetimeScope,
    gateway_sources: GatewaySkillSources | None = None,
) -> None:
    """在应用 loop 消费既有来源通知，生命周期严格归当前 generation。"""
    loop = asyncio.get_running_loop()
    subscription = file_registry.subscribe_observation(label=f"Skill catalog:{lifetime_scope.name}")
    directory_ids = frozenset(catalog.directory_ids.values())
    task: asyncio.Task[None] | None = None
    closed = False

    def publish_current() -> None:
        nonlocal catalog
        if closed or lifetime_scope.state != "open":
            return
        publish_workspace_agents(
            registry=registry, file_registry=file_registry, file_uris=catalog.agents_file_uris,
        )
        batch = _accepted_skill_sources(
            catalog.directory_ids, file_registry=file_registry, gateway_sources=gateway_sources,
            gateway_id=catalog.gateway_id, previous_sources=catalog.sources,
        )
        catalog = _publish_workspace_skill_catalog(
            batch, registry=registry, directory_ids=catalog.directory_ids, gateway_id=catalog.gateway_id,
            agents_file_uris=catalog.agents_file_uris,
        )
        if not catalog.catalog_snapshot.available:
            logging.getLogger(__name__).error("Skill catalog 刷新失败: %s", batch.diagnostic or catalog.catalog_snapshot.error)

    async def consume() -> None:
        while not closed and lifetime_scope.state == "open":
            observation = await subscription.next()
            if observation.gap or observation.uri in catalog.agents_file_uris or observation.directory_id in directory_ids or any(
                observation.uri == source.display_uri for source in catalog.sources
            ):
                publish_current()

    def report_failure(finished: asyncio.Task[None]) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            if not closed and lifetime_scope.state == "open":
                registry.publish(replace(
                    catalog.catalog_snapshot, payload=None, available=False,
                    error=f"Skill catalog publication 任务失败: {type(error).__name__}", error_code="loader-error",
                    retained_revision=catalog.catalog_revision,
                ))
            loop.call_exception_handler({
                "message": "Skill catalog publication 任务失败", "exception": error, "task": finished,
            })

    async def close() -> None:
        nonlocal closed
        closed = True
        file_registry.unsubscribe_observation(subscription)
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    try:
        lifetime_scope.register(close, label="Skill catalog publication")
    except BaseException:
        file_registry.unsubscribe_observation(subscription)
        raise
    # 先订阅再读取内存 accepted 快照，补齐 worker 编译到首次 capture 之间的窗口。
    publish_current()
    task = asyncio.create_task(consume(), name=f"boxteam-skill-catalog:{lifetime_scope.name}")
    task.add_done_callback(report_failure)


def append_skill_middlewares(
    middleware_stack: list[AgentMiddleware],
    *,
    catalog: PublishedSkillCatalog | None,
    system_prompt: str | None = SKILLS_SYSTEM_PROMPT,
    context_source_manager: ContextSourceManager | None = None,
    source_registry: WorkspaceFileResourceRegistry | None = None,
    context_source_reactor: ContextSourceReactor | None = None,
    execution_owned_catalog: bool = False,
) -> None:
    """集中维护 workspace Skill 上下文来源 middleware 顺序。"""
    if (catalog is not None and catalog.entries) or source_registry is not None:
        middleware_stack.append(
            WorkspaceSkillsMiddleware(
                catalog=catalog,
                system_prompt=system_prompt,
                context_source_manager=context_source_manager,
                source_registry=source_registry,
                context_source_reactor=context_source_reactor,
                execution_owned_catalog=execution_owned_catalog,
            )
        )
