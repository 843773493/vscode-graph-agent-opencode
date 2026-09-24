"""McpCatalogActivationBinder focused 合同测试（OpenSpec E4 第二段）。

覆盖：binding+指引原子冻结、默认 Turn 不漂移、model_call 下一安全边界刷新、
relist 失败 fail closed、revision 冲突显式拒绝、未启动显式失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import (
    MCP_GUIDANCE_SOURCE_ID,
    DurableMcpCatalogActivationSaver,
    McpCatalogActivationBinder,
    McpCatalogActivationConflictError,
    McpCatalogActivationSnapshot,
    McpCatalogOwner,
    McpCatalogRelistError,
    McpToolGuidanceProducer,
    McpToolGuidanceSourceRegistration,
    build_extension_catalog_binding,
    mcp_catalog_activation_payload,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingMismatchError,
    ExtensionDispatchBindingRef,
)
from app.services.orchestration.resource_activation.contracts import (
    ResourceActivationPolicySnapshot,
)
from tests.support.mcp_session_doubles import (
    FakeMcpSessionFactory as _FakeSessionFactory,
)
from tests.support.mcp_session_doubles import (
    drain_pending_relists as _drain_pending_relists,
)


class _MemorySaver:
    def __init__(self) -> None:
        self.snapshots: list[McpCatalogActivationSnapshot] = []

    async def save_mcp_catalog_activation_snapshot(
        self, snapshot: McpCatalogActivationSnapshot
    ) -> None:
        self.snapshots.append(snapshot)


class _GuidancePort:
    def __init__(self) -> None:
        self.registrations: list[McpToolGuidanceSourceRegistration] = []
        self.error: Exception | None = None

    def register_tail_only_guidance(
        self, registration: McpToolGuidanceSourceRegistration
    ) -> None:
        if self.error is not None:
            raise self.error
        self.registrations.append(registration)


def _policy(overrides: dict[str, str] | None = None) -> ResourceActivationPolicySnapshot:
    return ResourceActivationPolicySnapshot.from_config(
        {
            "context": {
                "resource_activation": {
                    "default_boundary": "turn",
                    "overrides": overrides or {},
                }
            }
        }
    )


def _stdio_server(server_id: str = "mini") -> dict[str, object]:
    return {
        server_id: {
            "enabled": True,
            "transport": "stdio",
            "command": "fake-server",
        }
    }


def _make_owner(
    tmp_path: Path,
    factory: _FakeSessionFactory,
    servers: dict[str, object] | None = None,
) -> McpCatalogOwner:
    return McpCatalogOwner(
        raw_config={"servers": servers if servers is not None else _stdio_server()},
        workspace_root=tmp_path,
        event_service=EventChannelService(),
        session_factory=factory,
    )


async def test_freeze_turn_seals_binding_and_guidance_atomically(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        # 原子性：同一 snapshot 内 binding 与指引 revision 逐字段一致。
        assert snapshot.snapshot_kind == "turn"
        assert snapshot.effective_boundary == "turn"
        assert snapshot.binding_ref.catalog_revision == snapshot.guidance.catalog_revision
        assert snapshot.binding_ref.catalog_revision == owner.catalog_revision()
        assert snapshot.binding_ref.generation == owner.catalog_generation()
        assert [
            entry.tool_id for entry in snapshot.guidance.entries
        ] == ["mcp__mini__echo"]
        assert snapshot.activation_provenance_hash.startswith("sha256:")
        assert len(saver.snapshots) == 1
    finally:
        await owner.shutdown()


async def test_freeze_registers_tail_only_guidance_before_saving_snapshot(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    guidance_port = _GuidancePort()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy(),
        saver=saver,
        guidance_source_port=guidance_port,
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        assert len(guidance_port.registrations) == 1
        registration = guidance_port.registrations[0]
        assert registration.source_id == MCP_GUIDANCE_SOURCE_ID
        assert registration.activation_snapshot_id == snapshot.activation_snapshot_id
        assert registration.catalog_revision == snapshot.binding_ref.catalog_revision
        assert registration.guidance_revision == snapshot.guidance.guidance_revision
        assert registration.root_placement == "tail_only"
        assert len(saver.snapshots) == 1
    finally:
        await owner.shutdown()


async def test_guidance_registration_failure_blocks_snapshot_save(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    guidance_port = _GuidancePort()
    guidance_port.error = RuntimeError("guidance owner unavailable")
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy(),
        saver=saver,
        guidance_source_port=guidance_port,
    )
    try:
        with pytest.raises(RuntimeError, match="guidance owner unavailable"):
            await binder.freeze_turn_activation(
                owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
            )
        assert saver.snapshots == []
    finally:
        await owner.shutdown()


async def test_default_turn_model_call_reuses_parent(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        parent = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        prepared = await binder.prepare_model_call_activation(
            parent=parent, model_call_id="mc_1"
        )
        # 默认 turn 策略：同一 Turn 内 snapshot 不漂移，不重复冻结。
        assert prepared is parent
        assert len(saver.snapshots) == 1
        # 通知能力 server 不参与无通知 relist：list_tools 仅 startup 一次。
        assert factory.sessions["mini"].list_calls == 1
    finally:
        await owner.shutdown()


async def test_model_call_boundary_refreshes_at_next_safe_boundary(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy({"mcp_tool_catalog": "model_call"}),
        saver=saver,
    )
    try:
        parent = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        assert parent.effective_boundary == "model_call"
        # 目录在两个 model call 之间发布新增 target。
        factory.sessions["mini"].tool_names.append("extra")
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)

        prepared = await binder.prepare_model_call_activation(
            parent=parent, model_call_id="mc_2"
        )
        assert prepared is not parent
        assert prepared.snapshot_kind == "model_call"
        assert prepared.parent_activation_id == parent.activation_snapshot_id
        assert prepared.model_call_id == "mc_2"
        assert prepared.binding_ref.catalog_revision == owner.catalog_revision()
        assert prepared.binding_ref.generation > parent.binding_ref.generation
        assert prepared.guidance.added_tool_ids == ("mcp__mini__extra",)
        assert len(saver.snapshots) == 2
    finally:
        await owner.shutdown()


async def test_relist_failure_fails_closed(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    factory.sessions["mini"].supports_notifications = False
    factory.sessions["mini"].relist_error = RuntimeError("relist 爆炸")
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        with pytest.raises(McpCatalogRelistError):
            await binder.freeze_turn_activation(
                owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
            )
        assert saver.snapshots == []
    finally:
        await owner.shutdown()


def test_snapshot_rejects_binding_guidance_revision_mismatch() -> None:

    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    guidance = McpToolGuidanceProducer().produce(
        catalog_revision="sha256:" + "1" * 64,
        descriptors=[],
        tools_by_id={},
    )
    with pytest.raises(McpCatalogActivationConflictError):
        McpCatalogActivationSnapshot(
            activation_snapshot_id="mcp-activation:s:thr:turn",
            snapshot_kind="turn",
            owner_session_id="ses_1",
            owner_thread_id="thr_1",
            turn_id="turn_1",
            effective_boundary="turn",
            binding_ref=binding,
            guidance=guidance,
            dispatch_binding=ExtensionDispatchBindingRef(
                binding_ref=binding,
                guidance_revision=guidance.guidance_revision,
                activation_policy_revision="resource-activation-policy:v1:test",
                owner_session_id="ses_1",
                owner_thread_id="thr_1",
                turn_id="turn_1",
            ),
        )


async def test_freeze_before_owner_start_fails_closed(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    with pytest.raises(RuntimeError):
        await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
    assert saver.snapshots == []


class _MemoryBodyStore:
    """受保护 body store 的内存替身：只保存 typed identity -> 正文映射。"""

    def __init__(self) -> None:
        self.bodies: dict[tuple[str, str, str], dict[str, object]] = {}
        self.fail_next_write: Exception | None = None

    def write_activation_snapshot(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        body: object,
    ) -> None:
        if self.fail_next_write is not None:
            error = self.fail_next_write
            self.fail_next_write = None
            raise error
        self.bodies[(owner_session_id, owner_thread_id, activation_snapshot_id)] = dict(
            body  # type: ignore[arg-type]
        )

    def read_activation_snapshot(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
    ) -> dict[str, object] | None:
        return self.bodies.get(
            (owner_session_id, owner_thread_id, activation_snapshot_id)
        )


async def test_freeze_seals_dispatch_binding_with_own_hash(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    saver = _MemorySaver()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        dispatch = snapshot.dispatch_binding
        assert isinstance(dispatch, ExtensionDispatchBindingRef)
        # 独立 hash 使用 domain JCS 合同，且与 binding_hash 分离。
        assert dispatch.dispatch_binding_hash.startswith("sha256:jcs:v1:")
        assert dispatch.dispatch_binding_hash != snapshot.binding_ref.binding_hash
        assert dispatch.binding_ref.binding_hash == snapshot.binding_ref.binding_hash
        assert dispatch.guidance_revision == snapshot.guidance.guidance_revision
        assert dispatch.activation_policy_revision == _policy().revision
        # 冻结的 dispatch 与 snapshot provenance 原子绑定：provenance 覆盖 hash。
        assert snapshot.activation_provenance_hash.startswith("sha256:")
        assert dispatch.verify() is dispatch
    finally:
        await owner.shutdown()


async def test_dispatch_hash_changes_when_guidance_policy_changes(
    tmp_path: Path,
) -> None:
    """同一 catalog binding 下，指引/策略变化必须改变独立 dispatch hash。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    base = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision="sha256:" + "1" * 64,
        activation_policy_revision="resource-activation-policy:v1:aaa",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
    )
    # 指引 revision 变化 → dispatch hash 变化，但 binding_hash 不变。
    guidance_changed = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision="sha256:" + "2" * 64,
        activation_policy_revision="resource-activation-policy:v1:aaa",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
    )
    assert guidance_changed.dispatch_binding_hash != base.dispatch_binding_hash
    assert guidance_changed.binding_ref.binding_hash == base.binding_ref.binding_hash
    # 策略 revision 变化 → dispatch hash 变化。
    policy_changed = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision="sha256:" + "1" * 64,
        activation_policy_revision="resource-activation-policy:v1:bbb",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
    )
    assert policy_changed.dispatch_binding_hash != base.dispatch_binding_hash
    # 运行 identity 不进入内容 hash：仅 owner/turn 变化不改变 dispatch hash。
    identity_changed = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision="sha256:" + "1" * 64,
        activation_policy_revision="resource-activation-policy:v1:aaa",
        owner_session_id="ses_2",
        owner_thread_id="thr_2",
        turn_id="turn_2",
    )
    assert identity_changed.dispatch_binding_hash == base.dispatch_binding_hash


async def test_durable_saver_persists_and_restores_dispatch_binding(
    tmp_path: Path,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    body_store = _MemoryBodyStore()
    saver = DurableMcpCatalogActivationSaver(body_store=body_store)
    binder = McpCatalogActivationBinder(
        catalog_owner=owner, policy=_policy(), saver=saver
    )
    try:
        snapshot = await binder.freeze_turn_activation(
            owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
        )
        key = ("ses_1", "thr_1", snapshot.activation_snapshot_id)
        assert key in body_store.bodies
        restored = saver.load_mcp_catalog_activation_snapshot(
            owner_session_id="ses_1",
            owner_thread_id="thr_1",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
        assert restored.dispatch_binding_hash == (
            snapshot.dispatch_binding.dispatch_binding_hash
        )
        # 恢复只读受保护 snapshot：不访问当前 MCP 目录，同名新 target 不生效。
        factory.sessions["mini"].tool_names.append("smuggled")
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        again = saver.load_mcp_catalog_activation_snapshot(
            owner_session_id="ses_1",
            owner_thread_id="thr_1",
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
        assert again.dispatch_binding_hash == restored.dispatch_binding_hash
        assert "mcp__mini__smuggled" not in {
            target.target_id for target in again.binding_ref.targets.values()
        }
    finally:
        await owner.shutdown()


async def test_missing_history_binding_raises_extension_catalog_unavailable(
    tmp_path: Path,
) -> None:
    """历史 binding 丢失必须显式 extension-catalog-unavailable，不回退当前目录。"""
    body_store = _MemoryBodyStore()
    saver = DurableMcpCatalogActivationSaver(body_store=body_store)
    with pytest.raises(ExtensionCatalogUnavailableError) as error:
        saver.load_mcp_catalog_activation_snapshot(
            owner_session_id="ses_x",
            owner_thread_id="thr_x",
            activation_snapshot_id="mcp-activation:missing",
        )
    assert error.value.code == "extension-catalog-unavailable"
    assert "extension-catalog-unavailable" in str(error.value)
    # 不完整/损坏的封存正文同样显式拒绝。
    body_store.bodies[("ses_x", "thr_x", "mcp-activation:broken")] = {
        "schema": "mcp-catalog-activation:v1"
    }
    with pytest.raises(ExtensionCatalogUnavailableError):
        saver.load_mcp_catalog_activation_snapshot(
            owner_session_id="ses_x",
            owner_thread_id="thr_x",
            activation_snapshot_id="mcp-activation:broken",
        )


async def test_durable_saver_failure_never_half_publishes(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = _make_owner(tmp_path, factory)
    await owner.start()
    body_store = _MemoryBodyStore()
    body_store.fail_next_write = RuntimeError("body store unavailable")
    saver = DurableMcpCatalogActivationSaver(body_store=body_store)
    guidance_port = _GuidancePort()
    binder = McpCatalogActivationBinder(
        catalog_owner=owner,
        policy=_policy(),
        saver=saver,
        guidance_source_port=guidance_port,
    )
    try:
        with pytest.raises(RuntimeError, match="body store unavailable"):
            await binder.freeze_turn_activation(
                owner_session_id="ses_1", owner_thread_id="thr_1", turn_id="turn_1"
            )
        # 持久化失败：没有任何 snapshot 正文落盘，也没有可恢复的 binding。
        assert body_store.bodies == {}
        with pytest.raises(ExtensionCatalogUnavailableError):
            saver.load_mcp_catalog_activation_snapshot(
                owner_session_id="ses_1",
                owner_thread_id="thr_1",
                activation_snapshot_id="mcp-activation:ses_1:thr_1:turn_1",
            )
    finally:
        await owner.shutdown()


def test_snapshot_rejects_dispatch_binding_mismatch() -> None:
    """dispatch binding 与 binding/指引不一致时必须拒绝原子冻结（不做半发布）。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    guidance = McpToolGuidanceProducer().produce(
        catalog_revision="sha256:" + "0" * 64,
        descriptors=[],
        tools_by_id={},
    )
    other_binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=2,
        targets=[],
    )
    with pytest.raises(McpCatalogActivationConflictError):
        McpCatalogActivationSnapshot(
            activation_snapshot_id="mcp-activation:s:thr:turn",
            snapshot_kind="turn",
            owner_session_id="ses_1",
            owner_thread_id="thr_1",
            turn_id="turn_1",
            effective_boundary="turn",
            binding_ref=binding,
            guidance=guidance,
            dispatch_binding=ExtensionDispatchBindingRef(
                binding_ref=other_binding,
                guidance_revision=guidance.guidance_revision,
                activation_policy_revision="resource-activation-policy:v1:test",
                owner_session_id="ses_1",
                owner_thread_id="thr_1",
                turn_id="turn_1",
            ),
        )


def test_payload_round_trip_carries_dispatch_hash() -> None:
    """受保护正文必须携带可恢复的 dispatch binding 与独立 hash。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    guidance = McpToolGuidanceProducer().produce(
        catalog_revision="sha256:" + "0" * 64,
        descriptors=[],
        tools_by_id={},
    )
    dispatch = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision=guidance.guidance_revision,
        activation_policy_revision="resource-activation-policy:v1:test",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
    )
    snapshot = McpCatalogActivationSnapshot(
        activation_snapshot_id="mcp-activation:s:thr:turn",
        snapshot_kind="turn",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
        effective_boundary="turn",
        binding_ref=binding,
        guidance=guidance,
        dispatch_binding=dispatch,
    )
    payload = mcp_catalog_activation_payload(snapshot)
    restored = ExtensionDispatchBindingRef.from_sealed_snapshot(
        payload["extension_dispatch_binding"]
    )
    assert restored.dispatch_binding_hash == dispatch.dispatch_binding_hash
    assert payload["activation_provenance_hash"] == snapshot.activation_provenance_hash


def test_from_sealed_snapshot_rejects_tampered_dispatch_hash() -> None:
    """封存正文 hash 被篡改时必须显式拒绝 dispatch。"""
    binding = build_extension_catalog_binding(
        catalog_revision="sha256:" + "0" * 64,
        generation=1,
        targets=[],
    )
    dispatch = ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision="sha256:" + "1" * 64,
        activation_policy_revision="resource-activation-policy:v1:test",
        owner_session_id="ses_1",
        owner_thread_id="thr_1",
        turn_id="turn_1",
    )
    tampered = dispatch.to_dict()
    tampered["extension_dispatch_binding_hash"] = "sha256:jcs:v1:" + "0" * 64
    with pytest.raises(ExtensionDispatchBindingMismatchError):
        ExtensionDispatchBindingRef.from_sealed_snapshot(tampered)


def test_from_sealed_snapshot_rejects_non_mapping_and_missing_fields() -> None:
    with pytest.raises(ExtensionCatalogUnavailableError):
        ExtensionDispatchBindingRef.from_sealed_snapshot(None)
    with pytest.raises(ExtensionCatalogUnavailableError):
        ExtensionDispatchBindingRef.from_sealed_snapshot({"binding_id": "x"})
