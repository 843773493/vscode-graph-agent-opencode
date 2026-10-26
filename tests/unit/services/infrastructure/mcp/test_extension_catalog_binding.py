"""ExtensionCatalogBindingRef typed 合同测试（OpenSpec E4 硬探针）。

覆盖：binding 冻结/可验证、目录更新后 sealed ref 不漂移、generation lease
只在 revision 推进时递增、tombstone 后旧调用按 sealed ref 解析、空目录固定
envelope、同名 target 发布前显式拒绝。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.mcp import (
    ExtensionCatalogBindingRef,
    ExtensionTargetBindingInput,
    ExtensionTargetConflictError,
    ExtensionTargetResolutionError,
    McpCatalogOwner,
    build_extension_catalog_binding,
)
from app.services.infrastructure.mcp.extension_catalog import (
    EXTENSION_CATALOG_BINDING_SCHEMA,
    ExtensionCatalogUnavailableError,
)
from tests.support.mcp.session_doubles import (
    FakeMcpSessionFactory as _FakeSessionFactory,
)
from tests.support.mcp.session_doubles import (
    drain_pending_relists as _drain_pending_relists,
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
    event_service: EventChannelService,
    servers: dict[str, object] | None = None,
) -> McpCatalogOwner:
    return McpCatalogOwner(
        raw_config={"servers": servers if servers is not None else _stdio_server()},
        workspace_root=tmp_path,
        event_service=event_service,
        session_factory=factory,
    )


async def _started_owner(
    tmp_path: Path,
    factory: _FakeSessionFactory,
    event_service: EventChannelService,
    servers: dict[str, object] | None = None,
):
    owner = _make_owner(tmp_path, factory, event_service, servers)
    await owner.start()
    return owner


def _expected_binding_hash(ref: ExtensionCatalogBindingRef) -> str:
    """按文档化 payload 形状独立重算 binding_hash，验证可核对性。"""
    payload = {
        "schema": EXTENSION_CATALOG_BINDING_SCHEMA,
        "binding_id": ref.binding_id,
        "catalog_revision": ref.catalog_revision,
        "generation": ref.generation,
        "provider_binding_identity": ref.provider_binding_identity,
        "members": [
            {
                "member_id": member.member_id,
                "member_kind": member.member_kind,
                "schema_hash": member.schema_hash,
                "server_id": member.server_id,
                "revision": member.revision,
            }
            for member in sorted(ref.members, key=lambda item: item.member_id)
        ],
    }
    return sha256_jcs(payload)


async def test_binding_snapshot_frozen_and_verifiable(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service)
    try:
        ref = owner.binding_snapshot()
        assert ref.catalog_revision == owner.catalog_revision()
        assert ref.generation == owner.catalog_generation() == 1
        assert ref.binding_id == f"ext-catalog:v3:1:{ref.catalog_revision}:{sha256_jcs([member.to_dict() for member in ref.members])}"
        assert set(ref.targets) == {"mcp__mini__echo"}
        target = ref.targets["mcp__mini__echo"]
        assert target.origin == "mcp"
        assert target.server_id == "mini"
        assert target.connection_ref == ref.members[0].revision
        assert target.connection_ref == owner.get_tools()[0].metadata["mcp_connection_ref"]
        assert target.schema_hash.startswith("sha256:")
        # 独立按完整输入契约重算，不能用 properties 摘要冒充身份。
        assert target.schema_hash == (
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes({
                    "schema": "extension-input-schema:v2",
                    "input_schema": export_model_tool_json_schema(owner.get_tools()[0]),
                })
            ).hexdigest()
        )
        # binding_hash 可按文档化 payload 独立重算核对。
        assert ref.binding_hash == _expected_binding_hash(ref)
        # binding_hash 使用 domain 的 JCS 合同 token，而非自造 sha256 前缀。
        assert ref.binding_hash.startswith("sha256:jcs:v1:")
        # frozen：targets 不可变。
        with pytest.raises(TypeError):
            ref.targets["new"] = target  # type: ignore[index]
    finally:
        await owner.shutdown()


async def test_binding_snapshot_does_not_drift_after_catalog_update(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service)
    try:
        sealed = owner.binding_snapshot()
        sealed_revision = sealed.catalog_revision
        sealed_hash = sealed.binding_hash
        # 目录新增 target：sealed ref 封存的字段值必须保持不变。
        factory.sessions["mini"].tool_names.append("extra")
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)

        assert sealed.catalog_revision == sealed_revision
        assert sealed.binding_hash == sealed_hash
        assert _expected_binding_hash(sealed) == sealed_hash
        assert set(sealed.targets) == {"mcp__mini__echo"}
        assert sealed.resolve("mcp__mini__echo").server_id == "mini"

        fresh = owner.binding_snapshot()
        assert fresh.catalog_revision != sealed.catalog_revision
        assert fresh.generation == 2
        assert set(fresh.targets) == {"mcp__mini__echo", "mcp__mini__extra"}
    finally:
        await owner.shutdown()


async def test_generation_advances_only_on_revision_change(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service)
    try:
        generation_before = owner.catalog_generation()
        # unchanged relist：generation 不推进。
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        assert owner.catalog_generation() == generation_before

        # 语义变化 relist：generation 恰好 +1。
        factory.sessions["mini"].tool_names[0] = "renamed"
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)
        assert owner.catalog_generation() == generation_before + 1
    finally:
        await owner.shutdown()


async def test_old_call_resolves_by_sealed_ref_after_tombstone(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service)
    try:
        sealed = owner.binding_snapshot()
        # 目录删除 target（tombstone）：旧调用仍按 sealed ref 解析原 target。
        factory.sessions["mini"].tool_names.clear()
        factory.notify_callbacks["mini"]()
        await _drain_pending_relists(owner)

        assert sealed.resolve("mcp__mini__echo").server_id == "mini"
        with pytest.raises(ExtensionTargetResolutionError):
            sealed.resolve("mcp__mini__missing")
        # 新调用按新 sealed binding 解析：旧 target 已不存在，显式失败。
        live = owner.binding_snapshot()
        with pytest.raises(ExtensionTargetResolutionError):
            live.resolve("mcp__mini__echo")
    finally:
        await owner.shutdown()


async def test_empty_catalog_binding_envelope_deterministic(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    owner_a = await _started_owner(
        tmp_path, _FakeSessionFactory(), event_channel_service, servers={}
    )
    owner_b = await _started_owner(
        tmp_path / "b",
        _FakeSessionFactory(),
        event_channel_service,
        servers={},
    )
    try:
        ref_a = owner_a.binding_snapshot()
        ref_b = owner_b.binding_snapshot()
        assert ref_a.targets == {}
        # 空 envelope 的 revision 与 binding 跨实例逐字节一致。
        assert ref_a.catalog_revision == ref_b.catalog_revision
        assert ref_a.binding_id == ref_b.binding_id
        assert ref_a.binding_hash == ref_b.binding_hash
    finally:
        await owner_a.shutdown()
        await owner_b.shutdown()


async def test_cross_server_same_remote_name_uses_declared_namespace(
    tmp_path: Path,
    event_channel_service: EventChannelService,
) -> None:
    """跨 server 同名远端工具按规范命名空间区分，不冲突也不静默覆盖。"""
    servers: dict[str, object] = {
        "alpha": {"enabled": True, "transport": "stdio", "command": "fake-a"},
        "beta": {"enabled": True, "transport": "stdio", "command": "fake-b"},
    }
    factory = _FakeSessionFactory(initial_tools={"alpha": ["echo"], "beta": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service, servers)
    try:
        ref = owner.binding_snapshot()
        assert set(ref.targets) == {"mcp__alpha__echo", "mcp__beta__echo"}
        assert ref.resolve("mcp__alpha__echo").server_id == "alpha"
        assert ref.resolve("mcp__beta__echo").server_id == "beta"
    finally:
        await owner.shutdown()


def test_builder_rejects_duplicate_targets_and_invalid_inputs() -> None:
    base = ExtensionTargetBindingInput(
        target_id="mcp__mini__echo", origin="mcp", input_schema={"type": "object", "properties": {"text": {}}}, server_id="mini",
        connection_ref="mcp-connection:v1:" + "a" * 32 + ":1",
    )
    with pytest.raises(ExtensionTargetConflictError):
        build_extension_catalog_binding(
            catalog_revision="sha256:" + "0" * 64,
            generation=1,
            targets=[base, base],
        )
    with pytest.raises(ValueError):
        build_extension_catalog_binding(
            catalog_revision="sha256:" + "0" * 64,
            generation=0,
            targets=[base],
        )
    with pytest.raises(ValueError):
        build_extension_catalog_binding(
            catalog_revision="not-a-hash",
            generation=1,
            targets=[base],
        )


async def test_restart_same_schema_never_resolves_old_connection(
    tmp_path: Path, event_channel_service: EventChannelService,
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory, event_channel_service)
    try:
        sealed = owner.binding_snapshot()
        old_target = sealed.resolve("mcp__mini__echo")
        assert owner.resolve_execution_target(old_target) is owner.get_tools()[0]
        await owner.shutdown()
        with pytest.raises(ExtensionTargetResolutionError, match="原连接"):
            owner.resolve_execution_target(old_target)
        await owner.start()
        fresh = owner.binding_snapshot()
        fresh_target = fresh.resolve(old_target.target_id)
        assert fresh.catalog_revision == sealed.catalog_revision
        assert fresh_target.schema_hash == old_target.schema_hash
        assert fresh_target.connection_ref != old_target.connection_ref
        assert fresh.binding_id != sealed.binding_id
        assert fresh.binding_hash != sealed.binding_hash
        with pytest.raises(ExtensionTargetResolutionError, match="原连接"):
            owner.resolve_execution_target(old_target)
        assert owner.resolve_execution_target(fresh_target) is owner.get_tools()[0]
    finally:
        await owner.shutdown()


async def test_same_server_and_generation_are_isolated_between_owner_instances(
    tmp_path: Path, event_channel_service: EventChannelService,
) -> None:
    owners = [await _started_owner(
        tmp_path / name, _FakeSessionFactory(initial_tools={"mini": ["echo"]}), event_channel_service,
    ) for name in ("workspace-a", "workspace-b")]
    try:
        left, right = (owner.binding_snapshot() for owner in owners)
        assert left.catalog_revision == right.catalog_revision
        assert left.generation == right.generation == 1
        assert left.binding_id != right.binding_id
        assert left.resolve("mcp__mini__echo").connection_ref != right.resolve("mcp__mini__echo").connection_ref
        with pytest.raises(ExtensionTargetResolutionError, match="原连接"):
            owners[1].resolve_execution_target(left.resolve("mcp__mini__echo"))
        assert owners[0].resolve_execution_target(left.resolve("mcp__mini__echo")) is owners[0].get_tools()[0]
    finally:
        for owner in owners:
            await owner.shutdown()


@pytest.mark.parametrize("connection_ref", [None, "sha256:" + "a" * 64,
    "mcp-connection:v1:" + "a" * 32 + ":0",
    "mcp-connection:v1:" + "a" * 32 + ":01",
    "mcp-connection:v1:" + "a" * 32 + ":１",
])
def test_mcp_binding_requires_exact_connection_identity(connection_ref: str | None) -> None:
    with pytest.raises(ExtensionCatalogUnavailableError, match="精确连接身份"):
        build_extension_catalog_binding(
            catalog_revision="sha256:" + "a" * 64, generation=1,
            targets=[ExtensionTargetBindingInput(
                target_id="mcp__mini__echo", origin="mcp", server_id="mini",
                input_schema={"type": "object"}, connection_ref=connection_ref,
            )],
        )
