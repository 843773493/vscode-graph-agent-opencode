"""resource.state/*、config.lifecycle/*、context.source/* 与 mcp.catalog/* 的 typed 轻量事件合同。

各值对象只允许携带 identity、revision、state/kind 等轻量字段；时间与
event sequence 由 :class:`EventChannelService` 的投递信封（
:class:`ChannelDelivery`）盖章，不由生产方自行填写。

轻量红线（与 ``assert_notification_is_lightweight`` 同一做法）：字段集合按
dataclass 定义做类级白名单校验，值按形状做值级校验；塞入正文、credential 或
宿主机路径必须显式报错，而不是被静默传递。事件只是内存通知，不是 durable
事实，也不进入 job 队列。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Final

from app.services.infrastructure.events.event_channel_service import (
    CONFIG_LIFECYCLE_CHANNEL_KIND,
    CONTEXT_SOURCE_CHANNEL_KIND,
    MCP_CATALOG_CHANNEL_KIND,
    RESOURCE_STATE_CHANNEL_KIND,
    EventChannelService,
    EventChannelSpec,
    channel_name,
)

# 宿主机路径形状：POSIX 绝对路径与 Windows 盘符路径。
_HOST_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:[\\/]")

# revision 必须是完整的 sha256 摘要（sha256: + 恰好 64 位小写 hex）。
# 只查前缀会让「sha256: + 任意正文」走私（R2b 审查 M1 实测）。
_SHA256_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"sha256:(?:jcs:v1:)?[0-9a-f]{64}"
)

# identity/可选 id 的长度上限：typed id、域 名与定位字段都是短标识，
# 超长值只可能是把正文塞进了 identity 字段。
_MAX_IDENTITY_LENGTH: Final[int] = 512

RESOURCE_STATE_STATES: Final[frozenset[str]] = frozenset(
    {"released", "release_failed", "unavailable", "degraded", "unknown"}
)
RESOURCE_STATE_KINDS: Final[frozenset[str]] = frozenset({"state", "gap", "overflow"})

CONFIG_LIFECYCLE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "published",
        "failed",
        "reverted",
        # 配置 shadow lifecycle 适配器的完整结果闭集：candidate 与 active
        # revision 逐字节一致时发布 unchanged；domain 关闭时发布 closed。
        "unchanged",
        "closed",
        "gap",
        "overflow",
    }
)

CONTEXT_SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {"committed", "untracked", "reconcile", "gap", "overflow"}
)

MCP_CATALOG_KINDS: Final[frozenset[str]] = frozenset(
    {"published", "unchanged", "tombstone"}
)


def _validate_identity(value: str, *, field_name: str) -> None:
    """identity 只允许 typed id 或域 名，禁止路径形状、控制字符与超长值。

    拒绝 ``/``、``\\``、``..`` 与 ``~`` 前缀：这些是宿主机/相对路径的形状
    特征，合法的 typed id（如 ``skill:demo``、``node_debug``）不会包含它们。
    虚拟 URI 不经过本合同——``resource.observe/*`` 的 uri 字段有自己的
    ``boxteam://`` 校验。
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} 必须是非空字符串")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(
            f"{field_name} 超过长度上限 {_MAX_IDENTITY_LENGTH}: 实际 {len(value)}"
        )
    if value.startswith("/") or _HOST_PATH_PATTERN.match(value):
        raise RuntimeError(
            f"{field_name} 不允许是宿主机路径，只能是虚拟 identity: {value!r}"
        )
    if "/" in value or "\\" in value or ".." in value or value.startswith("~"):
        raise RuntimeError(
            f"{field_name} 不允许包含路径分隔符、'..' 或 '~' 前缀: {value!r}"
        )
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{field_name} 包含非法控制字符: {value!r}")


def _validate_revision(value: str | None, *, field_name: str) -> None:
    """revision 必须是完整 sha256 摘要（sha256: + 64 位小写 hex）或 None。

    全匹配校验：只查 ``sha256:`` 前缀会让「sha256: + 任意正文」静默通过
    （R2b 审查 M1 探针实测），因此这里必须匹配完整摘要形状。
    """
    if value is None:
        return
    if not isinstance(value, str) or not _SHA256_DIGEST_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"{field_name} 必须是 sha256: 摘要（sha256: + 64 位小写 hex）或 None: "
            f"{type(value).__name__} 长度 "
            f"{len(value) if isinstance(value, str) else 'N/A'}"
        )


def _validate_optional_id(value: str | None, *, field_name: str) -> None:
    """可选 id 字段与 identity 同一形状合同：非空、有界、无路径形状。"""
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} 必须是非空字符串或 None")
    if len(value) > _MAX_IDENTITY_LENGTH:
        raise ValueError(
            f"{field_name} 超过长度上限 {_MAX_IDENTITY_LENGTH}: 实际 {len(value)}"
        )
    if "/" in value or "\\" in value or ".." in value or value.startswith("~"):
        raise RuntimeError(
            f"{field_name} 不允许包含路径分隔符、'..' 或 '~' 前缀: {value!r}"
        )
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"{field_name} 包含非法控制字符: {value!r}")


_NO_ENUM: Final[frozenset[str]] = frozenset()


# 必填 revision 必须是完整 sha256 摘要；``with_type_name`` 只在构造校验路径附带值
# 类型名，与历史消息逐字一致（McpCatalogEvent 断言路径不带）。
def _required_revision_digest(*, with_type_name: bool) -> Callable[..., None]:
    def validate(value: object, *, field_name: str) -> None:
        if not isinstance(value, str) or not _SHA256_DIGEST_PATTERN.fullmatch(value):
            suffix = f": {type(value).__name__}" if with_type_name else ""
            raise RuntimeError(
                f"{field_name} 必须是 sha256: 摘要（sha256: + 64 位小写 hex）{suffix}"
            )

    return validate


_REVISION_DIGEST_WITH_TYPE = _required_revision_digest(with_type_name=True)
_REVISION_DIGEST_PLAIN = _required_revision_digest(with_type_name=False)


_EventCheck = tuple[Callable[..., None] | None, str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class _EventSchema:
    """一个 typed channel 事件的类级白名单与有序值级校验（唯一定义处）。

    ``checks``/``assert_checks`` 每项是 ``(validator, attribute, allowed)``：
    validator 为 None 时按 ``allowed`` 闭集校验，否则调用
    ``validator(value, field_name=label)``，字段名由 ``event_name`` 与 attribute 拼出。
    ``assert_checks`` 仅在轻量断言路径消息与构造路径历史性不同的事件上覆盖。
    """

    event_name: str
    allowed_fields: frozenset[str]
    checks: tuple[_EventCheck, ...]
    assert_checks: tuple[_EventCheck, ...] | None = None

    def values(self, event: object) -> None:
        """按序执行值级校验；构造（``__post_init__``）使用。"""
        self._run(event, self.checks)

    def full(self, event: object) -> None:
        """类级字段白名单 + 值级校验；轻量断言使用。"""
        unexpected = {field.name for field in fields(type(event))} - self.allowed_fields
        if unexpected:
            raise RuntimeError(
                f"{self.event_name} 声明了未允许的字段: " + ",".join(sorted(unexpected))
            )
        self._run(event, self.assert_checks or self.checks)

    def _run(self, event: object, checks: tuple[_EventCheck, ...]) -> None:
        for validate, attribute, allowed in checks:
            label = f"{self.event_name}.{attribute}"
            value = getattr(event, attribute)
            if validate is None:
                if value not in allowed:
                    raise ValueError(f"{label} 必须是 {sorted(allowed)} 之一: {value!r}")
            else:
                validate(value, field_name=label)


# --------------------------------------------------------------------------
# resource.state/{owner_domain}
# --------------------------------------------------------------------------

_ALLOWED_RESOURCE_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"owner_domain", "resource_id", "state", "kind", "revision"}
)

_RESOURCE_STATE_SCHEMA: Final[_EventSchema] = _EventSchema(
    event_name="ResourceStateEvent",
    allowed_fields=_ALLOWED_RESOURCE_STATE_FIELDS,
    checks=(
        (_validate_identity, "owner_domain", _NO_ENUM),
        (_validate_identity, "resource_id", _NO_ENUM),
        (None, "state", RESOURCE_STATE_STATES),
        (None, "kind", RESOURCE_STATE_KINDS),
        (_validate_revision, "revision", _NO_ENUM),
    ),
)


def resource_state_channel_name(owner_domain: str) -> str:
    """构造 ``resource.state/{owner_domain}`` channel 名。"""
    return channel_name(RESOURCE_STATE_CHANNEL_KIND, owner_domain)


@dataclass(frozen=True, slots=True)
class ResourceStateEvent:
    """资源生命周期状态的轻量通知：只有 owner 域、资源 identity、状态与 kind。"""

    owner_domain: str
    resource_id: str
    state: str
    kind: str = "state"
    revision: str | None = None

    def __post_init__(self) -> None:
        _RESOURCE_STATE_SCHEMA.values(self)


def assert_resource_state_event_is_lightweight(event: ResourceStateEvent) -> None:
    """类级 + 值级校验：resource.state 通知不得夹带正文/credential/宿主机路径。"""
    _RESOURCE_STATE_SCHEMA.full(event)


class ResourceStateEventPublisher:
    """把实际 resource owner 的轻量状态发布到独立 resource.state channel。"""

    def __init__(
        self,
        *,
        event_service: EventChannelService,
        owner_domain: str,
        max_queue_size: int = 64,
    ) -> None:
        if not isinstance(event_service, EventChannelService):
            raise TypeError("ResourceStateEventPublisher 需要 EventChannelService")
        _validate_identity(owner_domain, field_name="owner_domain")
        self._owner_domain = owner_domain
        self._channel = event_service.ensure_channel(
            EventChannelSpec(
                name=resource_state_channel_name(owner_domain),
                overflow_policy="gap",
                max_queue_size=max_queue_size,
                history_size=0,
            )
        )

    @property
    def channel_name(self) -> str:
        return self._channel.name

    def publish(
        self,
        *,
        resource_id: str,
        state: str,
        revision: str | None = None,
    ) -> None:
        """发布 owner 已确认的状态；通知失败必须由 owner 显式处理。"""
        event = ResourceStateEvent(
            owner_domain=self._owner_domain,
            resource_id=resource_id,
            state=state,
            revision=revision,
        )
        assert_resource_state_event_is_lightweight(event)
        self._channel.publish(event)


# --------------------------------------------------------------------------
# config.lifecycle/{domain}
# --------------------------------------------------------------------------

_ALLOWED_CONFIG_LIFECYCLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"domain", "kind", "generation", "revision"}
)

_CONFIG_LIFECYCLE_SCHEMA: Final[_EventSchema] = _EventSchema(
    event_name="ConfigLifecycleEvent",
    allowed_fields=_ALLOWED_CONFIG_LIFECYCLE_FIELDS,
    checks=(
        (_validate_identity, "domain", _NO_ENUM),
        (None, "kind", CONFIG_LIFECYCLE_KINDS),
        (_validate_optional_id, "generation", _NO_ENUM),
        (_validate_revision, "revision", _NO_ENUM),
    ),
)


def config_lifecycle_channel_name(domain: str) -> str:
    """构造 ``config.lifecycle/{domain}`` channel 名。"""
    return channel_name(CONFIG_LIFECYCLE_CHANNEL_KIND, domain)


@dataclass(frozen=True, slots=True)
class ConfigLifecycleEvent:
    """配置生命周期通知：只有配置域、generation/revision identity 与 kind。"""

    domain: str
    kind: str
    generation: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        _CONFIG_LIFECYCLE_SCHEMA.values(self)


def assert_config_lifecycle_event_is_lightweight(event: ConfigLifecycleEvent) -> None:
    """类级 + 值级校验：config.lifecycle 通知不得夹带配置正文或路径。"""
    _CONFIG_LIFECYCLE_SCHEMA.full(event)


# --------------------------------------------------------------------------
# context.source/{workspace_id}
# --------------------------------------------------------------------------

_ALLOWED_CONTEXT_SOURCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "source_id",
        "source_kind",
        "kind",
        "revision",
        "session_id",
        "thread_id",
    }
)

_CONTEXT_SOURCE_SCHEMA: Final[_EventSchema] = _EventSchema(
    event_name="ContextSourceEvent",
    allowed_fields=_ALLOWED_CONTEXT_SOURCE_FIELDS,
    checks=(
        (_validate_identity, "source_id", _NO_ENUM),
        (_validate_identity, "source_kind", _NO_ENUM),
        (None, "kind", CONTEXT_SOURCE_KINDS),
        (_validate_revision, "revision", _NO_ENUM),
        (_validate_optional_id, "session_id", _NO_ENUM),
        (_validate_optional_id, "thread_id", _NO_ENUM),
    ),
)


def context_source_channel_name(scope_id: str) -> str:
    """构造 ``context.source/{scope}`` channel 名。

    OpenSpec design 的参数是 ``workspace_id``；当前生产接线在 CSM 边界拿不到
    workspace_id，暂以调用方注入的最近稳定 identity（session_id）作为参数，
    见 :class:`ContextSourceEventPublisher` 与 OpenSpec 2.4 的迁移 TODO。
    """
    return channel_name(CONTEXT_SOURCE_CHANNEL_KIND, scope_id)


@dataclass(frozen=True, slots=True)
class ContextSourceEvent:
    """上下文来源生命周期通知：只有来源 identity、revision 与 kind。

    ``session_id``/``thread_id`` 是 identity 级定位字段；事件不携带来源正文、
    diff 或内部 locator。
    """

    source_id: str
    source_kind: str
    kind: str
    revision: str | None = None
    session_id: str | None = None
    thread_id: str | None = None

    def __post_init__(self) -> None:
        _CONTEXT_SOURCE_SCHEMA.values(self)


def assert_context_source_event_is_lightweight(event: ContextSourceEvent) -> None:
    """类级 + 值级校验：context.source 通知不得夹带正文/credential/宿主机路径。"""
    _CONTEXT_SOURCE_SCHEMA.full(event)


class ContextSourceEventPublisher:
    """把 CSM commit/untrack 边界的轻量事件发布到 ``context.source/{scope}``。

    当前 CSM 边界拿不到 workspace_id，使用调用方注入的最近稳定 identity
    （生产接线为 session_id）作为 channel 参数；TODO(OpenSpec 2.4)：model-call
    preparation 原子边界落地后，发布点与 scope 参数需要一起迁移。

    channel 采用 gap 溢出策略：通知可丢，丢失时以 gap 标记提示 consumer 重新
    对账；发布是纯内存操作，不做 I/O。
    """

    def __init__(
        self,
        *,
        event_service: EventChannelService,
        scope_id: str,
        max_queue_size: int = 64,
    ) -> None:
        if not isinstance(event_service, EventChannelService):
            raise TypeError("ContextSourceEventPublisher 需要 EventChannelService")
        if not isinstance(scope_id, str) or not scope_id:
            raise ValueError("ContextSourceEventPublisher.scope_id 必须是非空字符串")
        self._scope_id = scope_id
        self._channel = event_service.ensure_channel(
            EventChannelSpec(
                name=context_source_channel_name(scope_id),
                overflow_policy="gap",
                max_queue_size=max_queue_size,
                history_size=0,
            )
        )

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def channel_name(self) -> str:
        return self._channel.name

    def publish(self, event: ContextSourceEvent) -> None:
        """发布一条轻量来源事件；契约违规显式抛出，不静默吞掉。"""
        assert_context_source_event_is_lightweight(event)
        self._channel.publish(event)

    def __call__(self, event: ContextSourceEvent) -> None:
        self.publish(event)



# --------------------------------------------------------------------------
# mcp.catalog/workspace
# --------------------------------------------------------------------------

_ALLOWED_MCP_CATALOG_FIELDS: Final[frozenset[str]] = frozenset(
    {"kind", "revision", "server_id", "previous_revision"}
)

# 目录 revision 必填且必须是完整 sha256 摘要（与其它 channel 事件合同一致）。
# 该事件的构造函数在必填 revision 非法时额外附带值类型名，轻量断言则不带；
# assert_checks 保留这一历史消息差异。
_MCP_CATALOG_SCHEMA: Final[_EventSchema] = _EventSchema(
    event_name="McpCatalogEvent",
    allowed_fields=_ALLOWED_MCP_CATALOG_FIELDS,
    checks=(
        (None, "kind", MCP_CATALOG_KINDS),
        (_REVISION_DIGEST_WITH_TYPE, "revision", _NO_ENUM),
        (_validate_optional_id, "server_id", _NO_ENUM),
        (_validate_revision, "previous_revision", _NO_ENUM),
    ),
    assert_checks=(
        (None, "kind", MCP_CATALOG_KINDS),
        (_REVISION_DIGEST_PLAIN, "revision", _NO_ENUM),
        (_validate_optional_id, "server_id", _NO_ENUM),
        (_validate_revision, "previous_revision", _NO_ENUM),
    ),
)


def mcp_catalog_channel_name() -> str:
    """构造 ``mcp.catalog/workspace`` channel 名；目录是工作区后端进程级的。"""
    return channel_name(MCP_CATALOG_CHANNEL_KIND, "workspace")


@dataclass(frozen=True, slots=True)
class McpCatalogEvent:
    """MCP 工具目录变化通知：只有 kind、revision 与 server identity。

    ``published`` 表示目录 revision 推进（新增/修改/初次发布）；
    ``tombstone`` 表示本次推进包含工具删除；``unchanged`` 表示 relist 结果
    与当前 revision 一致、未推进。事件不携带工具 schema 正文、credential 或
    任何 locator；目录的权威状态由 ``McpCatalogOwner`` 持有，consumer 收到
    事件后向 owner 对账。
    """

    kind: str
    revision: str
    server_id: str | None = None
    previous_revision: str | None = None

    def __post_init__(self) -> None:
        _MCP_CATALOG_SCHEMA.values(self)


def assert_mcp_catalog_event_is_lightweight(event: McpCatalogEvent) -> None:
    """类级 + 值级校验：mcp.catalog 通知不得夹带 schema 正文/credential/路径。"""
    _MCP_CATALOG_SCHEMA.full(event)


class McpCatalogEventPublisher:
    """把目录 owner 的轻量变化发布到独立 ``mcp.catalog/workspace`` channel。

    channel 采用 gap 溢出策略：通知可丢，丢失时以 gap 标记提示 consumer 重新
    对账；发布是纯内存操作，不进入 job 队列，也不做 I/O。
    """

    def __init__(
        self,
        *,
        event_service: EventChannelService,
        max_queue_size: int = 64,
    ) -> None:
        if not isinstance(event_service, EventChannelService):
            raise TypeError("McpCatalogEventPublisher 需要 EventChannelService")
        self._channel = event_service.ensure_channel(
            EventChannelSpec(
                name=mcp_catalog_channel_name(),
                overflow_policy="gap",
                max_queue_size=max_queue_size,
                history_size=0,
            )
        )

    @property
    def channel_name(self) -> str:
        return self._channel.name

    def publish(
        self,
        *,
        server_id: str | None,
        kind: str,
        revision: str,
        previous_revision: str | None = None,
    ) -> None:
        """发布一条目录变化通知；契约违规显式抛出，不静默吞掉。"""
        event = McpCatalogEvent(
            kind=kind,
            revision=revision,
            server_id=server_id,
            previous_revision=previous_revision,
        )
        assert_mcp_catalog_event_is_lightweight(event)
        self._channel.publish(event)


__all__ = [
    "CONFIG_LIFECYCLE_KINDS",
    "CONTEXT_SOURCE_KINDS",
    "MCP_CATALOG_KINDS",
    "RESOURCE_STATE_KINDS",
    "RESOURCE_STATE_STATES",
    "ConfigLifecycleEvent",
    "ContextSourceEvent",
    "ContextSourceEventPublisher",
    "McpCatalogEvent",
    "McpCatalogEventPublisher",
    "ResourceStateEvent",
    "ResourceStateEventPublisher",
    "assert_config_lifecycle_event_is_lightweight",
    "assert_context_source_event_is_lightweight",
    "assert_mcp_catalog_event_is_lightweight",
    "assert_resource_state_event_is_lightweight",
    "config_lifecycle_channel_name",
    "context_source_channel_name",
    "mcp_catalog_channel_name",
    "resource_state_channel_name",
]
