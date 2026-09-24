"""ExtensionCatalogBindingRef typed 合同测试（OpenSpec E4 硬探针）。

覆盖：binding 冻结/可验证、目录更新后 sealed ref 不漂移、generation lease
只在 revision 推进时递增、tombstone 后旧调用按 sealed ref 解析、空目录固定
envelope、同名 target 发布前显式拒绝。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.domain.itemized.hashing import sha256_jcs
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
    extension_args_fingerprint,
)
from tests.support.mcp_session_doubles import (
    FakeMcpSessionFactory as _FakeSessionFactory,
)
from tests.support.mcp_session_doubles import (
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
    servers: dict[str, object] | None = None,
) -> McpCatalogOwner:
    return McpCatalogOwner(
        raw_config={"servers": servers if servers is not None else _stdio_server()},
        workspace_root=tmp_path,
        event_service=EventChannelService(),
        session_factory=factory,
    )


async def _started_owner(
    tmp_path: Path,
    factory: _FakeSessionFactory,
    servers: dict[str, object] | None = None,
):
    owner = _make_owner(tmp_path, factory, servers)
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
        "targets": [
            {
                "target_id": t.target_id,
                "origin": t.origin,
                "server_id": t.server_id,
                "schema_hash": t.schema_hash,
            }
            for t in sorted(ref.targets.values(), key=lambda item: item.target_id)
        ],
    }
    return sha256_jcs(payload)


async def test_binding_snapshot_frozen_and_verifiable(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory)
    try:
        ref = owner.binding_snapshot()
        assert ref.catalog_revision == owner.catalog_revision()
        assert ref.generation == owner.catalog_generation() == 1
        assert ref.binding_id == f"ext-catalog:v1:1:{ref.catalog_revision}"
        assert set(ref.targets) == {"mcp__mini__echo"}
        target = ref.targets["mcp__mini__echo"]
        assert target.origin == "mcp"
        assert target.server_id == "mini"
        assert target.schema_hash.startswith("sha256:")
        # schema_hash 与目录语义 revision 同源：对同一 args 重算一致。
        assert target.schema_hash == (
            "sha256:"
            + hashlib.sha256(
                extension_args_fingerprint(
                    owner.get_tools()[0].args
                ).encode()
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
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory)
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


async def test_generation_advances_only_on_revision_change(tmp_path: Path) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory)
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
) -> None:
    factory = _FakeSessionFactory(initial_tools={"mini": ["echo"]})
    owner = await _started_owner(tmp_path, factory)
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


async def test_empty_catalog_binding_envelope_deterministic(tmp_path: Path) -> None:
    owner_a = await _started_owner(tmp_path, _FakeSessionFactory(), servers={})
    owner_b = await _started_owner(
        tmp_path / "b", _FakeSessionFactory(), servers={}
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
) -> None:
    """跨 server 同名远端工具按规范命名空间区分，不冲突也不静默覆盖。"""
    servers: dict[str, object] = {
        "alpha": {"enabled": True, "transport": "stdio", "command": "fake-a"},
        "beta": {"enabled": True, "transport": "stdio", "command": "fake-b"},
    }
    factory = _FakeSessionFactory(
        initial_tools={"alpha": ["echo"], "beta": ["echo"]}
    )
    owner = await _started_owner(tmp_path, factory, servers)
    try:
        ref = owner.binding_snapshot()
        assert set(ref.targets) == {"mcp__alpha__echo", "mcp__beta__echo"}
        assert ref.resolve("mcp__alpha__echo").server_id == "alpha"
        assert ref.resolve("mcp__beta__echo").server_id == "beta"
    finally:
        await owner.shutdown()


def test_builder_rejects_duplicate_targets_and_invalid_inputs() -> None:
    base = ExtensionTargetBindingInput(
        target_id="mcp__mini__echo", origin="mcp", args={"text": {}}, server_id="mini"
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
