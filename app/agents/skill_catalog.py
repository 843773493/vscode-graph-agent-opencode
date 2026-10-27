"""Skill catalog 的来源捕获、纯语义派生和进程内发布；中间件只消费冻结结果。"""

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
from typing import Any, cast

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
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)

BUNDLED_SKILL_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

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
    priority: int = 0


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
                        entry.layer, entry.name, None, entry.display_uri, entry.entry_identity, entry.source_id, entry.priority,
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
            known = {source.source_id: source for source in (*previous_sources, *sources) if source.layer == layer or (layer == "gateway" and source.layer == "network")}
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
        source.priority,
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
