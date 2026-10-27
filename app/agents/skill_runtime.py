from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    AgentState,
    PrivateStateAttr,
)
from langchain_core.runnables import RunnableConfig

from app.agents.instruction_producers import (
    InstructionProducerSpec,
    InstructionUpdateBinding,
    register_instruction_producer,
)
from app.agents.middleware_prompts import SKILLS_SYSTEM_PROMPT
from app.agents.skill_catalog import PublishedSkillCatalog
from app.services.business.resource_derivations.skill_frontmatter import (
    parse_skill_frontmatter,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
    PublishedSourceObservationRef,
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
        descriptor_by_source_id = {
            descriptor.source_id: descriptor for descriptor in descriptors
        }
        frozen = [
            PublishedContextSourceRevision(
                source.source_id,
                source.revision,
                self._source_body(
                    source.source_id,
                    source.content,
                    descriptor_by_source_id=descriptor_by_source_id,
                ),
            )
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
                update_binding=InstructionUpdateBinding("skill_catalog", self._catalog.catalog_revision, "resource_activation"),
            ),
        )

    def _sync_source_bindings(self) -> tuple[ContextSourceDescriptor, ...]:
        """在请求边界做零 I/O 的订阅对账（新注册来源、tracking 状态变化）。"""
        manager = self._context_source_manager
        reactor = self._context_source_reactor
        if manager is None or reactor is None:
            return ()
        descriptors = manager.descriptors()
        selected_descriptors = descriptors
        if self._execution_owned_catalog:
            selected_descriptors = tuple(
                descriptor
                for descriptor in descriptors
                if descriptor.source_kind != "skill"
            )
        reactor.sync_sources(descriptors=selected_descriptors)
        return descriptors

    def _observe_pending_sources(
        self,
        descriptors: tuple[ContextSourceDescriptor, ...],
    ) -> None:
        """只消费已排队的 observation；仅有待观察项时构建一次 descriptor 索引。"""
        reactor = self._context_source_reactor
        manager = self._context_source_manager
        if reactor is None or manager is None:
            return
        descriptor_by_source_id: dict[str, ContextSourceDescriptor] | None = None
        for observation in reactor.drain(descriptors=descriptors):
            if descriptor_by_source_id is None:
                descriptor_by_source_id = {
                    descriptor.source_id: descriptor for descriptor in descriptors
                }
            typed_observation: PublishedSourceObservationRef = observation
            raw_content = reactor.content_for(typed_observation)
            manager.observe(
                typed_observation.source_id,
                self._source_body(
                    typed_observation.source_id,
                    raw_content,
                    descriptor_by_source_id=descriptor_by_source_id,
                ),
                revision=typed_observation.revision,
            ).require_tracked()

    def _activation_body(self, raw_content: str) -> str:
        """activation/tracked 只消费 frontmatter 之后的精确正文。"""
        parsed = parse_skill_frontmatter(raw_content)
        return raw_content[parsed.body_offset :]

    def _source_body(
        self,
        source_id: str,
        raw_content: str,
        *,
        descriptor_by_source_id: Mapping[str, ContextSourceDescriptor] | None = None,
    ) -> str:
        """按来源语义提取注入正文；Skill 取 frontmatter 后正文，其余取原文。"""
        descriptor = (
            self._descriptor_for(source_id)
            if descriptor_by_source_id is None
            else descriptor_by_source_id.get(source_id)
        )
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
        descriptors = self._sync_source_bindings()
        if self._context_source_reactor is not None:
            self._observe_pending_sources(descriptors)
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
    manager.restore_frozen_skill_selections()
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
        update_binding=InstructionUpdateBinding("skill_catalog", snapshot.catalog_revision, "resource_activation"),
    ))


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
