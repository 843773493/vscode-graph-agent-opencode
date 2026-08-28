from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.resource_manager import ResourceManager


@pytest.mark.asyncio
async def test_turn_cancel_releases_durable_lease_and_stops_turn_resource(tmp_path: Path) -> None:
    stopped: list[str] = []
    manager = ResourceManager(state_path=tmp_path / "resources.json")
    manager.register(
        resource_id="browser_1",
        kind="browser_context",
        lifetime_scope="session",
        stopper=lambda: stopped.append("browser_1"),
    )
    manager.register(
        resource_id="terminal_1",
        kind="terminal",
        lifetime_scope="turn",
        cleanup_policy="destroy_on_turn_end",
        stopper=lambda: stopped.append("terminal_1"),
    )
    manager.acquire(
        resource_id="browser_1",
        turn_stream_id="stream_1",
        lease_id="lease_browser",
        operation_id="op_1",
    )
    manager.acquire(
        resource_id="terminal_1",
        turn_stream_id="stream_1",
        lease_id="lease_terminal",
        operation_id="op_2",
    )

    released = await manager.cancel_turn("stream_1")
    assert [lease.lease_id for lease in released] == ["lease_browser"]
    browser = manager.get("browser_1")
    terminal = manager.get("terminal_1")
    assert browser is not None and browser.status == "running"
    assert terminal is not None and terminal.status == "stopped"
    assert stopped == ["terminal_1"]

    restored = ResourceManager(state_path=tmp_path / "resources.json")
    assert restored.leases_for_turn("stream_1")[0].status == "released"
    assert json.loads((tmp_path / "resources.json").read_text())


@pytest.mark.asyncio
async def test_reconcile_never_assumes_missing_external_resource_is_safe(tmp_path: Path) -> None:
    manager = ResourceManager(state_path=tmp_path / "resources.json")
    manager.register(
        resource_id="mcp_1",
        kind="mcp_connection",
        lifetime_scope="workspace",
    )
    manager.acquire(
        resource_id="mcp_1",
        turn_stream_id="stream_1",
        lease_id="lease_1",
        operation_id="op_1",
    )

    records = manager.reconcile({})
    assert records[0].status == "orphaned"
    assert manager.leases_for_turn("stream_1")[0].status == "reconcile_required"


def test_external_resource_registration_requires_explicit_supported_kind(tmp_path: Path) -> None:
    manager = ResourceManager(state_path=tmp_path / "resources.json")
    manager.register_external(
        resource_id="browser_1",
        kind="browser_context",
    )
    lease = manager.acquire_operation(
        resource_id="browser_1",
        turn_stream_id="stream_1",
        operation_id="open_page",
    )

    assert lease.resource_id == "browser_1"
    with pytest.raises(ValueError, match="不支持的外部持久资源"):
        manager.register_external(
            resource_id="unknown_1",
            kind="unknown_process",
        )
