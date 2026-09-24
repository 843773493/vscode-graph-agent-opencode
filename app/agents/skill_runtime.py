from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NotRequired

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import (
    AgentState,
    ExtendedModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import HumanMessage

from app.agents.middleware_prompts import SKILLS_SYSTEM_PROMPT
from app.agents.skill_frontmatter import parse_skill_frontmatter
from app.core.env import get_project_root
from app.core.path_utils import get_boxteam_home, get_workspace_root
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
    SourceObservation,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    skill_display_uri,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
)

WORKSPACE_AGENTS_FILE = "AGENTS.md"
WORKSPACE_AGENTS_URI = "boxteam://workspace/agents"
WORKSPACE_AGENTS_SOURCE_ID = "agents:workspace"
BUNDLED_SKILL_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

# 唯一 CSM 消费链只接受已知来源语义的 delta；未知 kind 直接失败，
# 不回退到通用措辞。正文已由 ApplySourceLifecycleDecision 封存，这里只做准入。
_KNOWN_SOURCE_KINDS: frozenset[str] = frozenset({"skill", "workspace_agents"})


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
    ``before_model`` 消费链提交；请求路径不读磁盘，也不维护任何 AGENTS
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
    ) -> None:
        if source_registry is not None and context_source_manager is None:
            raise RuntimeError("AGENTS 上下文来源注册需要 ContextSourceManager")
        self._catalog = catalog
        self._context_source_manager = context_source_manager
        self._source_registry = source_registry
        self._context_source_reactor = context_source_reactor
        self.system_prompt_template = system_prompt

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
        normalized["skills_metadata"] = [
            dict(item) for item in self._context_source_manager.metadata()
        ]
        return normalized

    def _install_activation_snapshot(self) -> None:
        """把 D1 发布的 immutable catalog 冻结成 activation snapshot。

        snapshot/tracked 的 skill_load 只从这个 exact binding 解析；
        catalog 后续新 revision 不改变已冻结调用。
        """
        if self._context_source_manager is None:
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

    def before_agent(self, state: Any, runtime: Any, config: Any) -> dict[str, Any] | None:
        del state, runtime, config
        return self._register_workspace_context_sources()

    async def abefore_agent(
        self,
        state: Any,
        runtime: Any,
        config: Any | None = None,
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
        update = self._register_context_sources(update)
        self._register_agents_source()
        self._sync_source_bindings()
        self._install_activation_snapshot()
        return update

    def _register_agents_source(self) -> None:
        """把工作区 AGENTS.md 登记为唯一 CSM tracked 文件来源。
        D3-B 起本中间件不维护任何 AGENTS 快照、diff reminder 或 compaction
        marker 状态：首帧与后续 delta 全部经 reactor typed observation 进入
        CSM pending，由唯一 before_model 消费链提交。
        """
        if self._source_registry is None:
            return
        self._source_registry.register_file(
            uri=WORKSPACE_AGENTS_URI,
            path=self._source_registry.workspace_root / WORKSPACE_AGENTS_FILE,
        )
        self._context_source_manager.register(
            ContextSourceDescriptor(
                source_id=WORKSPACE_AGENTS_SOURCE_ID,
                source_kind="workspace_agents",
                name="AGENTS.md",
                description="当前工作区根目录 AGENTS.md 指令来源",
                internal_locator=WORKSPACE_AGENTS_URI,
                resource_uri=WORKSPACE_AGENTS_URI,
            ),
            tracking_status="tracked",
        )

    def _build_source_delta_message(self, delta: ContextSourceDelta) -> HumanMessage:
        if delta.source_kind not in _KNOWN_SOURCE_KINDS:
            raise RuntimeError(
                "未知 context source kind，拒绝构造注入消息: "
                f"source_id={delta.source_id} kind={delta.source_kind}"
            )
        return HumanMessage(
            # source 正文已经由 ApplySourceLifecycleDecision 封存；这里仅为
            # LangGraph state 提供同一正文的临时 projection，不再承担写入。
            content=delta.content,
            id=(
                f"context-source:{delta.source_id}:"
                f"{delta.revision}:{delta.kind}"
            ),
            response_metadata={
                "context_source_kind": delta.source_kind,
                "context_source_id": delta.source_id,
                "context_source_name": delta.source_name,
                "context_wire_role": delta.wire_role,
                "context_revision": delta.revision,
                "internal": True,
            },
        )

    def _sync_source_bindings(self) -> None:
        """在请求边界做零 I/O 的订阅对账（新注册来源、tracking 状态变化）。"""
        if self._context_source_reactor is not None:
            self._context_source_reactor.sync_sources()

    def _observe_pending_sources(self) -> None:
        """只消费已排队的 observation；不遍历 descriptor、不读磁盘。"""
        reactor = self._context_source_reactor
        manager = self._context_source_manager
        if reactor is None or manager is None:
            return
        for observation in reactor.drain():
            typed_observation: SourceObservation = observation
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
        batch = self._context_source_manager.prepare_pending()
        if batch is None:
            return None
        messages = [
            self._build_source_delta_message(delta) for delta in batch.deltas
        ]
        # 这里是 LangGraph middleware 的 state-update 边界：消息已经完整构造，
        # 再确认 CSM 的 pending 基准，避免“先 drain、后构造失败”吞掉变化。
        self._context_source_manager.commit_model_call_pending(batch)
        return {"messages": messages}

    def modify_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        """把 catalog metadata 注入 system prompt;模板关闭时不改动请求。"""
        if self.system_prompt_template is None:
            return request
        skills_metadata = request.state.get("skills_metadata", [])
        if not isinstance(skills_metadata, list):
            raise TypeError("skills_metadata 状态必须是列表")
        if not skills_metadata:
            return request
        skills_section = self.system_prompt_template.format(
            skills_list=self._format_skills_list(skills_metadata),
        )
        return request.override(
            system_message=append_to_system_message(
                request.system_message,
                skills_section,
            )
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return handler(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        return await handler(self.modify_request(request))


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
class PublishedSkillCatalog:
    """ResourceRegistry 发布的 immutable SkillCatalog revision 快照。

    source_paths 是服务端装配数据,用于把 entry 绑定到 C1 文件来源的
    稳定读取/监视链;它不是模型可见 catalog 的一部分。
    """

    catalog_snapshot: ResourceSnapshot
    entries: tuple[SkillCatalogEntry, ...]
    source_paths: Mapping[str, Path]
    activation_bodies: Mapping[str, str]

    @property
    def catalog_revision(self) -> str:
        return self.catalog_snapshot.revision

    def source_path(self, name: str) -> Path | None:
        return self.source_paths.get(name)


_SKILL_LAYER_LABELS = {"workspace": "Workspace", "gateway": "Gateway", "bundled": "Built-in"}


def _scan_layer_skill_files(
    *,
    layer: str,
    root: Path,
) -> dict[str, tuple[Path, str, object]]:
    """枚举固定 skills/<entry>/SKILL.md 一层边界,拒绝越界 symlink。"""
    layer_files: dict[str, tuple[Path, str, object]] = {}
    if not root.is_dir():
        return layer_files
    for entry_dir in sorted(root.iterdir()):
        if not entry_dir.is_dir():
            continue
        if entry_dir.is_symlink():
            raise RuntimeError(f"Skill entry 不允许 symlink: {entry_dir}")
        skill_file = entry_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        if skill_file.is_symlink():
            raise RuntimeError(f"SKILL.md 不允许 symlink: {skill_file}")
        content = skill_file.read_text(encoding="utf-8")
        parsed = parse_skill_frontmatter(content)
        if parsed.name != entry_dir.name:
            raise RuntimeError(
                f"Skill name 必须与 entry 目录名一致: name={parsed.name!r} "
                f"entry={entry_dir.name!r} layer={layer}"
            )
        raw_revision = "sha256:" + hashlib.sha256(
            skill_file.read_bytes()
        ).hexdigest()
        layer_files[parsed.name] = (skill_file, raw_revision, parsed)
    return layer_files


def _publish_skill_facet(
    registry: ResourceRegistry,
    *,
    layer: str,
    name: str,
    display_uri: str,
    facet: str,
    payload: object,
    source_revision: str,
) -> ResourceSnapshot:
    """按 entry/facet 发布 immutable 语义快照;revision 取 payload JCS hash。"""
    resource_id = f"skill-entry:{layer}:{name}:{facet}"
    snapshot = ResourceSnapshot(
        resource_id=resource_id,
        resource_kind="skill",
        facet=facet,
        display_uri=display_uri,
        revision=sha256_jcs(payload),
        payload=payload,
        source_lineage=((f"skill-source:{layer}:{name}", source_revision),),
        generation=0,
    )
    return registry.publish(snapshot)


def build_workspace_skill_catalog(
    workspace_root: Path | None = None,
    *,
    registry: ResourceRegistry,
    bundled_skill_groups: Sequence[str] | None = None,
    project_root: Path | None = None,
) -> PublishedSkillCatalog:
    """构建三层权威 SkillCatalog 并向 ResourceRegistry 发布 immutable revision。

    按 workspace > gateway-global > bundled 解析唯一 entry;同名高优先级
    层覆盖低优先级层,origin provenance 保留在 entry identity 中。
    """
    resolved_workspace_root = workspace_root or get_workspace_root()
    resolved_groups = resolve_bundled_skill_groups(bundled_skill_groups)
    workspace_id = load_or_create_workspace_id(resolved_workspace_root)
    layer_order: tuple[str, ...] = ("bundled", "gateway", "workspace")
    display_uri_by_layer: dict[str, dict[str, str]] = {}
    scanned: dict[str, dict[str, tuple[Path, str, object]]] = {}
    for layer in layer_order:
        if layer == "bundled":
            if not resolved_groups:
                scanned[layer] = {}
                continue
            bundled_root = (project_root or get_project_root()) / "resources" / "skills"
            if not bundled_root.is_dir():
                raise FileNotFoundError(f"发行包内置 Skill 根目录不存在: {bundled_root}")
            for group in resolved_groups:
                skill_root = bundled_root / group
                if not skill_root.is_dir():
                    raise FileNotFoundError(f"发行包内置 Skill 组目录不存在: {skill_root}")
                if not (skill_root / "SKILL.md").is_file():
                    raise FileNotFoundError(f"发行包内置 Skill 组缺少 SKILL.md: {skill_root}")
            layer_files = _scan_layer_skill_files(layer=layer, root=bundled_root)
            # bundled 只发布 manifest(resolved groups)声明的 entry;组外
            # 目录不是发行包已发布资源。
            layer_files = {
                name: item for name, item in layer_files.items()
                if name in set(resolved_groups)
            }
        elif layer == "gateway":
            gateway_root = get_boxteam_home() / "skills"
            if gateway_root.exists() and not gateway_root.is_dir():
                raise RuntimeError(f"Gateway skill 路径不是目录: {gateway_root}")
            layer_files = _scan_layer_skill_files(layer=layer, root=gateway_root)
        else:
            workspace_skills_root = resolved_workspace_root / ".boxteam" / "skills"
            if workspace_skills_root.exists() and not workspace_skills_root.is_dir():
                raise RuntimeError(f"工作区 skill 路径不是目录: {workspace_skills_root}")
            layer_files = _scan_layer_skill_files(layer=layer, root=workspace_skills_root)
        scanned[layer] = layer_files
        scope = {"workspace": "workspace", "gateway": "gateway", "bundled": "builtin"}[layer]
        scope_id = workspace_id if layer == "workspace" else "local"
        display_uri_by_layer[layer] = {
            name: skill_display_uri(scope=scope, scope_id=scope_id, skill_name=name)
            for name in layer_files
        }

    effective: dict[str, tuple[str, Path, str, object]] = {}
    for layer in layer_order:
        for name, (skill_file, raw_revision, parsed) in scanned[layer].items():
            effective[name] = (layer, skill_file, raw_revision, parsed)

    entries: list[SkillCatalogEntry] = []
    source_paths: dict[str, Path] = {}
    activation_bodies: dict[str, str] = {}
    catalog_payload_entries: list[dict[str, str]] = []
    for name in sorted(effective):
        layer, skill_file, raw_revision, parsed = effective[name]
        del parsed
        display_uri = display_uri_by_layer[layer][name]
        entry_identity = f"skill-entry:{layer}:{name}"
        metadata_payload = {
            "name": name,
            "description": scanned[layer][name][2].description,
            "display_uri": display_uri,
            "entry_identity": entry_identity,
        }
        metadata_snapshot = _publish_skill_facet(
            registry,
            layer=layer,
            name=name,
            display_uri=display_uri,
            facet="metadata",
            payload=metadata_payload,
            source_revision=raw_revision,
        )
        activation_payload = {
            "body": skill_file.read_text(encoding="utf-8")[
                scanned[layer][name][2].body_offset :
            ]
        }
        activation_bodies[name] = activation_payload["body"]
        activation_snapshot = _publish_skill_facet(
            registry,
            layer=layer,
            name=name,
            display_uri=display_uri,
            facet="activation",
            payload=activation_payload,
            source_revision=raw_revision,
        )
        entry = SkillCatalogEntry(
            name=name,
            description=metadata_payload["description"],
            layer=layer,
            entry_identity=entry_identity,
            display_uri=display_uri,
            metadata_resource_id=metadata_snapshot.resource_id,
            activation_resource_id=activation_snapshot.resource_id,
            metadata_revision=metadata_snapshot.revision,
            activation_revision=activation_snapshot.revision,
        )
        entries.append(entry)
        source_paths[name] = skill_file
        catalog_payload_entries.append(
            {
                "name": name,
                "layer": layer,
                "entry_identity": entry_identity,
                "display_uri": display_uri,
                "metadata_resource_id": entry.metadata_resource_id,
                "activation_resource_id": entry.activation_resource_id,
                "metadata_revision": entry.metadata_revision,
                "activation_revision": entry.activation_revision,
            }
        )

    catalog_snapshot = _publish_skill_facet(
        registry,
        layer="workspace",
        name="catalog",
        display_uri=f"boxteam://workspace/{workspace_id}/resources/skills/catalog",
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
    )


def append_skill_middlewares(
    middleware_stack: list[AgentMiddleware],
    *,
    catalog: PublishedSkillCatalog | None,
    system_prompt: str | None = SKILLS_SYSTEM_PROMPT,
    context_source_manager: ContextSourceManager | None = None,
    source_registry: WorkspaceFileResourceRegistry | None = None,
    context_source_reactor: ContextSourceReactor | None = None,
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
            )
        )
