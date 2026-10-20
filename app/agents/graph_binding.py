"""OpenSpec 8.4 的 GraphBinding：可验证的 graph factory selector 与其注册表。

持久化的是可验证的 factory selector 四元组（``graph_id``、``graph_revision``、
``graph_schema_hash``、``capability_profile_hash``），不是 Python
``CompiledStateGraph`` 或任何对象引用。重启后 runtime 以持久化 binding 在
:class:`GraphFactoryRegistry` 中解析受注册的 factory 并逐字段校验
revision/hash；解析不到或不匹配时抛 :class:`GraphBindingUnavailableError`
（``graph_binding_unavailable``），绝不回退到当前最新图。

进程级 blueprint/topology 不捕获 Session/Thread；真实编译图由精确
SessionThread 的 generation scope 持有，工厂通过 :class:`ThreadRuntimeBinding`
显式绑定该 thread。跨 Turn 复用同代图，每轮 execution admission 由 Runner
重新绑定，禁止把已编译图或首次 admission 交给其它 thread。

hash 口径（deep-agent revision 9）：两个 hash 都是「确定性 canonical JSON →
sha256」的稳定内容摘要，摘要形状遵循
``app/services/infrastructure/events/channel_events.py`` 的同一标准
（``sha256:`` + 恰好 64 位小写 hex，fullmatch 全匹配校验）：

- ``graph_schema_hash``：middleware 栈 slot 标识（有序）+ 工具面 slot 标识
  （有序）+ graph_id 的确定性序列化。
- ``capability_profile_hash``：能力 profile 平面键值映射的确定性序列化。

revision 9 的 slot/profile 是代码内声明式骨架（``DEEP_AGENT_MIDDLEWARE_STACK``
等常量），而不是按 generation 实际构建产物逐次计算：resolved 工具面与
middleware 的实际出现会随 denylist/overrides/MCP 连接状态按 generation 漂移，
若把它们直接纳入 hash，同一 ``(graph_id, graph_revision)`` 的重复注册会触发
注册冲突错误。TODO(OpenSpec 8.4 后续轮)：把声明式骨架替换为「真实构建产物中
稳定部分的确定性投影」，并让 registry 冲突检查覆盖该投影。

注册表采用 events/ 的闭集哲学：只允许代码内注册，不提供运行时可配置的
factory 注册扩展点；新增 graph family 属于代码变更。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from app.agents.tool_invocation_context import ThreadRuntimeBinding
from app.core.session_control_store import SessionControlStore

# 摘要形状与 channel_events.py 同一标准：完整 ``sha256:`` + 64 位小写 hex。
# 只查前缀会让「sha256: + 任意正文」走私，因此必须 fullmatch 全匹配。
_SHA256_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")

# graph_id 是短稳定标识：超长值只可能是把正文塞进了 identity 字段。
_MAX_IDENTITY_LENGTH: Final[int] = 512

# 宿主机路径形状：POSIX 绝对路径与 Windows 盘符路径。
_HOST_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:[\\/]")


def _validate_graph_identity(value: object, *, field_name: str) -> None:
    """graph_id 只允许稳定标识形状，禁止路径形状、控制字符与超长值。

    与 events/channel_events.py 的 identity 合同同一理由：合法的 graph family
    标识（如 ``deep-agent``）不会包含路径分隔符，持久化 selector 也不允许把
    宿主机路径伪装成标识。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(
            f"{field_name} 超过长度上限 {_MAX_IDENTITY_LENGTH}: 实际 {len(value)}"
        )
    if value.startswith("/") or _HOST_PATH_PATTERN.match(value):
        raise ValueError(f"{field_name} 不允许是宿主机路径，只能是稳定标识: {value!r}")
    if "/" in value or "\\" in value or ".." in value or value.startswith("~"):
        raise ValueError(
            f"{field_name} 不允许包含路径分隔符、'..' 或 '~' 前缀: {value!r}"
        )
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{field_name} 包含非法控制字符: {value!r}")


def _validate_digest(value: object, *, field_name: str) -> None:
    """摘要字段必须是完整 sha256 摘要（sha256: + 64 位小写 hex）。"""
    if not isinstance(value, str) or not _SHA256_DIGEST_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} 必须是 sha256 摘要（sha256: + 64 位小写 hex）: {value!r}"
        )


@dataclass(frozen=True, slots=True)
class GraphBinding:
    """一个 graph family revision 的持久化 factory selector。

    四元组逐字段可验证：registry 以 ``(graph_id, graph_revision)`` 定位受注册
    的 factory，再以两个 hash 校验「当前代码仍会重建同一 graph 骨架/能力面」。
    """

    graph_id: str
    graph_revision: int
    graph_schema_hash: str
    capability_profile_hash: str

    def __post_init__(self) -> None:
        _validate_graph_identity(
            self.graph_id,
            field_name="GraphBinding.graph_id",
        )
        if not isinstance(self.graph_revision, int) or isinstance(
            self.graph_revision, bool
        ):
            raise TypeError(
                "GraphBinding.graph_revision 必须是整数: "
                f"{type(self.graph_revision).__name__}"
            )
        if self.graph_revision < 1:
            raise ValueError(
                f"GraphBinding.graph_revision 必须 >= 1: 实际 {self.graph_revision}"
            )
        _validate_digest(
            self.graph_schema_hash,
            field_name="GraphBinding.graph_schema_hash",
        )
        _validate_digest(
            self.capability_profile_hash,
            field_name="GraphBinding.capability_profile_hash",
        )

    def selector_fields(self) -> tuple[object, ...]:
        """返回持久化与相等比较用的四元组。"""
        return (
            self.graph_id,
            self.graph_revision,
            self.graph_schema_hash,
            self.capability_profile_hash,
        )


class GraphBindingUnavailableError(RuntimeError):
    """binding 无法解析到受注册的 graph factory（不允许回退到最新 revision）。"""


class GraphFactoryRegistrationConflictError(RuntimeError):
    """同一 ``(graph_id, graph_revision)`` 被注册为不同内容。"""


GraphFactoryBuilder = Callable[..., object]
"""可由 GraphBinding 选中的 graph factory callable。

合同（OpenSpec 8.4 / design「graph factory 可重建」）：注册 callable 不捕获
Session/Thread。构建精确 thread generation 时，session/thread 经
:class:`ThreadRuntimeBinding` 显式注入；编译结果只由该 thread 的 scope 持有。
工厂与长期资源不得捕获任何 invocation admission。
"""


@dataclass(frozen=True, slots=True)
class _GraphFactoryEntry:
    """registry 中一个 ``(graph_id, graph_revision)`` 的受注册内容。"""

    binding: GraphBinding
    builder: GraphFactoryBuilder


def _binding_summary(binding: GraphBinding) -> str:
    """渲染 binding 四元组，用于错误消息逐字段对照。"""
    return (
        f"graph_id={binding.graph_id!r}, "
        f"graph_revision={binding.graph_revision}, "
        f"graph_schema_hash={binding.graph_schema_hash!r}, "
        f"capability_profile_hash={binding.capability_profile_hash!r}"
    )


class GraphFactoryRegistry:
    """代码内闭集注册的 graph factory 注册表。

    key 是 ``(graph_id, graph_revision)``；同一 key 重复注册不同
    schema/capability hash 或不同 builder 都显式冲突。``resolve`` 逐字段校验
    四元组：factory 不存在 / revision 不匹配 / schema hash 不匹配 /
    capability hash 不匹配分别给出带期望与实际值的
    :class:`GraphBindingUnavailableError`，绝不回退到其它 revision。
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], _GraphFactoryEntry] = {}

    def register(
        self,
        binding: GraphBinding,
        builder: GraphFactoryBuilder,
    ) -> None:
        """注册一个 graph family revision 的 factory；幂等或显式冲突。"""
        if not callable(builder):
            raise TypeError(
                f"graph factory builder 必须是可调用对象: {type(builder).__name__}"
            )
        key = (binding.graph_id, binding.graph_revision)
        existing = self._entries.get(key)
        if existing is not None:
            if existing.binding == binding and existing.builder is builder:
                # 幂等重复注册（例如模块重复 import / 测试重复注册同一 factory）。
                return
            raise GraphFactoryRegistrationConflictError(
                "graph-factory-registration-conflict: 同一 "
                f"(graph_id, graph_revision) 已注册为不同内容: key={key!r}, "
                f"已注册=[{_binding_summary(existing.binding)}, "
                f"builder={existing.builder!r}], "
                f"新注册=[{_binding_summary(binding)}, builder={builder!r}]"
            )
        self._entries[key] = _GraphFactoryEntry(binding=binding, builder=builder)

    def resolve(self, binding: GraphBinding) -> GraphFactoryBuilder:
        """按四元组逐字段解析 factory；任何不匹配都显式失败，不回退。"""
        registered_graph_ids = self.registered_graph_ids()
        if binding.graph_id not in registered_graph_ids:
            raise GraphBindingUnavailableError(
                "graph_binding_unavailable: registry 中不存在该 graph_id 的 "
                f"factory: 期望 graph_id={binding.graph_id!r}, "
                f"实际已注册 graph_id={list(registered_graph_ids)}；"
                f"binding: {_binding_summary(binding)}"
            )
        entry = self._entries.get((binding.graph_id, binding.graph_revision))
        if entry is None:
            available_revisions = sorted(
                revision
                for graph_id, revision in self._entries
                if graph_id == binding.graph_id
            )
            raise GraphBindingUnavailableError(
                "graph_binding_unavailable: graph revision 不匹配，不回退到其它 "
                f"revision: 期望 graph_revision={binding.graph_revision}, "
                f"实际已注册 revisions={available_revisions}；"
                f"binding: {_binding_summary(binding)}"
            )
        if entry.binding.graph_schema_hash != binding.graph_schema_hash:
            raise GraphBindingUnavailableError(
                "graph_binding_unavailable: graph schema hash 不匹配: "
                f"期望(注册)={entry.binding.graph_schema_hash!r}, "
                f"实际(binding)={binding.graph_schema_hash!r}；"
                f"binding: {_binding_summary(binding)}"
            )
        if entry.binding.capability_profile_hash != binding.capability_profile_hash:
            raise GraphBindingUnavailableError(
                "graph_binding_unavailable: capability profile hash 不匹配: "
                f"期望(注册)={entry.binding.capability_profile_hash!r}, "
                f"实际(binding)={binding.capability_profile_hash!r}；"
                f"binding: {_binding_summary(binding)}"
            )
        return entry.builder

    def registered_graph_ids(self) -> tuple[str, ...]:
        """返回当前已注册的全部 graph_id（排序后），用于错误对照。"""
        return tuple(sorted({graph_id for graph_id, _ in self._entries}))


def _sha256_of_canonical_json(payload: object) -> str:
    """确定性 canonical JSON 序列化后取 sha256，返回标准摘要形状。"""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_slot_sequence(value: object, *, field_name: str) -> tuple[str, ...]:
    """slot 标识序列必须是非空字符串序列（顺序参与 hash，代表栈/面的形状）。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{field_name} 必须是字符串序列")
    slots = tuple(value)
    if not slots:
        raise ValueError(f"{field_name} 不能为空")
    for slot in slots:
        if not isinstance(slot, str) or not slot.strip():
            raise ValueError(f"{field_name} 的每一项都必须是非空字符串: {slot!r}")
    return slots


def compute_graph_schema_hash(
    *,
    graph_id: str,
    middleware_stack: Sequence[str],
    tool_face: Sequence[str],
) -> str:
    """计算 graph schema hash：middleware 栈 slot + 工具面 slot 的内容摘要。

    口径（v1）：``{"kind", "graph_id", "middleware_stack", "tool_face"}`` 的
    canonical JSON → sha256。middleware 栈顺序与工具面构成决定
    ``create_agent`` 的图拓扑形状；per-invocation 的动态成员（MCP 实例、
    session 服务闭包）不属于 schema，只能经 ThreadRuntimeBinding 注入。
    """
    _validate_graph_identity(graph_id, field_name="graph_id")
    middleware_slots = _validate_slot_sequence(
        middleware_stack,
        field_name="middleware_stack",
    )
    tool_slots = _validate_slot_sequence(tool_face, field_name="tool_face")
    return _sha256_of_canonical_json(
        {
            "kind": "graph_schema",
            "graph_id": graph_id,
            "middleware_stack": list(middleware_slots),
            "tool_face": list(tool_slots),
        }
    )


_PROFILE_SCALAR_TYPES = (str, int, bool, type(None))


def compute_capability_profile_hash(
    *,
    capability_profile: Mapping[str, object],
) -> str:
    """计算能力 profile hash：平面键值映射的确定性内容摘要。

    口径（v1）：profile 必须是平面映射（键为非空字符串，值只能是
    str/int/bool/None 标量），``{"kind", "profile"}`` 的 canonical JSON →
    sha256。嵌套容器会让「同一能力面」出现多种等价序列化，先显式拒绝。
    """
    if not isinstance(capability_profile, Mapping) or not capability_profile:
        raise ValueError("capability_profile 必须是非空映射")
    profile: dict[str, object] = {}
    for key, value in capability_profile.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"capability_profile 的键必须是非空字符串: {key!r}")
        if not isinstance(value, _PROFILE_SCALAR_TYPES):
            raise TypeError(
                "capability_profile 的值只能是 str/int/bool/None 标量: "
                f"key={key!r}, value_type={type(value).__name__}"
            )
        profile[key] = value
    return _sha256_of_canonical_json({"kind": "capability_profile", "profile": profile})


DEEP_AGENT_GRAPH_ID: Final[str] = "deep-agent"
"""deep agent graph family 的固定标识。"""

DEEP_AGENT_GRAPH_REVISION: Final[int] = 9
"""deep agent 当前代码的 graph revision；改动图骨架/能力面时必须 +1。"""

# revision 9 的 middleware 栈声明式骨架：与 build_deep_agent_middleware 的
# 有序 slot 一一对应。条件 slot（按 denylist/配置/MCP 状态出现）也以固定 slot
# 名进入声明；按 invocation 的实际出现不属于 selector 校验范围（见模块
# docstring 的 TODO）。
DEEP_AGENT_MIDDLEWARE_STACK: Final[tuple[str, ...]] = (
    "ToolInvocationContextMiddleware",
    "ToolOutputMiddleware",
    "SourceOwnedTodoListMiddleware",
    "ContextSourceMiddlewares",
    "FilesystemMiddleware",
    "CustomToolConfirmationMiddleware",
    "HumanInTheLoopMiddleware",
    "CachePreservingSummarizationMiddleware",
    "CachePreservingSummarizationToolMiddleware",
    "StructuredToolCallMiddleware",
    "InjectedRuntimeMiddleware",
    "CapabilityRoutingMiddleware",
    "StructuredPromptValidationMiddleware",
)

# revision 9 的工具面声明式骨架：内置默认工具、custom specs、扩展工具统一
# invoke_extension_tool 信封、skill_load 工具。
DEEP_AGENT_TOOL_FACE: Final[tuple[str, ...]] = (
    "builtin-default-tools",
    "filesystem-ls:bounded-uri-navigation",
    "filesystem-read:bounded-uri-media",
    "custom-spec-tools",
    "extension-invoker-envelope:invoke_extension_tool",
    "skill-load-tool",
)

# revision 9 的能力 profile：main thread 产品合同（child profile
# goal_enabled=false 属于后续 ThreadRuntimePolicy 轮次）。
DEEP_AGENT_CAPABILITY_PROFILE: Final[Mapping[str, object]] = MappingProxyType(
    {
        "thread_role": "main",
        "goal_enabled": True,
        "extension_tool_envelope": "invoke_extension_tool",
        "skill_visibility": "name-description-only",
        "model_execution": "sealed-terminal",
    }
)

DEEP_AGENT_GRAPH_BINDING: Final[GraphBinding] = GraphBinding(
    graph_id=DEEP_AGENT_GRAPH_ID,
    graph_revision=DEEP_AGENT_GRAPH_REVISION,
    graph_schema_hash=compute_graph_schema_hash(
        graph_id=DEEP_AGENT_GRAPH_ID,
        middleware_stack=DEEP_AGENT_MIDDLEWARE_STACK,
        tool_face=DEEP_AGENT_TOOL_FACE,
    ),
    capability_profile_hash=compute_capability_profile_hash(
        capability_profile=DEEP_AGENT_CAPABILITY_PROFILE,
    ),
)

GRAPH_FACTORY_REGISTRY: Final[GraphFactoryRegistry] = GraphFactoryRegistry()
"""进程级 graph factory 注册表；只允许代码内注册（闭集，无运行时扩展点）。"""


@dataclass(frozen=True, slots=True)
class GraphBindingOwnerKey:
    """GraphBinding 持久化的 owner：一个精确的 SessionThread。"""

    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        for field_name in ("session_id", "thread_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"GraphBindingOwnerKey.{field_name} 必须是非空字符串")


class GraphBindingStorePort(Protocol):
    """GraphBinding 在 owner SessionThread 上的只读查询端口。"""

    def load_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
    ) -> GraphBinding | None:
        """读取权威 catalog selector；未绑定行返回 None，不能写入或推断。"""
        ...


class SessionControlGraphBindingStore:
    """只读读取 thread_catalog authority 的 GraphBinding 适配器。"""

    def __init__(
        self,
        session_control_store_factory: Callable[[str], SessionControlStore],
    ) -> None:
        if not callable(session_control_store_factory):
            raise TypeError("session_control_store_factory 必须可调用")
        self._session_control_store_factory = session_control_store_factory

    def load_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
    ) -> GraphBinding | None:
        if not isinstance(owner, GraphBindingOwnerKey):
            raise TypeError(
                f"load_graph_binding 需要 GraphBindingOwnerKey: {type(owner).__name__}"
            )
        store = self._session_control_store_factory(owner.session_id)
        try:
            record = store.get_thread_graph_binding(owner.thread_id)
        finally:
            store.close()
        if record is None:
            return None
        return GraphBinding(
            graph_id=record.graph_id,
            graph_revision=record.graph_revision,
            graph_schema_hash=record.graph_schema_hash,
            capability_profile_hash=record.capability_profile_hash,
        )


@dataclass(frozen=True, slots=True)
class InvocationGraphBinding:
    """一次 invocation 的完整绑定：持久 graph selector + 受信线程归属。"""

    graph: GraphBinding
    thread: ThreadRuntimeBinding

    def __post_init__(self) -> None:
        if not isinstance(self.graph, GraphBinding):
            raise TypeError(
                f"InvocationGraphBinding.graph 必须是 GraphBinding: "
                f"{type(self.graph).__name__}"
            )
        if not isinstance(self.thread, ThreadRuntimeBinding):
            raise TypeError(
                f"InvocationGraphBinding.thread 必须是 ThreadRuntimeBinding: "
                f"{type(self.thread).__name__}"
            )


def binding_for(
    graph_binding: GraphBinding,
    thread_runtime_binding: ThreadRuntimeBinding,
) -> InvocationGraphBinding:
    """组合查询辅助：把持久 selector 与 invocation 线程归属组成完整绑定。

    持久 selector 负责解析受注册的 factory；线程
    归属只来自 generation 装配时的 :class:`ThreadRuntimeBinding`，二者合成
    一次 invocation 的完整身份。
    """
    return InvocationGraphBinding(
        graph=graph_binding,
        thread=thread_runtime_binding,
    )


def resolve_graph_binding(
    *,
    store: GraphBindingStorePort,
    owner: GraphBindingOwnerKey,
    registry: GraphFactoryRegistry = GRAPH_FACTORY_REGISTRY,
) -> GraphBinding:
    """读取 authority 中的精确 selector 并解析注册 factory；缺失或漂移即失败。"""
    if not isinstance(owner, GraphBindingOwnerKey):
        raise TypeError(f"owner 必须是 GraphBindingOwnerKey: {type(owner).__name__}")
    persisted = store.load_graph_binding(owner)
    if persisted is None:
        raise GraphBindingUnavailableError(
            "graph_binding_unavailable: thread_catalog 缺少已创建的 GraphBinding: "
            f"owner=({owner.session_id!r}, {owner.thread_id!r})"
        )
    registry.resolve(persisted)
    return persisted


__all__ = [
    "DEEP_AGENT_CAPABILITY_PROFILE",
    "DEEP_AGENT_GRAPH_BINDING",
    "DEEP_AGENT_GRAPH_ID",
    "DEEP_AGENT_GRAPH_REVISION",
    "DEEP_AGENT_MIDDLEWARE_STACK",
    "DEEP_AGENT_TOOL_FACE",
    "GRAPH_FACTORY_REGISTRY",
    "GraphBinding",
    "GraphBindingOwnerKey",
    "GraphBindingStorePort",
    "GraphBindingUnavailableError",
    "GraphFactoryBuilder",
    "GraphFactoryRegistrationConflictError",
    "GraphFactoryRegistry",
    "InvocationGraphBinding",
    "SessionControlGraphBindingStore",
    "binding_for",
    "compute_capability_profile_hash",
    "compute_graph_schema_hash",
    "resolve_graph_binding",
]
