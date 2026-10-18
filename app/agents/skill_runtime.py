from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any, NotRequired

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
from app.agents.skill_frontmatter import (
    ParsedSkillFrontmatter,
    SkillFrontmatterError,
    parse_skill_frontmatter,
)
from app.core.distribution_identity import load_distribution_id
from app.core.env import get_project_root
from app.core.lifecycle import LifetimeScope
from app.core.path_utils import get_workspace_root
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
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
from app.services.infrastructure.resource_platform.virtual_resources import (
    workspace_agent_spec_display_uri,
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

WORKSPACE_AGENTS_FILE = "AGENTS.md"
WORKSPACE_AGENTS_SOURCE_ID = "agents:workspace"


def workspace_agents_uri(workspace_id: str) -> str:
    """工作区 AGENTS.md 的规范 URI（agent-spec kind）。"""
    return workspace_agent_spec_display_uri(workspace_id)


BUNDLED_SKILL_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

class WorkspaceSkillsState(AgentState):
    """Skill metadata 的私有请求状态;模型可见内容只有 name/description。"""

    skills_metadata: Annotated[
        NotRequired[list[dict[str, Any]]],
        PrivateStateAttr,
    ]


class WorkspaceSkillsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Skill catalog metadata/activation 与 AGENTS 的唯一 CSM 消费中间件。
    Skill metadata 来自 ResourceRegistry 发布的 immutable catalog revision;
    AGENTS.md 由 ``_register_agents_source`` 登记为 tracked 文件来源。两者的
    首帧/delta 都只经 reactor typed observation 进入 CSM pending，再由唯一
    Saver prepare/seal 消费链提交；请求路径不读磁盘，也不维护任何 AGENTS
    快照、diff reminder 或 compaction marker 状态。
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
            raise RuntimeError("AGENTS 上下文来源注册需要 ContextSourceManager")
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
        raw_skills = update.get("skills_metadata", [])
        if not isinstance(raw_skills, list):
            raise TypeError("Skills metadata 必须是列表")
        for raw_skill in raw_skills:
            if not isinstance(raw_skill, Mapping):
                raise TypeError("Skill metadata 元素必须是对象")
            name = raw_skill.get("name")
            description = raw_skill.get("description")
            resource_uri = raw_skill.get("display_uri")
            if not all(isinstance(value, str) and value for value in (name, resource_uri)):
                raise TypeError("Skill metadata 缺少 name/display_uri")
            if not isinstance(description, str):
                raise TypeError("Skill metadata.description 必须是字符串")

            if self._source_registry is not None:
                source_path = self._catalog.source_path(name)
                if source_path is not None:
                    self._source_registry.register_file(
                        uri=resource_uri,
                        path=source_path,
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
        normalized["skills_metadata"] = [dict(item) for item in raw_skills]
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
        """登记 Skill catalog 与 AGENTS 来源，注册完成后统一建立订阅。
        顺序契约：descriptor 全部注册完成后才 sync_sources，reactor 才能一次
        性为 Skill 文件与 AGENTS.md 建立订阅并播下首帧观察种子。
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
        self._register_agents_source()
        self._sync_source_bindings()
        if not self._execution_owned_catalog:
            self._install_activation_snapshot()
            self._register_catalog_instructions()
        return update

    def _register_agents_source(self) -> None:
        """把工作区 AGENTS.md 登记为唯一 CSM tracked 文件来源。
        D3-B 起本中间件不维护任何 AGENTS 快照、diff reminder 或 compaction
        marker 状态：首帧与后续 delta 全部经 reactor typed observation 进入
        CSM pending，由唯一 Saver prepare/seal 消费链提交。
        """
        if self._source_registry is None:
            return
        agents_uri = workspace_agents_uri(
            load_or_create_workspace_id(self._source_registry.workspace_root)
        )
        self._source_registry.register_file(
            uri=agents_uri,
            path=self._source_registry.workspace_root / WORKSPACE_AGENTS_FILE,
            optional=True,
        )
        self._context_source_manager.register(
            ContextSourceDescriptor(
                source_id=WORKSPACE_AGENTS_SOURCE_ID,
                source_kind="workspace_agents",
                name="AGENTS.md",
                description="当前工作区根目录 AGENTS.md 指令来源",
                internal_locator=agents_uri,
                resource_uri=agents_uri,
                root_placement="root_eligible",
            ),
            tracking_status="tracked",
        )

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
        self._context_source_manager.refresh_context_rebuild_state()
        self._sync_source_bindings()
        if self._context_source_reactor is not None:
            self._observe_pending_sources()
        # before_model 只观察 desired；唯一 Saver seal 成功后才确认 applied。
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
        """模型可见 metadata;绝不携带 path/locator/物理路径。"""
        return {
            "name": self.name,
            "description": self.description,
            "display_uri": self.display_uri,
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

    @property
    def catalog_revision(self) -> str:
        return self.catalog_snapshot.revision

    def source_path(self, name: str) -> Path | None:
        return self.source_paths.get(name)


class _SkillSourceError(RuntimeError):
    """已登记 Skill 来源尚不能派生新的语义快照。"""


@dataclass(frozen=True, slots=True)
class _AcceptedSkillSource:
    source: SkillCatalogSource
    raw_revision: str
    parsed: ParsedSkillFrontmatter
    content: str


def _parse_skill_source(
    source: SkillCatalogSource, *, revision: str | None, content: str,
    available: bool, error: str | None,
) -> _AcceptedSkillSource:
    if not available or revision is None:
        raise _SkillSourceError(
            f"Skill 来源不可用: uri={source.display_uri} error={error}"
        )
    parsed = parse_skill_frontmatter(content)
    if parsed.name != source.name:
        raise _SkillSourceError(
            f"Skill name 必须与 entry 目录名一致: name={parsed.name!r} "
            f"entry={source.name!r} layer={source.layer}"
        )
    return _AcceptedSkillSource(source, revision, parsed, content)


def _publish_skill_facet(
    registry: ResourceRegistry,
    *,
    entry_identity: str,
    source_id: str,
    display_uri: str | None,
    facet: str,
    payload: object,
    source_revision: str,
) -> ResourceSnapshot:
    """按来源 owner 的稳定 entry/source 身份发布 immutable 语义快照。"""
    snapshot = ResourceSnapshot(
        resource_id=f"{entry_identity}:{facet}",
        resource_kind="skill",
        facet=facet,
        display_uri=display_uri,
        revision=sha256_jcs(payload),
        payload=payload,
        source_lineage=((source_id, source_revision),),
        generation=0,
    )
    return registry.publish(snapshot)


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
        gateway_sources.snapshot(gateway_id=resolved_gateway_id)
        directory_ids["gateway"] = gateway_sources.directory_id
    return _publish_workspace_skill_catalog(
        _accepted_skill_sources(directory_ids, file_registry=file_registry, gateway_sources=gateway_sources),
        registry=registry, directory_ids=directory_ids,
    )


def _accepted_skill_sources(
    directory_ids: Mapping[str, str], *, file_registry: WorkspaceFileResourceRegistry,
    gateway_sources: GatewaySkillSources | None = None,
) -> dict[str, dict[str, _AcceptedSkillSource]]:
    """消费 typed 来源 owner 已接受的事实；本函数零 I/O。"""
    scanned: dict[str, dict[str, _AcceptedSkillSource]] = {
        "inline": {}, "gateway": {}, "workspace": {},
    }
    for layer, directory_id in directory_ids.items():
        if layer == "gateway":
            if gateway_sources is None or gateway_sources.directory_id != directory_id:
                raise _SkillSourceError("Gateway Skill source owner mismatch")
            try:
                remote = gateway_sources.snapshot(gateway_id=gateway_sources.gateway_id)
            except RuntimeError as error:
                raise _SkillSourceError(str(error)) from error
            if not remote.available:
                raise _SkillSourceError(f"Gateway Skill source unavailable: {remote.error_code}")
            for entry in remote.entries:
                source = SkillCatalogSource(
                    layer, entry.name, None, entry.display_uri, entry.entry_identity, entry.source_id,
                )
                scanned[layer][entry.name] = _parse_skill_source(
                    source, revision=entry.revision, content=entry.content,
                    available=entry.available, error=entry.error_code,
                )
            continue
        directory = file_registry.skill_directory_snapshot(directory_id)
        if not directory.available:
            raise _SkillSourceError(f"Skill 固定目录不可用: layer={layer} error={directory.error}")
        for entry in directory.entries:
            source = SkillCatalogSource(
                layer, entry.name, entry.path, entry.uri,
                f"skill-entry:{layer}:{entry.name}", f"skill-source:{layer}:{entry.name}",
            )
            raw = file_registry.snapshot(entry.uri)
            scanned[layer][entry.name] = _parse_skill_source(
                source, revision=raw.revision, content=raw.content,
                available=raw.available, error=raw.error,
            )
    return scanned


def _publish_workspace_skill_catalog(
    scanned: Mapping[str, Mapping[str, _AcceptedSkillSource]],
    *,
    registry: ResourceRegistry,
    directory_ids: Mapping[str, str],
    previous_sources: tuple[SkillCatalogSource, ...] = (),
) -> PublishedSkillCatalog:
    """从同一批 accepted 来源派生全部 facets；发布段没有 await 或文件读取。"""
    effective: dict[str, tuple[SkillCatalogEntry, Path | None, str]] = {}
    sources: list[SkillCatalogSource] = []
    for layer in ("inline", "gateway", "workspace"):
        for name, accepted in scanned[layer].items():
            source, raw_revision, parsed, content = accepted.source, accepted.raw_revision, accepted.parsed, accepted.content
            skill_file, display_uri, entry_identity = source.path, source.display_uri, source.entry_identity
            metadata_snapshot = _publish_skill_facet(
                registry, entry_identity=entry_identity, source_id=source.source_id, display_uri=display_uri,
                facet="metadata", source_revision=raw_revision,
                payload={
                    "name": name, "description": parsed.description,
                    "display_uri": display_uri, "entry_identity": entry_identity,
                },
            )
            body = content[parsed.body_offset:]
            activation_snapshot = _publish_skill_facet(
                registry, entry_identity=entry_identity, source_id=source.source_id, display_uri=display_uri,
                facet="activation", payload={"body": body}, source_revision=raw_revision,
            )
            effective[name] = (SkillCatalogEntry(
                name=name, description=parsed.description, layer=layer,
                entry_identity=entry_identity, display_uri=display_uri,
                metadata_resource_id=metadata_snapshot.resource_id,
                activation_resource_id=activation_snapshot.resource_id,
                metadata_revision=metadata_snapshot.revision,
                activation_revision=activation_snapshot.revision,
            ), skill_file, body)
            sources.append(source)

    source_uris = {source.display_uri for source in sources}
    for source in previous_sources:
        if source.display_uri not in source_uris:
            for facet in ("metadata", "activation"):
                registry.withdraw(f"{source.entry_identity}:{facet}")

    entries: list[SkillCatalogEntry] = []
    source_paths: dict[str, Path] = {}
    activation_bodies: dict[str, str] = {}
    catalog_payload_entries: list[dict[str, str]] = []
    for name in sorted(effective):
        entry, skill_file, body = effective[name]
        activation_bodies[name] = body
        entries.append(entry)
        if skill_file is not None:
            source_paths[name] = skill_file
        catalog_payload_entries.append(
            {
                "name": name,
                "layer": entry.layer,
                "entry_identity": entry.entry_identity,
                "display_uri": entry.display_uri,
                "metadata_resource_id": entry.metadata_resource_id,
                "activation_resource_id": entry.activation_resource_id,
                "metadata_revision": entry.metadata_revision,
                "activation_revision": entry.activation_revision,
            }
        )

    catalog_snapshot = _publish_skill_facet(
        registry,
        entry_identity="skill-entry:workspace:catalog",
        source_id="skill-source:workspace:catalog",
        # catalog 是内部语义聚合，没有可访问地址；条目仍携带真实资源 URI。
        display_uri=None,
        facet="catalog",
        payload={"entries": catalog_payload_entries},
        source_revision="sha256:" + hashlib.sha256(
            json.dumps(catalog_payload_entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    )
    return PublishedSkillCatalog(
        catalog_snapshot=catalog_snapshot,
        entries=tuple(entries),
        source_paths=source_paths,
        activation_bodies=activation_bodies,
        sources=tuple(sources),
        directory_ids=directory_ids,
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
        try:
            scanned = _accepted_skill_sources(catalog.directory_ids, file_registry=file_registry, gateway_sources=gateway_sources)
        except (_SkillSourceError, SkillFrontmatterError) as error:
            # required 来源失败必须阻止后续 capture；旧正文仅保留为审计 revision。
            registry.publish(replace(
                catalog.catalog_snapshot, payload=None, available=False,
                error=str(error), error_code="source-unavailable",
                retained_revision=catalog.catalog_revision,
            ))
            logging.getLogger(__name__).error("Skill catalog 刷新失败: %s", error)
            return
        catalog = _publish_workspace_skill_catalog(
            scanned, registry=registry, directory_ids=catalog.directory_ids,
            previous_sources=catalog.sources,
        )

    async def consume() -> None:
        while not closed and lifetime_scope.state == "open":
            observation = await subscription.next()
            if observation.gap or observation.directory_id in directory_ids or any(
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
                    error=str(error), error_code="loader-error",
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
    """集中维护 workspace Skill/AGENTS 上下文来源 middleware 顺序。"""
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
