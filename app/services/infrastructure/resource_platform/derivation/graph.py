"""无环 ResourceDerivationGraph:来源 revision → 语义 facet 快照。

graph 只消费 SourceReconciler 已发布的 ObservedSourceRevision 与
ResourceRegistry 已发布的快照(经 Protocol 解耦,不直接调用 reader);
按代码内注册的 derivation 规范驱动版本化 loader,做语义 diff 与 CAS
发布。依赖缺失/成环、混合 generation、来源 unavailable 都显式失败
并保留旧 valid 快照。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticInput,
    SemanticLoader,
    SemanticPayload,
    SemanticResourceDescriptor,
)

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.registry.semantic_registry import (
        ResourceRegistry,
    )


@runtime_checkable
class ObservedRevisionSource(Protocol):
    """graph 需要的 reconciler 窄接口;不暴露 reader 与 locator。"""

    def observed_revision(self, source_id: str) -> object:
        """返回已发布的不可变来源 revision;未 observe 过时抛 KeyError。"""
        ...


def _invoke_loader(
    loader: SemanticLoader, inputs: Mapping[str, SemanticInput],
) -> SemanticPayload:
    """统一调用 loader:带 load 方法的 port 对象或纯函数均可。"""
    load = getattr(loader, "load", None)
    if callable(load):
        return load(inputs)
    return loader(inputs)


class _DerivationBlocked(Exception):
    """派生被阻断的内部信号;携带闭合 error_code。"""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


class ResourceDerivationGraph:
    """代码内固定注册的无环语义派生图。

    - register 时对已注册规范做无环校验,发布前再次守护;
    - observe_source 由来源 owner 在 reconciler 发布新 revision 后调用,
      推进来源代际;发布要求全部输入同一 generation;
    - publish 对单 facet 做语义 diff:payload 的 JCS hash 未变时不推进
      语义 revision、不重新发布(CAS 幂等)。
    """

    def __init__(
        self,
        *,
        reconciler: ObservedRevisionSource,
        registry: ResourceRegistry,
    ) -> None:
        self._reconciler = reconciler
        self._registry = registry
        self._specs: dict[str, SemanticResourceDescriptor] = {}
        self._loaders: dict[str, SemanticLoader] = {}
        # 代际在每次成功发布时推进;unavailable 快照沿用当前代际。
        self._generation = 0

    def register(
        self, spec: SemanticResourceDescriptor, loader: SemanticLoader
    ) -> None:
        """代码内注册 derivation 规范与版本化 loader;重复注册同 id 冲突。"""
        if not isinstance(spec, SemanticResourceDescriptor):
            raise TypeError(
                "ResourceDerivationGraph.register 需要 SemanticResourceDescriptor"
            )
        if not callable(loader):
            raise TypeError("ResourceDerivationGraph.register 需要可调用 loader")
        if spec.resource_id in self._specs:
            raise ValueError(f"语义资源重复注册: resource_id={spec.resource_id}")
        self._specs[spec.resource_id] = spec
        self._loaders[spec.resource_id] = loader
        self._ensure_acyclic()
        self._registry.register_descriptor(spec)

    def specs(self) -> tuple[SemanticResourceDescriptor, ...]:
        return tuple(self._specs.values())

    def observe_source(self, source_id: str) -> int:
        """来源 owner 确认该来源最新 revision 已被消费。

        来源 revision 事实由 SourceReconciler 持有;本方法只做 identity
        校验并回执当前代际。混合代际守卫在 publish 时按依赖新鲜度执行:
        依赖快照的 source lineage 必须等于上游来源当前 observed revision。
        """
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("observe_source.source_id 必须是非空字符串")
        return self._generation

    def current_generation(self) -> int:
        return self._generation

    def publish(self, resource_id: str) -> ResourceSnapshot:
        """对单个语义资源执行一次有界派生发布。"""
        spec = self._specs.get(resource_id)
        if spec is None:
            raise KeyError(f"语义资源尚未注册: resource_id={resource_id}")
        self._ensure_acyclic()
        try:
            inputs = self._collect_inputs(spec)
        except _DerivationBlocked as blocked:
            return self._publish_unavailable(spec, blocked)
        loader = self._loaders[resource_id]
        try:
            payload = _invoke_loader(loader, inputs)
        except (TypeError, ValueError) as error:
            return self._publish_unavailable(
                spec,
                _DerivationBlocked("loader-error", f"语义 loader 失败: {error}"),
            )
        if not payload.available:
            return self._publish_unavailable(
                spec,
                _DerivationBlocked(
                    "loader-error",
                    payload.error or "语义 loader 返回 unavailable",
                ),
            )
        try:
            revision = sha256_jcs(payload.payload)
        except ValueError as error:
            raise ValueError(
                f"语义 loader 的 payload 不是 JSON value: resource_id={resource_id}"
            ) from error
        previous = self._previous_valid(resource_id)
        if previous is not None and previous.revision == revision:
            # facet payload 未变:不推进语义 revision,不重新发布。
            return previous
        self._generation += 1
        snapshot = ResourceSnapshot(
            resource_id=resource_id,
            resource_kind=spec.resource_kind,
            facet=spec.facet,
            display_uri=spec.display_uri,
            revision=revision,
            payload=payload.payload,
            source_lineage=tuple(sorted(
                (identity, item.revision) for identity, item in inputs.items()
            )),
            generation=self._generation,
        )
        return self._registry.publish(snapshot)

    def publish_all(self) -> tuple[ResourceSnapshot, ...]:
        """对全部已注册语义资源做一次有界派生(不扫描、不递归文件系统)。"""
        return tuple(
            self.publish(resource_id) for resource_id in tuple(self._specs)
        )

    def _collect_inputs(
        self, spec: SemanticResourceDescriptor,
    ) -> dict[str, SemanticInput]:
        inputs: dict[str, SemanticInput] = {}
        for source_id in spec.source_ids:
            try:
                observed = self._reconciler.observed_revision(source_id)
            except KeyError as error:
                raise _DerivationBlocked(
                    "dependency-missing",
                    f"来源尚未 observe: source_id={source_id}",
                ) from error
            if not getattr(observed, "available", False):
                raise _DerivationBlocked(
                    "source-unavailable",
                    "来源 revision 不可用: "
                    + f"source_id={source_id} error={getattr(observed, 'error', None)}",
                )
            inputs[source_id] = SemanticInput(
                identity=source_id,
                revision=observed.revision,
                generation=self._generation,
                content=observed.content,
            )
        for dependency_id in spec.dependency_resource_ids:
            try:
                dependency = self._registry.snapshot(dependency_id)
            except KeyError as error:
                raise _DerivationBlocked(
                    "dependency-missing",
                    f"依赖语义资源尚未发布: resource_id={dependency_id}",
                ) from error
            if not dependency.available:
                raise _DerivationBlocked(
                    "dependency-unavailable",
                    "依赖语义资源不可用: "
                    + f"resource_id={dependency_id} error={dependency.error}",
                )
            self._ensure_dependency_fresh(dependency_id, dependency)
            inputs[dependency_id] = SemanticInput(
                identity=dependency_id,
                revision=dependency.revision,
                generation=dependency.generation,
                content=dependency.payload,
            )
        if not inputs:
            raise _DerivationBlocked(
                "dependency-missing",
                f"语义资源没有任何依赖输入: resource_id={spec.resource_id}",
            )
        return inputs

    def _ensure_dependency_fresh(
        self, dependency_id: str, dependency: ResourceSnapshot,
    ) -> None:
        """混合代际守卫:依赖快照的 lineage 必须等于上游来源当前 revision。"""
        dependency_spec = self._specs.get(dependency_id)
        if dependency_spec is None:
            return
        lineage = dict(dependency.source_lineage)
        for upstream_source in dependency_spec.source_ids:
            try:
                current = self._reconciler.observed_revision(upstream_source).revision
            except KeyError:
                current = None
            if lineage.get(upstream_source) != current:
                raise _DerivationBlocked(
                    "generation-mismatch",
                    "依赖快照落后于来源当前 revision: "
                    + f"resource_id={dependency_id} source_id={upstream_source}",
                )


    def _publish_unavailable(
        self, spec: SemanticResourceDescriptor, blocked: _DerivationBlocked
    ) -> ResourceSnapshot:
        previous = self._previous_valid(spec.resource_id)
        retained = previous.revision if previous is not None else None
        snapshot = ResourceSnapshot(
            resource_id=spec.resource_id,
            resource_kind=spec.resource_kind,
            facet=spec.facet,
            display_uri=spec.display_uri,
            revision=retained or "",
            payload=None,
            source_lineage=previous.source_lineage if previous is not None else (),
            generation=self._generation,
            available=False,
            error=blocked.message,
            error_code=blocked.error_code,
            retained_revision=retained,
        )
        return self._registry.publish(snapshot)

    def _previous_valid(self, resource_id: str) -> ResourceSnapshot | None:
        try:
            snapshot = self._registry.snapshot(resource_id)
        except KeyError:
            return None
        return snapshot if snapshot.available else None

    def _ensure_acyclic(self) -> None:
        """对全部已注册规范做 DFS 无环校验;依赖成环显式失败。"""
        state: dict[str, int] = {}

        def visit(resource_id: str, trail: tuple[str, ...]) -> None:
            marker = state.get(resource_id)
            if marker == 1:
                raise ValueError(
                    "语义依赖成环: " + " -> ".join((*trail, resource_id))
                )
            if marker == 2:
                return
            spec = self._specs.get(resource_id)
            if spec is None:
                return
            state[resource_id] = 1
            for dependency_id in spec.dependency_resource_ids:
                visit(dependency_id, (*trail, resource_id))
            state[resource_id] = 2

        for resource_id in tuple(self._specs):
            visit(resource_id, ())
