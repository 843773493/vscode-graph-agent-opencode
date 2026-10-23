"""无环 ResourceDerivationGraph:来源 revision → 语义 facet 快照。

graph 只消费 SourceReconciler 已发布的 ObservedSourceRevision 与
ResourceRegistry 已发布的快照(经 Protocol 解耦,不直接调用 reader);
按代码内注册的 derivation 规范驱动版本化 loader,做语义 diff 与 CAS
发布。依赖缺失/成环、混合 generation、来源 unavailable 都显式失败
并保留旧 valid 快照。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticInput,
    SemanticLoader,
    SemanticResourceDescriptor,
)

if TYPE_CHECKING:
    from app.services.infrastructure.resource_platform.registry.semantic_registry import (
        ResourceRegistry,
    )


@runtime_checkable
class AcceptedSourceRevision(Protocol):
    """来源 owner 已接受的最小不可变值，不包含 locator 或读取能力。"""

    @property
    def revision(self) -> str: ...

    @property
    def content(self) -> object: ...

    @property
    def available(self) -> bool: ...

    @property
    def error(self) -> str | None: ...


@runtime_checkable
class ObservedRevisionSource(Protocol):
    """graph 需要的 reconciler 窄接口;不暴露 reader 与 locator。"""

    def observed_revision(self, source_id: str) -> AcceptedSourceRevision:
        """返回已发布的不可变来源 revision;未 observe 过时抛 KeyError。"""
        ...


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
      语义 revision；lineage 和可用性仍按完整值发布(CAS 幂等)。
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
        self._topological_order_cache: tuple[str, ...] | None = None
        # 重建图仍延续同一 Registry 的发布代际，不能让来源刷新回退至零。
        self._generation = max(
            (snapshot.generation for snapshot in registry.snapshots()),
            default=0,
        )

    def register(
        self,
        spec: SemanticResourceDescriptor,
        loader: SemanticLoader,
        *,
        expected: SemanticResourceDescriptor | None = None,
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
        previous_order = self._topological_order_cache
        self._specs[spec.resource_id] = spec
        self._topological_order_cache = None
        try:
            self._topological_order()
            self._registry.register_descriptor(spec, expected=expected)
        except BaseException:
            del self._specs[spec.resource_id]
            self._topological_order_cache = previous_order
            raise
        self._loaders[spec.resource_id] = loader

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
        self._topological_order()
        return self._publish_registered_spec(spec)

    def _publish_registered_spec(
        self,
        spec: SemanticResourceDescriptor,
    ) -> ResourceSnapshot:
        resource_id = spec.resource_id
        try:
            inputs = self._collect_inputs(spec)
        except _DerivationBlocked as blocked:
            return self._publish_unavailable(spec, blocked)
        loader = self._loaders[resource_id]
        payload = loader(inputs)
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
        lineage = tuple(
            sorted((identity, item.revision) for identity, item in inputs.items())
        )
        current = self._current_snapshot(resource_id)
        if (
            current is not None
            and current.available
            and current.revision == revision
            and current.source_lineage == lineage
        ):
            return current
        self._generation += 1
        snapshot = ResourceSnapshot(
            resource_id=resource_id,
            resource_kind=spec.resource_kind,
            facet=spec.facet,
            display_uri=spec.display_uri,
            revision=revision,
            payload=payload.payload,
            source_lineage=lineage,
            generation=self._generation,
        )
        return self._registry.publish(snapshot)

    def publish_all(self) -> tuple[ResourceSnapshot, ...]:
        """对全部已注册语义资源做一次有界派生(不扫描、不递归文件系统)。"""
        with self._registry.publication():
            try:
                order = self._topological_order()
                return tuple(
                    self._publish_registered_spec(self._specs[resource_id])
                    for resource_id in order
                )
            except Exception as error:
                # 部分 facet 已推进时也必须先阻断整批 required，再暴露程序缺陷。
                for spec in self._specs.values():
                    self._publish_unavailable(
                        spec,
                        _DerivationBlocked(
                            "loader-error",
                            f"语义派生图求值失败: {type(error).__name__}",
                        ),
                    )
                raise

    def _collect_inputs(
        self,
        spec: SemanticResourceDescriptor,
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
            if not observed.available:
                raise _DerivationBlocked(
                    "source-unavailable",
                    f"来源 revision 不可用: source_id={source_id}",
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
        self,
        dependency_id: str,
        dependency: ResourceSnapshot,
    ) -> None:
        """混合代际守卫:依赖快照的 lineage 必须等于上游来源当前 revision。"""
        dependency_spec = self._specs.get(dependency_id)
        if dependency_spec is None:
            return
        lineage = dict(dependency.source_lineage)
        for upstream_source in dependency_spec.source_ids:
            try:
                source = self._reconciler.observed_revision(upstream_source)
            except KeyError:
                source = None
            if (
                source is None
                or not source.available
                or lineage.get(upstream_source) != source.revision
            ):
                raise _DerivationBlocked(
                    "generation-mismatch",
                    "依赖快照落后于来源当前 revision: "
                    + f"resource_id={dependency_id} source_id={upstream_source}",
                )
        for upstream_id in dependency_spec.dependency_resource_ids:
            current = self._current_snapshot(upstream_id)
            if (
                current is None
                or not current.available
                or (lineage.get(upstream_id) != current.revision)
            ):
                raise _DerivationBlocked(
                    "generation-mismatch",
                    f"依赖快照落后于上游发布: resource_id={dependency_id} dependency={upstream_id}",
                )
            self._ensure_dependency_fresh(upstream_id, current)

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
            return self._registry.last_valid_snapshot(resource_id)
        except KeyError:
            return None

    def _current_snapshot(self, resource_id: str) -> ResourceSnapshot | None:
        try:
            return self._registry.snapshot(resource_id)
        except KeyError:
            return None

    def _topological_order(self) -> tuple[str, ...]:
        """返回注册状态对应的拓扑序，注册成功前 DFS 无环校验一次。"""
        if self._topological_order_cache is None:
            self._topological_order_cache = self._compute_topological_order()
        return self._topological_order_cache

    def _compute_topological_order(self) -> tuple[str, ...]:
        """对全部已注册规范做 DFS 无环校验;依赖成环显式失败。"""
        state: dict[str, int] = {}
        ordered: list[str] = []

        def visit(resource_id: str, trail: tuple[str, ...]) -> None:
            marker = state.get(resource_id)
            if marker == 1:
                raise ValueError("语义依赖成环: " + " -> ".join((*trail, resource_id)))
            if marker == 2:
                return
            spec = self._specs.get(resource_id)
            if spec is None:
                return
            state[resource_id] = 1
            for dependency_id in spec.dependency_resource_ids:
                visit(dependency_id, (*trail, resource_id))
            state[resource_id] = 2
            ordered.append(resource_id)

        for resource_id in tuple(self._specs):
            visit(resource_id, ())
        return tuple(ordered)
