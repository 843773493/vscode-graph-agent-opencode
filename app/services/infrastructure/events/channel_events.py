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


def _validate_field_whitelist(
    event_type: type,
    allowed: frozenset[str],
    *,
    label: str,
) -> None:
    """类级字段白名单：新增/改名/夹带字段都会显式失败，而不是恒真通过。"""
    declared = {field.name for field in fields(event_type)}
    unexpected = declared - allowed
    if unexpected:
        raise RuntimeError(
            f"{label} 声明了未允许的字段: " + ",".join(sorted(unexpected))
        )


# --------------------------------------------------------------------------
# resource.state/{owner_domain}
# --------------------------------------------------------------------------

_ALLOWED_RESOURCE_STATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"owner_domain", "resource_id", "state", "kind", "revision"}
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
        _validate_identity(self.owner_domain, field_name="ResourceStateEvent.owner_domain")
        _validate_identity(self.resource_id, field_name="ResourceStateEvent.resource_id")
        if self.state not in RESOURCE_STATE_STATES:
            raise ValueError(
                f"ResourceStateEvent.state 必须是 {sorted(RESOURCE_STATE_STATES)} 之一: "
                f"{self.state!r}"
            )
        if self.kind not in RESOURCE_STATE_KINDS:
            raise ValueError(
                f"ResourceStateEvent.kind 必须是 {sorted(RESOURCE_STATE_KINDS)} 之一: "
                f"{self.kind!r}"
            )
        _validate_revision(self.revision, field_name="ResourceStateEvent.revision")


def assert_resource_state_event_is_lightweight(event: ResourceStateEvent) -> None:
    """类级 + 值级校验：resource.state 通知不得夹带正文/credential/宿主机路径。"""
    _validate_field_whitelist(
        type(event),
        _ALLOWED_RESOURCE_STATE_FIELDS,
        label="ResourceStateEvent",
    )
    _validate_identity(event.owner_domain, field_name="ResourceStateEvent.owner_domain")
    _validate_identity(event.resource_id, field_name="ResourceStateEvent.resource_id")
    if event.state not in RESOURCE_STATE_STATES:
        raise ValueError(
            f"ResourceStateEvent.state 必须是 {sorted(RESOURCE_STATE_STATES)} 之一: "
            f"{event.state!r}"
        )
    if event.kind not in RESOURCE_STATE_KINDS:
        raise ValueError(
            f"ResourceStateEvent.kind 必须是 {sorted(RESOURCE_STATE_KINDS)} 之一: "
            f"{event.kind!r}"
        )
    _validate_revision(event.revision, field_name="ResourceStateEvent.revision")


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
        _validate_identity(self.domain, field_name="ConfigLifecycleEvent.domain")
        if self.kind not in CONFIG_LIFECYCLE_KINDS:
            raise ValueError(
                f"ConfigLifecycleEvent.kind 必须是 {sorted(CONFIG_LIFECYCLE_KINDS)} 之一: "
                f"{self.kind!r}"
            )
        _validate_optional_id(self.generation, field_name="ConfigLifecycleEvent.generation")
        _validate_revision(self.revision, field_name="ConfigLifecycleEvent.revision")


def assert_config_lifecycle_event_is_lightweight(event: ConfigLifecycleEvent) -> None:
    """类级 + 值级校验：config.lifecycle 通知不得夹带配置正文或路径。"""
    _validate_field_whitelist(
        type(event),
        _ALLOWED_CONFIG_LIFECYCLE_FIELDS,
        label="ConfigLifecycleEvent",
    )
    _validate_identity(event.domain, field_name="ConfigLifecycleEvent.domain")
    if event.kind not in CONFIG_LIFECYCLE_KINDS:
        raise ValueError(
            f"ConfigLifecycleEvent.kind 必须是 {sorted(CONFIG_LIFECYCLE_KINDS)} 之一: "
            f"{event.kind!r}"
        )
    _validate_optional_id(event.generation, field_name="ConfigLifecycleEvent.generation")
    _validate_revision(event.revision, field_name="ConfigLifecycleEvent.revision")


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
        _validate_identity(self.source_id, field_name="ContextSourceEvent.source_id")
        _validate_identity(
            self.source_kind, field_name="ContextSourceEvent.source_kind"
        )
        if self.kind not in CONTEXT_SOURCE_KINDS:
            raise ValueError(
                f"ContextSourceEvent.kind 必须是 {sorted(CONTEXT_SOURCE_KINDS)} 之一: "
                f"{self.kind!r}"
            )
        _validate_revision(self.revision, field_name="ContextSourceEvent.revision")
        _validate_optional_id(self.session_id, field_name="ContextSourceEvent.session_id")
        _validate_optional_id(self.thread_id, field_name="ContextSourceEvent.thread_id")


def assert_context_source_event_is_lightweight(event: ContextSourceEvent) -> None:
    """类级 + 值级校验：context.source 通知不得夹带正文/credential/宿主机路径。"""
    _validate_field_whitelist(
        type(event),
        _ALLOWED_CONTEXT_SOURCE_FIELDS,
        label="ContextSourceEvent",
    )
    _validate_identity(event.source_id, field_name="ContextSourceEvent.source_id")
    _validate_identity(
        event.source_kind, field_name="ContextSourceEvent.source_kind"
    )
    if event.kind not in CONTEXT_SOURCE_KINDS:
        raise ValueError(
            f"ContextSourceEvent.kind 必须是 {sorted(CONTEXT_SOURCE_KINDS)} 之一: "
            f"{event.kind!r}"
        )
    _validate_revision(event.revision, field_name="ContextSourceEvent.revision")
    _validate_optional_id(event.session_id, field_name="ContextSourceEvent.session_id")
    _validate_optional_id(event.thread_id, field_name="ContextSourceEvent.thread_id")


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
        if self.kind not in MCP_CATALOG_KINDS:
            raise ValueError(
                f"McpCatalogEvent.kind 必须是 {sorted(MCP_CATALOG_KINDS)} 之一: "
                f"{self.kind!r}"
            )
        # 目录 revision 必填且必须是完整 sha256 摘要（与其它 channel 事件合同一致）。
        if not isinstance(self.revision, str) or not _SHA256_DIGEST_PATTERN.fullmatch(
            self.revision
        ):
            raise RuntimeError(
                "McpCatalogEvent.revision 必须是 sha256: 摘要（sha256: + 64 位小写 hex）: "
                f"{type(self.revision).__name__}"
            )
        _validate_optional_id(self.server_id, field_name="McpCatalogEvent.server_id")
        _validate_revision(
            self.previous_revision,
            field_name="McpCatalogEvent.previous_revision",
        )


def assert_mcp_catalog_event_is_lightweight(event: McpCatalogEvent) -> None:
    """类级 + 值级校验：mcp.catalog 通知不得夹带 schema 正文/credential/路径。"""
    _validate_field_whitelist(
        type(event),
        _ALLOWED_MCP_CATALOG_FIELDS,
        label="McpCatalogEvent",
    )
    if event.kind not in MCP_CATALOG_KINDS:
        raise ValueError(
            f"McpCatalogEvent.kind 必须是 {sorted(MCP_CATALOG_KINDS)} 之一: "
            f"{event.kind!r}"
        )
    if not isinstance(event.revision, str) or not _SHA256_DIGEST_PATTERN.fullmatch(
        event.revision
    ):
        raise RuntimeError(
            "McpCatalogEvent.revision 必须是 sha256: 摘要（sha256: + 64 位小写 hex）"
        )
    _validate_optional_id(event.server_id, field_name="McpCatalogEvent.server_id")
    _validate_revision(
        event.previous_revision,
        field_name="McpCatalogEvent.previous_revision",
    )


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
