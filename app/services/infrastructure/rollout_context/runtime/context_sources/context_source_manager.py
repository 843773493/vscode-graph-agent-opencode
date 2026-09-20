"""上下文来源的最小生命周期协调器。

该模块不负责发现文件或写入 ContextStore。文件监视器、Gateway snapshot
owner 或权威内存 owner 只需把已登记来源的最新正文通过 ``observe`` 送入；
ContextSourceManager 负责 snapshot/tracked/untrack 的状态、revision 和把
多个尚未发送变化合并成一个尾部 user item。

控制状态（tracked registration、observed/applied revision 和 untrack/frozen）
通过 :class:`ContextSourceControlStatePort` 持久化到唯一的
RolloutCheckpointSaver/ContextStore owner；本模块不执行任何 I/O。

来源变化可以有两类入口：受信 owner 直接调用 ``observe`` 送入正文，或由资源
观察通道先调用 :meth:`ContextSourceManager.mark_pending_observation` 只登记
「某来源有新 revision」。后者让事件回调保持零 I/O，正文仍由 owner 的权威内存
快照在 :meth:`ContextSourceManager.next_pending_observation` 之后提供。
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

# OpenSpec 3.8-C：commit/untrack 成功边界把轻量事件发布到 context.source/*
# channel；事件只是内存通知，不是 durable 事实，也不替代控制状态持久化。
from app.services.infrastructure.events.channel_events import ContextSourceEvent
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
    ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    SourceObservation,
    build_source_lifecycle_decision,
)

SkillLoadMode = Literal["snapshot", "tracked", "untrack"]


@dataclass(frozen=True, slots=True)
class ContextSourceDescriptor:
    """来源的内部描述；locator 只存在于服务端。"""

    source_id: str
    source_kind: str
    name: str
    description: str
    internal_locator: str
    resource_uri: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_id",
            "source_kind",
            "name",
            "internal_locator",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ContextSourceDescriptor.{field_name} 必须是非空字符串")
        if not isinstance(self.description, str):
            raise TypeError("ContextSourceDescriptor.description 必须是字符串")
        if self.resource_uri is not None and (
            not isinstance(self.resource_uri, str) or not self.resource_uri
        ):
            raise ValueError(
                "ContextSourceDescriptor.resource_uri 必须是非空字符串或 None"
            )


@dataclass(frozen=True, slots=True)
class PendingSourceObservation:
    """等待 CSM 消费的一条轻量 observation 标记。

    该值只描述「某来源需要读取哪个 revision」；正文始终由来源 owner 的权威
    内存快照提供，CSM 不执行 I/O。``revision is None`` 表示只知道「该来源可能
    有新事实」，由消费方按权威快照对账（用于通知丢失后的全量 reconcile）。
    """

    source_id: str
    revision: str | None


@dataclass(frozen=True, slots=True)
class ContextSourceDelta:
    """准备交给唯一 ContextStore owner 的上下文尾部 item。

    previous_revision 是 from 基准，恒等于提交时刻的 latest visible
    committed revision；content_hash 是注入载荷（diff 或完整正文）的
    确定性 hash；observation_provenance 按观察顺序记录被合并进本 delta
    的 observed revisions，不包含 from 基准本身。多个 pending
    observation 在提交前合并为唯一 delta，不产生中间 context item。
    """

    source_id: str
    source_name: str
    source_kind: str
    revision: str
    previous_revision: str | None
    content: str
    content_hash: str
    observation_provenance: tuple[str, ...] = ()
    wire_role: Literal["user"] = "user"
    kind: Literal["activation", "delta", "rebuild"] = "activation"


SkillLoadStatus = Literal["loaded", "already_active", "not_tracked"]
SkillLoadAppendStatus = Literal["appended", "already_active", "none"]


class ContextSourceTrackingStateConflict(RuntimeError):
    """同一 (owner thread, normalized name) 存在多个 active registration。

    OpenSpec 3.14 的闭合错误合同：检测到重复 active tracked registration
    时 fail closed，code 固定为 tracking-state-conflict，不静默挑选
    其中一个 registration，也不伪造成功状态。
    """

    code = "tracking-state-conflict"


class SkillCatalogSnapshotConflict(RuntimeError):
    """skill_load 的冻结 SkillCatalog binding 与运行态不一致。

    OpenSpec 4.3 闭合错误合同：冻结 Turn/ModelCall SkillCatalogSnapshot
    缺失、binding 与已登记来源不一致或 activation revision 与 live 状态
    冲突时 fail closed，code 固定为 skill-catalog-snapshot-conflict，
    不回退读盘，也不静默改用当前 catalog。
    """

    code = "skill-catalog-snapshot-conflict"


def _normalize_skill_name(name: str) -> str:
    """Skill 逻辑名的唯一归一化：剥离首尾空白；大小写保持敏感。

    与虚拟资源 grammar 的固定 segment 一致，catalog 名称大小写敏感，
    normalized name 只做空白归一，避免同一 registration 出现两个索引键。
    """
    return name.strip()


@dataclass(frozen=True, slots=True)
class SkillLoadReceipt:
    """skill_load 的脱敏返回值，不包含 locator、credential 或正文。

    ``display_uri`` 是模型可见的 boxteam:// 虚拟资源 URI，属于
    ResourceProvenance 而非 Skill metadata；未绑定 descriptor 的恢复
    registration 返回 None，不得伪造 URI。``revision`` 是 source/semantic
    revision（当前为内容寻址 sha256，catalog revision producer 落地后两者
    分离）；``content_hash`` 是精确正文的 hash 标识，永远不含正文本身。
    """

    name: str
    mode: SkillLoadMode
    revision: str | None
    queued: bool
    tracked: bool
    display_uri: str | None = None
    content_hash: str | None = None
    status: SkillLoadStatus = "loaded"
    append_status: SkillLoadAppendStatus = "none"


@dataclass(frozen=True, slots=True)
class SkillCatalogBinding:
    """冻结 SkillCatalog activation binding。

    body 是 D1 发布的 activation facet 精确正文（frontmatter 之后），
    冻结进 binding 后 skill_load 在工具路径零 I/O 解析；模型可见结果
    永远不含 body。
    """

    name: str
    resource_id: str
    entry_identity: str
    display_uri: str
    activation_revision: str
    body: str
    content_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "name",
            "resource_id",
            "entry_identity",
            "display_uri",
            "activation_revision",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"SkillCatalogBinding.{field_name} 必须是非空字符串"
                )
        if not isinstance(self.body, str):
            raise TypeError("SkillCatalogBinding.body 必须是字符串")
        object.__setattr__(
            self,
            "content_hash",
            "sha256:" + hashlib.sha256(self.body.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class SkillCatalogActivationSnapshot:
    """冻结在 Turn/ModelCall 边界的 SkillCatalog activation snapshot。

    snapshot/tracked 只从本 snapshot 的 exact binding 解析；同名 catalog
    新 revision 不会改变已冻结调用，untrack 也不读本 snapshot。
    """

    catalog_revision: str
    entries: Mapping[str, SkillCatalogBinding]

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_revision, str) or not self.catalog_revision:
            raise ValueError(
                "SkillCatalogActivationSnapshot.catalog_revision 必须是非空字符串"
            )
        if not isinstance(self.entries, Mapping):
            raise TypeError("SkillCatalogActivationSnapshot.entries 必须是 Mapping")
        for name, binding in self.entries.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "SkillCatalogActivationSnapshot entry 名必须是非空字符串"
                )
            if binding.name != name:
                raise ValueError(
                    "SkillCatalogActivationSnapshot entry 名与 binding.name 不一致: "
                    f"{name!r} != {binding.name!r}"
                )
@dataclass(frozen=True, slots=True)
class PendingContextSourceBatch:
    """一次待提交的来源变更准备结果。

    prepare_pending 只读：不推进 applied revision，不改变任何持久状态。
    原子提交走 commit_model_call_pending：先经唯一 durable 端口逐条持久化
    提交后控制状态，全部成功后才推进内存 applied、清空 pending 并发布事件；
    重复提交同一批次复用同一结果。
    """

    deltas: tuple[ContextSourceDelta, ...]


@dataclass(frozen=True, slots=True)
class CommittedContextSourceBatch:
    """一次 model_call pending 提交的结果；重复提交复用同一结果对象。"""

    deltas: tuple[ContextSourceDelta, ...]


@dataclass(slots=True)
class _SourceState:
    """一个已登记来源的运行时状态。

    ``descriptor`` 为 ``None`` 表示该 registration 由持久化控制状态恢复，
    但 source owner 还没有用当前 catalog descriptor 重新绑定 locator；此时
    identity/revision 仍然有效，只是不能解析内部 locator。
    """

    source_id: str
    source_kind: str
    name: str
    descriptor: ContextSourceDescriptor | None = None
    description: str | None = None
    binding_revision: str | None = None
    tracking_status: ContextSourceTrackingStatus = "untracked"
    latest_visible_committed_revision: str | None = None
    latest_visible_committed_content: str | None = None
    latest_revision: str | None = None
    latest_content: str | None = None
    observed_revisions: list[str] = field(default_factory=list)
    pending_kind: Literal["activation", "delta", "rebuild"] | None = None
    persisted_state_revision: int = 0
    persisted_fields: tuple[object, ...] | None = None

    @property
    def tracked(self) -> bool:
        return self.tracking_status == "tracked"

    def bind_descriptor(self, descriptor: ContextSourceDescriptor) -> None:
        """把 Registry 重建的 descriptor 绑定回已恢复的 registration。"""
        if self.source_kind != descriptor.source_kind or self.name != descriptor.name:
            raise ValueError(
                "ContextSourceManager source identity 冲突: "
                f"source_id={descriptor.source_id} "
                f"registered_kind={self.source_kind} requested_kind={descriptor.source_kind} "
                f"registered_name={self.name} requested_name={descriptor.name}"
            )
        current = self.descriptor
        if current is not None and current != descriptor:
            raise ValueError(
                "ContextSourceManager source descriptor 冲突: "
                f"source_id={descriptor.source_id}"
            )
        self.descriptor = descriptor
        self.description = descriptor.description

    def has_durable_interest(self) -> bool:
        """从未激活/跟踪的目录注册不是需要跨重启保留的控制状态。"""
        return (
            self.tracking_status == "tracked"
            or self.latest_visible_committed_revision is not None
            or self.latest_revision is not None
            or self.persisted_state_revision > 0
        )

    @classmethod
    def from_control_state(cls, stored: ContextSourceControlState) -> _SourceState:
        """从持久化控制状态恢复 registration。

        tracked 且 latest_revision 为空是合法状态：registration 已登记但首帧
        观察尚未提交（注册后、首帧前重启）；恢复后由下一次 observation 产生
        activation 首帧，无需回填。
        """
        return cls(
            source_id=stored.source_id,
            source_kind=stored.source_kind,
            name=stored.name,
            binding_revision=stored.binding_revision,
            tracking_status=stored.tracking_status,
            latest_visible_committed_revision=stored.latest_visible_committed_revision,
            latest_revision=stored.latest_revision,
            persisted_state_revision=stored.state_revision,
            persisted_fields=stored.durable_fields(),
        )


class ContextSourceManager:
    """管理已注册 source 的激活状态，不拥有 watcher 或持久化 writer。

    ``owner`` 与 ``control_state_port`` 必须同时提供：owner 是精确
    ``(session_id, thread_id)``，端口是唯一 ContextStore owner 暴露的控制状态
    读写能力。两者省略时 CSM 是纯内存对象（不满足重启恢复合同）。
    """

    def __init__(
        self,
        *,
        owner: ContextSourceOwnerKey | None = None,
        control_state_port: ContextSourceControlStatePort | None = None,
        mutation_intent_port: object | None = None,
        lifecycle_event_sink: Callable[[ContextSourceEvent], None] | None = None,
    ) -> None:
        if (owner is None) != (control_state_port is None):
            raise ValueError(
                "ContextSourceManager 的 owner 与 control_state_port 必须同时提供或同时省略"
            )
        self._owner = owner
        self._control_state_port = control_state_port
        if mutation_intent_port is not None and owner is None:
            raise ValueError(
                "ContextSourceManager 的 mutation_intent_port 需要 owner"
            )
        self._mutation_intent_port = mutation_intent_port
        # OpenSpec 3.8-C：commit/untrack 成功边界的轻量事件出口（可选）。
        # OpenSpec 2.4-B4：before_model 已直接调用唯一原子实现
        # commit_model_call_pending，迁移入口已物理删除。
        self._lifecycle_event_sink = lifecycle_event_sink
        # 最近一次 model_call pending 提交结果：重复提交同一批次时原样复用，
        # 不重复推进 applied，也不重复发布事件。
        self._last_model_call_receipt: CommittedContextSourceBatch | None = None
        self._sources: dict[str, _SourceState] = {}
        self._source_ids_by_name: dict[str, str] = {}
        self._pending: dict[str, ContextSourceDelta] = {}
        # 冻结的 Turn/ModelCall SkillCatalog activation snapshot；
        # skill_load 的 snapshot/tracked 只从这里解析 exact binding。
        self._skill_activation_snapshot: SkillCatalogActivationSnapshot | None = None
        # 待观察标记按 source_id 去重：同一来源只保留一个最新标记。
        self._pending_observations: dict[str, PendingSourceObservation] = {}
        if owner is not None and control_state_port is not None:
            self._restore_control_states(owner, control_state_port)

    def _restore_control_states(
        self,
        owner: ContextSourceOwnerKey,
        port: ContextSourceControlStatePort,
    ) -> None:
        """进程重启/agent 重建时从 owner 恢复 tracked 与 untrack 状态。

        只恢复 identity、catalog 绑定 revision 和 revision 事实；pending
        delta 与正文由下一次 observation/reconciliation 重新产生。
        """
        for stored in port.load_context_source_control_states(owner):
            if stored.owner != owner:
                raise RuntimeError(
                    "ContextSourceManager 恢复的控制状态 owner 不匹配: "
                    f"expected=({owner.session_id},{owner.thread_id}) "
                    f"actual=({stored.owner.session_id},{stored.owner.thread_id})"
                )
            if stored.source_id in self._sources:
                raise RuntimeError(
                    "ContextSourceManager 持久化控制状态存在重复 source_id: "
                    f"source_id={stored.source_id}"
                )
            self._sources[stored.source_id] = _SourceState.from_control_state(stored)
            normalized_name = _normalize_skill_name(stored.name)
            if stored.tracking_status == "tracked":
                duplicates = sorted(
                    other.source_id
                    for other in self._sources.values()
                    if other.source_id != stored.source_id
                    and other.tracked
                    and _normalize_skill_name(other.name) == normalized_name
                )
                if duplicates:
                    raise ContextSourceTrackingStateConflict(
                        "tracking-state-conflict: 持久化控制状态存在同 normalized "
                        f"name 的多个 active registration: name={normalized_name} "
                        f"source_ids={duplicates + [stored.source_id]}"
                    )
            # 同名 registration 按恢复（created_at）顺序让最新者持有名称索引；
            # active tracked registration 的定位不依赖该索引（untrack 走扫描）。
            self._source_ids_by_name[normalized_name] = stored.source_id

    def register(
        self,
        descriptor: ContextSourceDescriptor,
        *,
        binding_revision: str | None = None,
        tracking_status: ContextSourceTrackingStatus = "untracked",
    ) -> None:
        """登记来源；恢复后的 registration 在这里重新绑定 Registry descriptor。

        ``tracking_status`` 允许受信来源（如 AGENTS.md）以 tracked 身份登记，
        使其首帧与后续 delta 都能走事件驱动的 pending 路径；已存在的
        registration 重新绑定时保持原 tracking 状态，不因重复注册降级。

        TODO: catalog/来源绑定 revision 目前没有生产 producer（SkillCatalog
        revision 发布属于 OpenSpec 3.3/4.1-A）。落地后必须由 source owner
        传入 ``binding_revision``，不得用内容 revision 冒充目录绑定 revision。
        """
        if binding_revision is not None and (
            not isinstance(binding_revision, str) or not binding_revision
        ):
            raise ValueError("register.binding_revision 必须是非空字符串或 None")
        if tracking_status not in {"tracked", "untracked"}:
            raise ValueError(
                "register.tracking_status 只能是 tracked 或 untracked: "
                f"{tracking_status!r}"
            )
        state = self._sources.get(descriptor.source_id)
        if state is None:
            state = _SourceState(
                source_id=descriptor.source_id,
                source_kind=descriptor.source_kind,
                name=descriptor.name,
                descriptor=descriptor,
                description=descriptor.description,
                binding_revision=binding_revision,
                tracking_status=tracking_status,
            )
            self._sources[descriptor.source_id] = state
        else:
            state.bind_descriptor(descriptor)
            if binding_revision is not None:
                state.binding_revision = binding_revision
        # 名称索引保持最近一次 catalog 注册顺序；同名高优先级 entry 覆盖
        # 索引但不自动改绑既有 active registration（tasks 3.3-A），
        # untrack 仍按 active tracked registration 定位原 registration。
        normalized_name = _normalize_skill_name(descriptor.name)
        self._source_ids_by_name.pop(normalized_name, None)
        self._source_ids_by_name[normalized_name] = descriptor.source_id
        self._persist_control_state(state)

    def metadata(self) -> tuple[Mapping[str, str], ...]:
        """仅返回模型可见的 name/description，不返回路径。"""
        result: list[Mapping[str, str]] = []
        for state in self._registered_states():
            descriptor = state.descriptor
            if descriptor is None:
                continue
            result.append(
                {"name": descriptor.name, "description": descriptor.description}
            )
        return tuple(result)

    def descriptors(self) -> tuple[ContextSourceDescriptor, ...]:
        """返回已注册来源的内部 descriptor，供受信 source owner 对账。"""
        return tuple(
            state.descriptor
            for state in self._registered_states()
            if state.descriptor is not None
        )

    def install_skill_activation_snapshot(
        self,
        snapshot: SkillCatalogActivationSnapshot,
    ) -> None:
        """在 activation boundary 安装冻结的 SkillCatalog activation snapshot。

        只能由 source owner（Skill middleware/factory 装配）在边界调用。
        本方法只冻结当前 catalog 事实，不做 registration 对账；binding 与
        live 来源的不一致由 load 解析路径 fail closed，同名高优先级覆盖
        （rebind 前夕）允许安装。
        """
        if not isinstance(snapshot, SkillCatalogActivationSnapshot):
            raise TypeError(
                "install_skill_activation_snapshot 需要 SkillCatalogActivationSnapshot"
            )
        self._skill_activation_snapshot = snapshot

    def _resolve_activation_binding(self, name: str) -> SkillCatalogBinding:
        """从冻结 snapshot 解析 exact binding；缺失/不一致 fail closed。"""
        snapshot = self._skill_activation_snapshot
        if snapshot is None:
            raise SkillCatalogSnapshotConflict(
                "skill-catalog-snapshot-conflict: 尚无冻结 SkillCatalog "
                "activation snapshot；snapshot/tracked 只能从冻结 snapshot 解析"
            )
        binding = snapshot.entries.get(name)
        if binding is None:
            raise KeyError(f"Skill 不存在: name={name}")
        state = self._sources.get(f"skill:{binding.name}")
        if (
            state is not None
            and state.descriptor is not None
            and state.descriptor.resource_uri is not None
            and state.descriptor.resource_uri != binding.display_uri
        ):
            raise SkillCatalogSnapshotConflict(
                "skill-catalog-snapshot-conflict: 冻结 binding 与已登记来源不一致: "
                f"name={name} frozen={binding.display_uri} "
                f"registered={state.descriptor.resource_uri}"
            )
        return binding

    def load_skill(self, name: str, mode: SkillLoadMode = "snapshot") -> SkillLoadReceipt:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("skill_load.name 必须是非空字符串")
        if mode not in {"snapshot", "tracked", "untrack"}:
            raise ValueError("skill_load.mode 只能是 snapshot、tracked 或 untrack")
        normalized = _normalize_skill_name(name)
        source_id = self._source_ids_by_name.get(normalized)
        if source_id is None:
            raise KeyError(f"Skill 不存在: name={normalized}")
        state = self._sources[source_id]
        display_uri = (
            state.descriptor.resource_uri if state.descriptor is not None else None
        )
        if mode == "untrack":
            # untrack 按唯一 active tracked registration 定位，不按 catalog
            # 优先级重新解析 entry（design 5.1）：同名高优先级 entry 出现后
            # 仍能停止原 tracking，也不读取 source。
            tracked_states = [
                candidate
                for candidate in self._sources.values()
                if candidate.tracked
                and _normalize_skill_name(candidate.name) == normalized
            ]
            if len(tracked_states) > 1:
                raise ContextSourceTrackingStateConflict(
                    "tracking-state-conflict: 同 normalized name 存在多个 "
                    f"active registration: name={normalized} "
                    f"source_ids={sorted(item.source_id for item in tracked_states)}"
                )
            target = tracked_states[0] if tracked_states else None
            if target is not None:
                target.tracking_status = "untracked"
                # untrack(frozen) 状态必须跨重启保留：重启后不得恢复跟踪或重新注入。
                self._persist_control_state(target)
                # OpenSpec 3.8-C：untrack 成功边界发布轻量事件（内存通知）。
                self._publish_lifecycle_event(
                    source_id=target.source_id,
                    source_kind=target.source_kind,
                    kind="untracked",
                    revision=target.latest_revision,
                )
                display_uri = (
                    target.descriptor.resource_uri
                    if target.descriptor is not None
                    else None
                )
            # 无 active registration 时返回确定性 not_tracked：不改状态、
            # 不发布事件、不伪造成功；revision 事实仍取定位到的 registration
            # （未命中时为当前 catalog entry）。
            resolved = target if target is not None else state
            return SkillLoadReceipt(
                name=name,
                mode=mode,
                revision=resolved.latest_revision,
                queued=False,
                tracked=False,
                display_uri=display_uri,
                content_hash=(
                    _revision(resolved.latest_content)
                    if resolved.latest_content is not None
                    else None
                ),
                status="loaded" if target is not None else "not_tracked",
                append_status="none",
            )

        # snapshot/tracked 只从冻结 Turn/ModelCall SkillCatalogSnapshot 的
        # exact binding 解析；同名 catalog 新 revision 不影响已冻结调用。
        binding = self._resolve_activation_binding(normalized)
        if state.latest_revision is None:
            self.activate_skill_content(
                normalized,
                binding.body,
                revision=binding.activation_revision,
            )
        elif (
            state.latest_revision != binding.activation_revision
        ):
            raise SkillCatalogSnapshotConflict(
                "skill-catalog-snapshot-conflict: 冻结 binding 与 live activation "
                f"状态不一致: name={normalized} frozen={binding.activation_revision} "
                f"live={state.latest_revision}"
            )
        if mode == "tracked":
            conflicts = sorted(
                candidate.source_id
                for candidate in self._sources.values()
                if candidate.source_id != source_id
                and candidate.tracked
                and _normalize_skill_name(candidate.name) == normalized
            )
            if conflicts:
                raise ContextSourceTrackingStateConflict(
                    "tracking-state-conflict: 同 normalized name 已有 active "
                    f"registration: name={normalized} source_ids={conflicts}"
                )
            state.tracking_status = "tracked"
            self._persist_control_state(state)
        if source_id in self._pending:
            status: SkillLoadStatus = "loaded"
            append_status: SkillLoadAppendStatus = "appended"
        elif state.latest_visible_committed_revision == state.latest_revision:
            # 同 source/revision 已在 active view 可见：不追加第二个 item。
            status = "already_active"
            append_status = "already_active"
        else:
            raise RuntimeError(
                "Skill revision 状态不一致：既无待提交 item 也未 applied；"
                f"name={normalized} latest={state.latest_revision} "
                f"applied={state.latest_visible_committed_revision}"
            )
        return SkillLoadReceipt(
            name=normalized,
            mode=mode,
            revision=binding.activation_revision,
            queued=source_id in self._pending,
            tracked=state.tracked,
            display_uri=binding.display_uri,
            content_hash=binding.content_hash,
            status=status,
            append_status=append_status,
        )

    def descriptor_for_skill(self, name: str) -> ContextSourceDescriptor:
        """只给受信 source owner 解析内部 descriptor，不进入模型协议。"""
        source_id = self._source_ids_by_name.get(name)
        if source_id is None:
            raise KeyError(f"Skill 不存在: name={name}")
        descriptor = self._sources[source_id].descriptor
        if descriptor is None:
            raise RuntimeError(
                "Skill 来源尚未由 source owner 重新注册，不能解析内部 locator: "
                f"name={name}"
            )
        return descriptor

    def activate_skill_content(
        self,
        name: str,
        content: str,
        *,
        revision: str | None = None,
    ) -> None:
        """接收 source owner 已验证的正文；CSM 不执行 I/O。"""
        descriptor = self.descriptor_for_skill(name)
        state = self._sources[descriptor.source_id]
        if not isinstance(content, str):
            raise TypeError(
                "ContextSourceManager.activate_skill_content.content 必须是字符串"
            )
        effective_revision = revision or _revision(content)
        if state.latest_revision is not None:
            if (
                state.latest_revision == effective_revision
                and state.latest_content == content
            ):
                return
            if state.latest_visible_committed_revision is not None and not state.tracked:
                raise RuntimeError(
                    "snapshot Skill 已激活，变化必须先通过 tracked source observation 提交"
                )
        self._record_observation(state, effective_revision)
        state.latest_revision = effective_revision
        state.latest_content = content
        state.pending_kind = "activation" if state.latest_visible_committed_revision is None else "delta"
        self._queue_delta(state)
        self._persist_control_state(state)

    def observe(self, source_id: str, content: str, *, revision: str | None = None) -> bool:
        """接收已登记来源的新快照；未跟踪来源不会改变上下文。"""
        state = self._sources.get(source_id)
        if state is None:
            raise KeyError(f"Context source 不存在: source_id={source_id}")
        if not isinstance(content, str):
            raise TypeError("ContextSourceManager.observe.content 必须是字符串")
        if not state.tracked:
            return False
        effective_revision = revision or _revision(content)
        if state.latest_revision == effective_revision and state.latest_content == content:
            return False
        if _adopt_restored_baseline(state, content, effective_revision):
            # 重启恢复：该 revision 已在已提交上下文中，只重建 diff 基准。
            return False
        if state.latest_visible_committed_revision is None:
            self._record_observation(state, effective_revision)
            self._apply_content(state, content, kind="activation")
            self._persist_control_state(state)
            return True
        self._record_observation(state, effective_revision)
        state.latest_revision = effective_revision
        state.latest_content = content
        state.pending_kind = "delta"
        self._queue_delta(state)
        self._persist_control_state(state)
        return True

    def mark_pending_observation(
        self,
        source_id: str,
        revision: str | None,
        *,
        reconcile: bool = False,
    ) -> bool:
        """把「来源有新 revision / 需要重新对账」标记为待观察，不读取任何内容。

        资源事件接线只调用本方法：事件回调保持零 I/O，正文仍由来源 owner 的
        内存快照在 :meth:`next_pending_observation` 之后提供。尚未 tracked 的
        来源也会被登记，保证首次 activation 与后续 delta 走同一条事件驱动路径；
        消费者只返回当时仍然 tracked 的来源。

        ``reconcile=True`` 表示通知丢失或无法归属到单一 revision，必须按权威
        快照重新对账；此时允许 ``revision=None``（来源尚未有过已知 revision）。
        同一来源同时只保留一个待观察标记，重复标记不会堆积。
        """
        state = self._sources.get(source_id)
        if state is None:
            raise KeyError(f"Context source 不存在: source_id={source_id}")
        if revision is not None and (not isinstance(revision, str) or not revision):
            raise ValueError(
                "mark_pending_observation.revision 必须是非空字符串或 None"
            )
        if not reconcile:
            if revision is None:
                raise ValueError(
                    "mark_pending_observation 非 reconcile 标记必须提供 revision"
                )
            if revision in {state.latest_revision, state.latest_visible_committed_revision}:
                return False
        marker = PendingSourceObservation(
            source_id=source_id,
            revision=None if reconcile else revision,
        )
        if self._pending_observations.get(source_id) == marker:
            return False
        self._pending_observations[source_id] = marker
        return True

    def mark_all_pending_observations(
        self,
        source_ids: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        """把指定（默认全部）已注册来源标记为「需要按权威快照重新对账」。

        用于资源通知丢失（gap）后的全量 reconcile：只登记来源 identity，不读取
        正文、不要求已知 revision；返回本次新标记的 source_id。
        """
        candidates = tuple(self._sources) if source_ids is None else source_ids
        marked: list[str] = []
        for source_id in candidates:
            if source_id not in self._sources:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if self.mark_pending_observation(source_id, None, reconcile=True):
                marked.append(source_id)
        return tuple(marked)

    def next_pending_observation(self) -> PendingSourceObservation | None:
        """取出一条最早的待观察标记；未跟踪来源的标记在这里被丢弃。"""
        while self._pending_observations:
            source_id, pending = next(iter(self._pending_observations.items()))
            del self._pending_observations[source_id]
            state = self._sources.get(source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={source_id}")
            if not state.tracked:
                continue
            return pending
        return None

    def pending_observation_count(self) -> int:
        return len(self._pending_observations)

    def source_observation_state(
        self,
        source_id: str,
    ) -> tuple[ContextSourceTrackingStatus, str | None]:
        """返回来源的 tracking 状态与最新已知 revision，供 owner 对账。"""
        state = self._sources.get(source_id)
        if state is None:
            raise KeyError(f"Context source 不存在: source_id={source_id}")
        return state.tracking_status, state.latest_revision

    def source_id_for_resource_uri(self, resource_uri: str) -> str | None:
        """按来源虚拟 identity 反查 source_id；locator/物理路径不参与匹配。"""
        if not isinstance(resource_uri, str) or not resource_uri:
            raise ValueError("source_id_for_resource_uri 需要非空 resource_uri")
        for state in self._sources.values():
            descriptor = state.descriptor
            if descriptor is not None and descriptor.resource_uri == resource_uri:
                return state.source_id
        return None

    def restore_active_revision(self, source_id: str, active_revision: str | None) -> bool:
        """rewind 后发现最新注入不在 active view 时重排当前有效正文。

        TODO: OpenSpec 3.6 要求 rewind/compaction 按目标 checkpoint 恢复
        tracking state（snapshot/untracked 不恢复）。当前 checkpoint 恢复路径
        还没有调用本方法，也没有 checkpoint-versioned 控制状态回放；在接入
        之前，rewind 只能依赖调用方显式传入 active revision。
        """
        state = self._sources.get(source_id)
        if state is None:
            raise KeyError(f"Context source 不存在: source_id={source_id}")
        if not state.tracked or state.latest_revision is None:
            return False
        if active_revision == state.latest_revision:
            return False
        state.pending_kind = "rebuild"
        self._queue_delta(state)
        return True

    def prepare_pending(self) -> PendingContextSourceBatch | None:
        pending = tuple(self._pending.values())
        if not pending:
            return None
        return PendingContextSourceBatch(deltas=pending)

    def commit_model_call_pending(
        self,
        batch: PendingContextSourceBatch,
    ) -> CommittedContextSourceBatch:
        """原子提交 model_call 边界的 pending 批次（复用唯一 durable 端口）。

        - 重复提交同一批次复用同一次提交结果：不重复持久化、不重复推进
          applied、不重复发布事件。
        - 批次与当前 pending 不一致（提交前出现新变化）时 fail closed，
          不推进任何 applied revision。
        - 每条 delta 与内存 registration 的身份/revision/diff 基准校验；
          任何漂移都让整批失败，不产生半提交。
        - 控制状态先经唯一 durable 端口逐条持久化（每条一个 owner 事务，CAS
          防并发推进）；全部成功后才推进内存 applied、清空 pending 并发布
          事件。持久化失败时内存零变化，批次保持可重试。
        - 事件只在 durable truth 成功后发布；纯内存 CSM（无 owner）在内存
          真值推进后发布。

        有 Saver owner 时，delta 会先映射为
        ``ApplySourceLifecycleDecision``，再由 owner 在 source item/control state
        的同一事务中提交；无 owner 的纯内存测试仍只验证 CSM 控制状态顺序。
        """
        last = self._last_model_call_receipt
        if last is not None and last.deltas == batch.deltas:
            return last
        if tuple(self._pending.values()) != batch.deltas:
            raise RuntimeError(
                "Context source pending 在提交前发生变化，拒绝推进 applied revision"
            )
        for delta in batch.deltas:
            state = self._sources.get(delta.source_id)
            if state is None:
                raise KeyError(f"Context source 不存在: source_id={delta.source_id}")
            if (
                delta.source_kind != state.source_kind
                or delta.source_name != state.name
                or delta.revision != state.latest_revision
                or delta.previous_revision != state.latest_visible_committed_revision
            ):
                raise RuntimeError(
                    "Context source pending 与 registration 状态不一致"
                    f"（fence drift），拒绝提交: source_id={delta.source_id}"
                )
        # 先经唯一 owner intent 端口提交全部 source item/control state；任何
        # 失败都让内存 applied/diff 基准保持不变。纯内存/旧测试装配没有
        # mutation_intent_port 时仍使用注入的控制状态替身，不触碰生产 Saver。
        mutation_consume = (
            getattr(self._mutation_intent_port, "consume_mutation_intent", None)
            if self._mutation_intent_port is not None
            else None
        )
        if mutation_consume is not None:
            if self._owner is None:
                raise RuntimeError(
                    "ContextSourceManager source intent 提交缺少 owner"
                )
            decisions = []
            for delta in batch.deltas:
                state = self._sources[delta.source_id]
                decision_kind = delta.kind
                observation = SourceObservation(
                    owner=self._owner,
                    source_id=delta.source_id,
                    source_kind=delta.source_kind,
                    name=delta.source_name,
                    revision=delta.revision,
                    tracking_mode=("tracked" if state.tracked else "untrack"),
                    from_revision=(
                        delta.previous_revision
                        if decision_kind == "delta"
                        else None
                    ),
                    content=delta.content,
                )
                decision = build_source_lifecycle_decision(
                    observation,
                    decision_kind=decision_kind,
                )
                decisions.append(
                    replace(
                        decision,
                        item_id=(
                            f"item-context-source:{delta.source_id}:"
                            f"{delta.revision}:{delta.kind}"
                        ),
                    )
                )
            for decision in decisions:
                mutation_consume(decision)
        else:
            # 已持久化的 source 在重试时按 durable fields 幂等跳过。
            for delta in batch.deltas:
                state = self._sources[delta.source_id]
                self._persist_control_state(
                    state,
                    applied_revision=state.latest_revision,
                )
        # durable truth 成功后才推进内存真值并清空 pending。
        for delta in batch.deltas:
            state = self._sources[delta.source_id]
            state.latest_visible_committed_revision = state.latest_revision
            state.latest_visible_committed_content = state.latest_content
            state.pending_kind = None
            # provenance 随本次提交闭合：已合并的 observed revisions 不再保留。
            state.observed_revisions.clear()
        self._pending.clear()
        receipt = CommittedContextSourceBatch(deltas=batch.deltas)
        self._last_model_call_receipt = receipt
        # OpenSpec 3.8-C：commit 成功边界发布轻量事件（每个来源一条），
        # 发布发生在 durable truth 成功之后，失败路径不发布。
        for delta in batch.deltas:
            self._publish_lifecycle_event(
                source_id=delta.source_id,
                source_kind=delta.source_kind,
                kind="committed",
                revision=delta.revision,
            )
        return receipt

    def _publish_lifecycle_event(
        self,
        *,
        source_id: str,
        source_kind: str,
        kind: str,
        revision: str | None,
    ) -> None:
        """把来源生命周期通知交给注入的发布出口；未注入时是无操作。"""
        sink = self._lifecycle_event_sink
        if sink is None:
            return
        owner = self._owner
        sink(
            ContextSourceEvent(
                source_id=source_id,
                source_kind=source_kind,
                kind=kind,
                revision=revision,
                session_id=owner.session_id if owner is not None else None,
                thread_id=owner.thread_id if owner is not None else None,
            )
        )

    def _registered_states(self) -> tuple[_SourceState, ...]:
        """按最近一次 register 的 catalog 顺序返回有 descriptor 的状态。"""
        states: list[_SourceState] = []
        for source_id in self._source_ids_by_name.values():
            state = self._sources.get(source_id)
            if state is not None and state.descriptor is not None:
                states.append(state)
        return tuple(states)

    def _persist_control_state(
        self,
        state: _SourceState,
        *,
        applied_revision: str | None = None,
    ) -> None:
        """把具有跨重启价值的 registration 状态经唯一 owner 端口写入。

        applied_revision 显式指定提交时刻的 applied 基准（model_call 提交路径
        传 state.latest_revision）；缺省时使用内存当前 applied 值。
        """
        port = self._control_state_port
        owner = self._owner
        if port is None or owner is None:
            return
        if not state.has_durable_interest():
            return
        snapshot = ContextSourceControlState(
            owner=owner,
            source_id=state.source_id,
            source_kind=state.source_kind,
            name=state.name,
            binding_revision=state.binding_revision,
            tracking_status=state.tracking_status,
            latest_visible_committed_revision=(
                state.latest_visible_committed_revision
                if applied_revision is None
                else applied_revision
            ),
            latest_revision=state.latest_revision,
            state_revision=state.persisted_state_revision,
        )
        if state.persisted_fields == snapshot.durable_fields():
            return
        stored = port.save_context_source_control_state(snapshot)
        if stored.owner != owner or stored.source_id != state.source_id:
            raise RuntimeError(
                "context source 控制状态端口返回了不匹配的 registration: "
                f"expected=({owner.session_id},{owner.thread_id},"
                f"{state.source_id}) "
                f"actual=({stored.owner.session_id},{stored.owner.thread_id},"
                f"{stored.source_id})"
            )
        state.persisted_state_revision = stored.state_revision
        state.persisted_fields = stored.durable_fields()

    def _apply_content(
        self,
        state: _SourceState,
        content: str,
        *,
        kind: Literal["activation", "delta", "rebuild"],
    ) -> None:
        revision = _revision(content)
        if state.latest_revision == revision and state.latest_content == content:
            return
        state.latest_revision = revision
        state.latest_content = content
        state.pending_kind = kind
        self._queue_delta(state)

    def _record_observation(self, state: _SourceState, revision: str) -> None:
        """按观察顺序记录 delta provenance；同一 revision 只记一次。"""
        if revision not in state.observed_revisions:
            state.observed_revisions.append(revision)

    def _queue_delta(self, state: _SourceState) -> None:
        if state.latest_revision is None or state.latest_content is None:
            raise RuntimeError("Context source 缺少 latest revision/content")
        if state.pending_kind == "rebuild":
            self._pending[state.source_id] = ContextSourceDelta(
                source_id=state.source_id,
                source_name=state.name,
                source_kind=state.source_kind,
                revision=state.latest_revision,
                previous_revision=state.latest_visible_committed_revision,
                content=state.latest_content,
                content_hash=_revision(state.latest_content),
                kind="rebuild",
            )
            return
        previous_content = state.latest_visible_committed_content
        if previous_content is None:
            content = state.latest_content
            kind: Literal["activation", "delta", "rebuild"] = state.pending_kind or "activation"
        else:
            diff = "".join(
                difflib.unified_diff(
                    previous_content.splitlines(keepends=True),
                    state.latest_content.splitlines(keepends=True),
                    fromfile=f"{state.name}@{state.latest_visible_committed_revision}",
                    tofile=f"{state.name}@{state.latest_revision}",
                )
            )
            if not diff:
                state.latest_revision = state.latest_visible_committed_revision
                state.latest_content = state.latest_visible_committed_content
                state.pending_kind = None
                state.observed_revisions.clear()
                self._pending.pop(state.source_id, None)
                return
            content = diff
            kind = state.pending_kind or "delta"
        self._pending[state.source_id] = ContextSourceDelta(
            source_id=state.source_id,
            source_name=state.name,
            source_kind=state.source_kind,
            revision=state.latest_revision,
            previous_revision=(
                state.latest_visible_committed_revision
            ),
            content=content,
            content_hash=_revision(content),
            observation_provenance=tuple(state.observed_revisions),
            kind=kind,
        )


def _adopt_restored_baseline(
    state: _SourceState,
    content: str,
    revision: str,
) -> bool:
    """重启恢复后的首帧：同一 revision 只重建 diff 基准，不重复注入。

    只作用于刚从持久化控制状态恢复、还没有内存正文的 registration；
    untracked 状态在 ``observe`` 之前就被拦截，因此不会走到这里。
    """
    if (
        state.latest_visible_committed_revision is None
        or state.latest_visible_committed_revision != revision
        or state.latest_revision != revision
        or state.latest_visible_committed_content is not None
        or state.latest_content is not None
        or state.pending_kind is not None
    ):
        return False
    state.latest_visible_committed_content = content
    state.latest_content = content
    return True


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "CommittedContextSourceBatch",
    "ContextSourceDelta",
    "ContextSourceDescriptor",
    "ContextSourceManager",
    "PendingContextSourceBatch",
    "PendingSourceObservation",
    "SkillLoadAppendStatus",
    "SkillLoadMode",
    "SkillLoadReceipt",
    "SkillLoadStatus",
]
