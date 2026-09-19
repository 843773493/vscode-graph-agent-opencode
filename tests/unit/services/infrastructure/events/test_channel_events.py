"""resource.state/*、config.lifecycle/*、context.source/* 的 typed 轻量事件合同测试。

覆盖：字段白名单（类级）、值校验（值级）、channel 名构造，以及「构造携带
正文/宿主机路径的事件必须显式报错」的轻量红线。
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.services.infrastructure.events.channel_events import (
    ConfigLifecycleEvent,
    ContextSourceEvent,
    ContextSourceEventPublisher,
    ResourceStateEvent,
    ResourceStateEventPublisher,
    assert_config_lifecycle_event_is_lightweight,
    assert_context_source_event_is_lightweight,
    assert_resource_state_event_is_lightweight,
    config_lifecycle_channel_name,
    context_source_channel_name,
    resource_state_channel_name,
)
from app.services.infrastructure.events.event_channel_service import (
    EventChannelService,
    EventChannelSpec,
    channel_name,
)


def _digest(label: str) -> str:
    """构造合法 revision 样例：完整 sha256 摘要（sha256: + 64 位小写 hex）。"""
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_resource_state_event_contract_accepts_lightweight_event() -> None:
    event = ResourceStateEvent(
        owner_domain="node_debug",
        resource_id="node_debug_process:proc_1",
        state="release_failed",
        kind="state",
        revision=_digest("sample"),
    )
    assert_resource_state_event_is_lightweight(event)
    assert event.kind == "state"


def test_resource_state_publisher_uses_isolated_typed_channel() -> None:
    service = EventChannelService()
    publisher = ResourceStateEventPublisher(
        event_service=service,
        owner_domain="node_debug",
    )
    subscription = service.channel(publisher.channel_name).subscribe(label="test")

    publisher.publish(resource_id="process_123", state="released")

    deliveries = subscription.pending()
    assert len(deliveries) == 1
    assert deliveries[0].event == ResourceStateEvent(
        owner_domain="node_debug",
        resource_id="process_123",
        state="released",
    )
    assert all(not name.startswith("job.events/") for name in service.channel_names)


def test_resource_state_event_rejects_extra_fields_and_bad_values() -> None:
    # 类级：新增/夹带字段显式失败。
    @dataclasses.dataclass(frozen=True)
    class SmuggledResourceStateEvent(ResourceStateEvent):
        content: str = ""

    with pytest.raises(RuntimeError, match="content"):
        assert_resource_state_event_is_lightweight(
            SmuggledResourceStateEvent(
                owner_domain="node_debug",
                resource_id="node_debug_process:proc_1",
                state="released",
            )
        )
    # 值级：宿主机路径不允许作为 identity。
    with pytest.raises(RuntimeError, match="宿主机路径"):
        ResourceStateEvent(
            owner_domain="node_debug",
            resource_id="/home/user/project/file.md",
            state="released",
        )
    with pytest.raises(RuntimeError, match="宿主机路径"):
        ResourceStateEvent(
            owner_domain="node_debug",
            resource_id="C:\\Users\\proj\\file.md",
            state="released",
        )
    # 值级：revision 必须是 sha256 摘要。
    with pytest.raises(RuntimeError, match="sha256"):
        assert_resource_state_event_is_lightweight(
            ResourceStateEvent(
                owner_domain="node_debug",
                resource_id="node_debug_process:proc_1",
                state="released",
                revision="r1",
            )
        )
    # 构造校验：空 identity、未知 state/kind。
    with pytest.raises(ValueError):
        ResourceStateEvent(owner_domain="", resource_id="r", state="released")
    with pytest.raises(ValueError):
        ResourceStateEvent(owner_domain="d", resource_id="r", state="exploded")
    with pytest.raises(ValueError):
        ResourceStateEvent(owner_domain="d", resource_id="r", state="released", kind="boom")


def test_config_lifecycle_event_contract() -> None:
    event = ConfigLifecycleEvent(
        domain="workspace",
        kind="published",
        generation="gen-7",
        revision=_digest("sample"),
    )
    assert_config_lifecycle_event_is_lightweight(event)
    assert config_lifecycle_channel_name("workspace") == "config.lifecycle/workspace"

    @dataclasses.dataclass(frozen=True)
    class SmuggledConfigEvent(ConfigLifecycleEvent):
        merged_document: str = ""

    with pytest.raises(RuntimeError, match="merged_document"):
        assert_config_lifecycle_event_is_lightweight(
            SmuggledConfigEvent(domain="workspace", kind="published")
        )
    with pytest.raises(RuntimeError, match="sha256"):
        ConfigLifecycleEvent(domain="workspace", kind="failed", revision="raw-text")
    with pytest.raises(ValueError, match="kind"):
        ConfigLifecycleEvent(domain="workspace", kind="teleported")
    with pytest.raises(RuntimeError, match="宿主机路径"):
        ConfigLifecycleEvent(domain="/etc/boxteam", kind="published")


def test_context_source_event_contract() -> None:
    event = ContextSourceEvent(
        source_id="src_1",
        source_kind="skill",
        kind="committed",
        revision=_digest("sample"),
        session_id="ses_" + "0" * 32,
        thread_id="thr_" + "0" * 32,
    )
    assert_context_source_event_is_lightweight(event)
    assert context_source_channel_name("ws-1") == "context.source/ws-1"

    @dataclasses.dataclass(frozen=True)
    class SmuggledContextSourceEvent(ContextSourceEvent):
        diff: str = ""

    with pytest.raises(RuntimeError, match="diff"):
        assert_context_source_event_is_lightweight(
            SmuggledContextSourceEvent(source_id="src_1", source_kind="skill", kind="committed")
        )
    # 轻量红线：把正文/diff 塞进 revision 字段必须显式报错。
    with pytest.raises(RuntimeError, match="sha256"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            revision="--- a/file.md\n+++ b/file.md\n+full document body",
        )
    with pytest.raises(RuntimeError, match="宿主机路径"):
        ContextSourceEvent(source_id="/host/path/SKILL.md", source_kind="skill", kind="committed")
    with pytest.raises(ValueError, match="kind"):
        ContextSourceEvent(source_id="src_1", source_kind="skill", kind="attached")
    with pytest.raises(ValueError):
        ContextSourceEvent(source_id="src_1", source_kind="", kind="committed")


def test_all_three_channels_can_be_ensured_in_one_service() -> None:
    """三类 channel 在同一服务内按名字独立创建，互不影响。"""
    service = EventChannelService()
    names = {
        resource_state_channel_name("node_debug"),
        config_lifecycle_channel_name("workspace"),
        context_source_channel_name("ws-1"),
    }
    for name in names:
        service.ensure_channel(EventChannelSpec(name=name, overflow_policy="gap"))
    assert set(service.channel_names) == names


def test_context_source_publisher_publishes_to_named_channel() -> None:
    service = EventChannelService()
    publisher = ContextSourceEventPublisher(event_service=service, scope_id="ws-1")
    assert publisher.channel_name == "context.source/ws-1"
    channel = service.channel("context.source/ws-1")
    assert channel.overflow_policy == "gap"
    subscription = channel.subscribe(label="consumer")

    publisher.publish(
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            revision=_digest("sample"),
            session_id="ses_" + "0" * 32,
            thread_id="thr_" + "0" * 32,
        )
    )
    deliveries = subscription.pending()
    assert len(deliveries) == 1
    assert deliveries[0].sequence == 1
    assert deliveries[0].gap is False
    assert deliveries[0].event.kind == "committed"

    # 契约违规显式抛出，不静默发布。
    with pytest.raises(RuntimeError, match="sha256"):
        publisher.publish(
            ContextSourceEvent(
                source_id="src_1",
                source_kind="skill",
                kind="committed",
                revision="/host/path/SKILL.md",
            )
        )
    with pytest.raises(ValueError, match="空白"):
        ContextSourceEventPublisher(event_service=service, scope_id="  ")
    with pytest.raises(TypeError, match="EventChannelService"):
        ContextSourceEventPublisher(event_service=object(), scope_id="ws-1")  # type: ignore[arg-type]


def test_channel_name_helper_rejects_empty_scope_for_context_source() -> None:
    with pytest.raises(ValueError):
        channel_name("context.source", "")


def test_contracts_reject_smuggled_payloads_in_identity_and_revision() -> None:
    """R2b 审查 M1 回归：前缀走私、超长正文与相对路径都必须显式报错。

    修复前 ``revision`` 只查 ``sha256:`` 前缀，「sha256: + 8KB 正文」静默通过；
    ``generation``/``domain`` 无长度上限；``../etc`` 相对路径不拒。
    """
    body = "正文内容" * 2000  # 约 8KB，远超摘要长度
    # revision 前缀走私：sha256: + 任意正文。
    with pytest.raises(RuntimeError, match="sha256"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            revision="sha256:" + body,
        )
    with pytest.raises(RuntimeError, match="sha256"):
        ResourceStateEvent(
            owner_domain="node_debug",
            resource_id="node_debug_process:proc_1",
            state="released",
            revision="sha256:" + body,
        )
    with pytest.raises(RuntimeError, match="sha256"):
        ConfigLifecycleEvent(
            domain="workspace",
            kind="published",
            revision="sha256:" + body,
        )
    # 摘要长度不足/超长同样拒绝（必须恰好 64 位 hex）。
    with pytest.raises(RuntimeError, match="sha256"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            revision="sha256:abc",
        )
    with pytest.raises(RuntimeError, match="sha256"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            revision=_digest("sample") + "ff",
        )
    # 超长 identity：把正文塞进 generation/domain 字段。
    with pytest.raises(ValueError, match="长度上限"):
        ConfigLifecycleEvent(domain="workspace", kind="published", generation=body)
    with pytest.raises(ValueError, match="长度上限"):
        ResourceStateEvent(
            owner_domain=body,
            resource_id="node_debug_process:proc_1",
            state="released",
        )
    # 相对路径与 home 前缀：不允许作为 identity/可选 id。
    with pytest.raises(RuntimeError, match="路径分隔符"):
        ConfigLifecycleEvent(domain="../etc", kind="published")
    with pytest.raises(RuntimeError, match="路径分隔符"):
        ResourceStateEvent(
            owner_domain="node_debug",
            resource_id="a/../b",
            state="released",
        )
    with pytest.raises(RuntimeError, match="路径分隔符"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="skill",
            kind="committed",
            session_id="~/etc",
        )
    with pytest.raises(RuntimeError, match="路径分隔符"):
        ContextSourceEvent(
            source_id="src/1",
            source_kind="skill",
            kind="committed",
        )
    # R2b 复核 M1-R：source_kind 只做非空检查时 5KB 正文静默通过，必须同 identity 合同。
    with pytest.raises(ValueError, match="长度上限"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="x" * 5000,
            kind="committed",
        )
    with pytest.raises(RuntimeError, match="路径分隔符"):
        ContextSourceEvent(
            source_id="src_1",
            source_kind="../etc",
            kind="committed",
        )
