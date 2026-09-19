"""OpenSpec 8.4 的 GraphBinding：可验证的 graph factory selector 与其注册表。

持久化的是可验证的 factory selector 四元组（``graph_id``、``graph_revision``、
``graph_schema_hash``、``capability_profile_hash``），不是 Python
``CompiledStateGraph`` 或任何对象引用。重启后 runtime 以持久化 binding 在
:class:`GraphFactoryRegistry` 中解析受注册的 factory 并逐字段校验
revision/hash；解析不到或不匹配时抛 :class:`GraphBindingUnavailableError`
（``graph_binding_unavailable``），绝不回退到当前最新图。

进程内缓存最多只允许复用不捕获 Session/Thread 的 graph blueprint/topology；
需要 Session、thread、工具、provider 或执行信息的工具/middleware 通过每次
invocation 的 :class:`ThreadRuntimeBinding` 取得（见 ``binding_for``），避免一个
已编译图把其它 thread 的闭包带入请求。

hash 口径（deep-agent revision 1）：两个 hash 都是「确定性 canonical JSON →
sha256」的稳定内容摘要，摘要形状遵循
``app/services/infrastructure/events/channel_events.py`` 的同一标准
（``sha256:`` + 恰好 64 位小写 hex，fullmatch 全匹配校验）：

- ``graph_schema_hash``：middleware 栈 slot 标识（有序）+ 工具面 slot 标识
  （有序）+ graph_id 的确定性序列化。
- ``capability_profile_hash``：能力 profile 平面键值映射的确定性序列化。

revision 1 的 slot/profile 是代码内声明式骨架（``DEEP_AGENT_MIDDLEWARE_STACK``
等常量），而不是按 invocation 实际构建产物逐次计算：resolved 工具面与
middleware 的实际出现会随 denylist/overrides/MCP 连接状态按 invocation 漂移，
若把它们直接纳入 hash，同一 ``(graph_id, graph_revision)`` 的重复注册会触发
注册冲突错误。TODO(OpenSpec 8.4 后续轮)：把声明式骨架替换为「真实构建产物中
稳定部分的确定性投影」，并让 registry 冲突检查覆盖该投影。

注册表采用 events/ 的闭集哲学：只允许代码内注册，不提供运行时可配置的
factory 注册扩展点；新增 graph family 属于代码变更。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from app.agents.tool_invocation_context import ThreadRuntimeBinding

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
        raise ValueError(
            f"{field_name} 不允许是宿主机路径，只能是稳定标识: {value!r}"
        )
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
        if (
            not isinstance(self.graph_revision, int)
            or isinstance(self.graph_revision, bool)
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

合同（OpenSpec 8.4 / design「graph factory 可重建」）：factory 只重建不捕获
Session/Thread 的 graph blueprint/topology；需要 Session、thread、工具、
provider 或执行信息的部分必须在 invocation 时经 :class:`ThreadRuntimeBinding`
显式注入，不允许 factory 闭包捕获其它 thread 的上下文。
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
    return _sha256_of_canonical_json(
        {"kind": "capability_profile", "profile": profile}
    )


DEEP_AGENT_GRAPH_ID: Final[str] = "deep-agent"
"""deep agent graph family 的固定标识。"""

DEEP_AGENT_GRAPH_REVISION: Final[int] = 1
"""deep agent 当前代码的 graph revision；改动图骨架/能力面时必须 +1。"""

# revision 1 的 middleware 栈声明式骨架：与 build_deep_agent_middleware 的
# 有序 slot 一一对应。条件 slot（按 denylist/配置/MCP 状态出现）也以固定 slot
# 名进入声明；按 invocation 的实际出现不属于 selector 校验范围（见模块
# docstring 的 TODO）。
DEEP_AGENT_MIDDLEWARE_STACK: Final[tuple[str, ...]] = (
    "ToolInvocationContextMiddleware",
    "ToolOutputMiddleware",
    "TodoListMiddleware",
    "ContextSourceMiddlewares",
    "FilesystemMiddleware",
    "CachePreservingSummarizationMiddleware",
    "CachePreservingSummarizationToolMiddleware",
    "PatchToolCallsMiddleware",
    "StructuredToolCallMiddleware",
    "ModelToolVisibilityMiddleware",
    "InjectedRuntimeMiddleware",
    "CapabilityRoutingMiddleware",
    "StructuredMemoryMiddleware",
    "CustomToolConfirmationMiddleware",
    "HumanInTheLoopMiddleware",
    "StructuredPromptValidationMiddleware",
)

# revision 1 的工具面声明式骨架：内置默认工具、custom specs、扩展工具统一
# invoke_extension_tool 信封、skill_load 工具。
DEEP_AGENT_TOOL_FACE: Final[tuple[str, ...]] = (
    "builtin-default-tools",
    "custom-spec-tools",
    "extension-invoker-envelope:invoke_extension_tool",
    "skill-load-tool",
)

# revision 1 的能力 profile：main thread 产品合同（child profile
# goal_enabled=false 属于后续 ThreadRuntimePolicy 轮次）。
DEEP_AGENT_CAPABILITY_PROFILE: Final[Mapping[str, object]] = MappingProxyType(
    {
        "thread_role": "main",
        "goal_enabled": True,
        "extension_tool_envelope": "invoke_extension_tool",
        "skill_visibility": "name-description-only",
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
                raise ValueError(
                    f"GraphBindingOwnerKey.{field_name} 必须是非空字符串"
                )


class GraphBindingStorePort(Protocol):
    """GraphBinding 在 owner SessionThread 上的读写端口；实现方拥有存储。"""

    def load_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
    ) -> GraphBinding | None:
        """读取该 owner 已持久化的 selector；从未持久化时返回 None。"""
        ...

    def save_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
        binding: GraphBinding,
    ) -> None:
        """持久化该 owner 的 selector；与已存内容不同必须显式冲突。"""
        ...


_STORE_FORMAT_VERSION: Final[int] = 1
_STORE_FILE_NAME: Final[str] = "graph-bindings.json"
_BINDING_ENTRY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "graph_id",
        "graph_revision",
        "graph_schema_hash",
        "capability_profile_hash",
    }
)


class JsonFileGraphBindingStore:
    """最小侵入的 GraphBinding 持久化：每个 owner 目录一个 JSON 文档。

    选择独立小存储而不是扩 RolloutCheckpointSaver/rollout SQLite schema 的
    理由：GraphBinding 在 thread 创建/重建时写入一次、重启时读取一次，没有
    CSM 控制状态那样的工具调用/before_model 高频事务边界；独立 JSON 文档不
    触碰 canonical writer 的文件锁序，也不涉及 v4 schema 版本化迁移，侵入
    最小。持久化位置由装配方显式传入：生产必须把 ``directory`` 指到统一会话
    路径解析器解析出的 thread 节点附属目录；本类不拼接任何 ``.boxteam`` 固定
    路径。session/thread 身份只作为文档内的字典键，不进入文件名，无路径拼接
    风险。

    语义：``save`` 对同一 owner 幂等（同一 selector 重复写入不产生副作用），
    写入不同 selector 视为绑定漂移并显式失败（fail closed）；重绑流程属于
    OpenSpec 后续轮次，本类不静默覆盖。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / _STORE_FILE_NAME

    def load_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
    ) -> GraphBinding | None:
        if not isinstance(owner, GraphBindingOwnerKey):
            raise TypeError(
                f"load_graph_binding 需要 GraphBindingOwnerKey: {type(owner).__name__}"
            )
        bindings = self._read_bindings()
        session_bindings = bindings.get(owner.session_id)
        if not isinstance(session_bindings, dict):
            return None
        entry = session_bindings.get(owner.thread_id)
        if entry is None:
            return None
        return self._binding_from_entry(owner, entry)

    def save_graph_binding(
        self,
        owner: GraphBindingOwnerKey,
        binding: GraphBinding,
    ) -> None:
        if not isinstance(owner, GraphBindingOwnerKey):
            raise TypeError(
                f"save_graph_binding 需要 GraphBindingOwnerKey: {type(owner).__name__}"
            )
        if not isinstance(binding, GraphBinding):
            raise TypeError(
                f"save_graph_binding 需要 GraphBinding: {type(binding).__name__}"
            )
        bindings = self._read_bindings()
        session_bindings = dict(
            bindings.get(owner.session_id)
            if isinstance(bindings.get(owner.session_id), dict)
            else {}
        )
        existing = session_bindings.get(owner.thread_id)
        if existing is not None:
            stored = self._binding_from_entry(owner, existing)
            if stored == binding:
                # 幂等写入：同一 owner 的同一 selector 重复持久化无副作用。
                return
            raise RuntimeError(
                "graph-binding-store-conflict: 该 SessionThread 已持久化不同的 "
                f"GraphBinding: owner=({owner.session_id!r}, {owner.thread_id!r}), "
                f"已持久化=[{_binding_summary(stored)}], "
                f"新写入=[{_binding_summary(binding)}]；重绑必须走显式流程，"
                "不允许静默覆盖"
            )
        session_bindings[owner.thread_id] = {
            "graph_id": binding.graph_id,
            "graph_revision": binding.graph_revision,
            "graph_schema_hash": binding.graph_schema_hash,
            "capability_profile_hash": binding.capability_profile_hash,
        }
        bindings[owner.session_id] = session_bindings
        self._write_document({"version": _STORE_FORMAT_VERSION, "bindings": bindings})

    def _read_bindings(self) -> dict[str, object]:
        """读取并严格校验存储文档；文件不存在等于没有任何持久化 binding。"""
        if not self._path.exists():
            return {}
        raw = self._path.read_text(encoding="utf-8")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"graph-binding-store 文档损坏，无法解析 JSON: path={self._path}: {error}"
            ) from error
        if not isinstance(document, dict):
            raise TypeError(
                f"graph-binding-store 文档必须是对象: path={self._path}, "
                f"actual_type={type(document).__name__}"
            )
        if set(document) != {"version", "bindings"}:
            raise RuntimeError(
                "graph-binding-store 文档字段集非法: "
                f"path={self._path}, fields={sorted(document)}"
            )
        version = document["version"]
        if isinstance(version, bool) or version != _STORE_FORMAT_VERSION:
            raise RuntimeError(
                f"graph-binding-store 文档版本非法: path={self._path}, "
                f"version={version!r}, expected={_STORE_FORMAT_VERSION}"
            )
        bindings = document["bindings"]
        if not isinstance(bindings, dict):
            raise TypeError(
                f"graph-binding-store bindings 必须是对象: path={self._path}"
            )
        for session_id, session_bindings in bindings.items():
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError(
                    f"graph-binding-store session_id 键非法: path={self._path}, "
                    f"key={session_id!r}"
                )
            if not isinstance(session_bindings, dict):
                raise TypeError(
                    f"graph-binding-store session 条目必须是对象: "
                    f"path={self._path}, session_id={session_id!r}"
                )
            for thread_id, entry in session_bindings.items():
                if not isinstance(thread_id, str) or not thread_id:
                    raise RuntimeError(
                        f"graph-binding-store thread_id 键非法: "
                        f"path={self._path}, session_id={session_id!r}, "
                        f"key={thread_id!r}"
                    )
                # 值级校验委托给 GraphBinding 构造（含摘要形状全匹配）。
                self._binding_from_entry(
                    GraphBindingOwnerKey(
                        session_id=session_id,
                        thread_id=thread_id,
                    ),
                    entry,
                )
        return bindings

    @staticmethod
    def _binding_from_entry(
        owner: GraphBindingOwnerKey,
        entry: object,
    ) -> GraphBinding:
        if not isinstance(entry, dict):
            raise TypeError(
                f"graph-binding-store binding 条目必须是对象: "
                f"owner=({owner.session_id!r}, {owner.thread_id!r}), "
                f"actual_type={type(entry).__name__}"
            )
        if set(entry) != _BINDING_ENTRY_FIELDS:
            raise RuntimeError(
                f"graph-binding-store binding 条目字段集非法: "
                f"owner=({owner.session_id!r}, {owner.thread_id!r}), "
                f"fields={sorted(entry)}"
            )
        return GraphBinding(
            graph_id=entry["graph_id"],
            graph_revision=entry["graph_revision"],
            graph_schema_hash=entry["graph_schema_hash"],
            capability_profile_hash=entry["capability_profile_hash"],
        )

    def _write_document(self, document: dict[str, object]) -> None:
        """原子写入：先写同目录临时文件，再 os.replace 覆盖正式文档。"""
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_name(f"{_STORE_FILE_NAME}.tmp")
        temporary_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary_path, self._path)


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

    持久 selector 负责解析不捕获 thread 的 graph blueprint/topology；线程
    归属只来自 Agent 后端装配时的 :class:`ThreadRuntimeBinding`，二者合成
    一次 invocation 的完整身份。
    """
    return InvocationGraphBinding(
        graph=graph_binding,
        thread=thread_runtime_binding,
    )


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
    "JsonFileGraphBindingStore",
    "binding_for",
    "compute_capability_profile_hash",
    "compute_graph_schema_hash",
]
