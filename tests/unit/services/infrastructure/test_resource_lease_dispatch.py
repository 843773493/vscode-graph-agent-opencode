"""原lease的派发阶段与正式回执共享唯一持久写链。"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)


@pytest.fixture
def dispatch_ledger(tmp_path: Path) -> tuple[Path, ExternalResourceLeaseLedger]:
    path = tmp_path / "resources.json"
    ledger = ExternalResourceLeaseLedger(state_path=path)
    ledger.register_external(resource_id="terminal", kind="terminal", lifetime_scope="workspace")
    ledger.acquire_operation(resource_id="terminal", turn_stream_id="holder", operation_id="operation", lease_id="original")
    return path, ledger


@pytest.mark.parametrize("fault", ["fsync", "replace"])
@pytest.mark.parametrize("stage", ["dispatch", "confirm", "release"])
def test_dispatch_publication_failure_keeps_exact_durable_phase_until_explicit_retry(
    dispatch_ledger: tuple[Path, ExternalResourceLeaseLedger], monkeypatch: pytest.MonkeyPatch,
    fault: str, stage: str,
) -> None:
    path, ledger = dispatch_ledger
    if stage != "dispatch":
        ledger.mark_operation_dispatched("original")
    before, lease = path.read_bytes(), ledger.get_lease("original")
    native = OSError(28, "派发阶段原生写盘失败", str(path))
    change = ledger.mark_operation_dispatched if stage == "dispatch" else partial(ledger.finish_operation_dispatch, release=stage == "release")
    with monkeypatch.context() as io_fault:
        if fault == "fsync":
            def fail(_descriptor: int) -> None:
                raise native
            io_fault.setattr(os, "fsync", fail)
        else:
            replace = Path.replace

            def fail_replace(source: Path, target: Path) -> Path:
                if target == path:
                    raise native
                return replace(source, target)
            io_fault.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError) as caught:
            change("original")
        assert caught.value.__cause__ is native
    assert path.read_bytes() == before and ledger.get_lease("original") == lease
    assert ExternalResourceLeaseLedger(state_path=path).get_lease("original") == lease
    change("original")
    retained = ExternalResourceLeaseLedger(state_path=path).get_lease("original")
    assert retained is not None and retained.status == {"dispatch": "dispatching", "confirm": "active", "release": "released"}[stage]
    assert retained.lease_id == "original" and retained.operation_id == "operation"


async def test_dispatch_rejects_replay_and_plain_release_but_remains_visible_to_all_lease_queries(
    dispatch_ledger: tuple[Path, ExternalResourceLeaseLedger],
) -> None:
    path, ledger = dispatch_ledger
    ledger.mark_operation_dispatched("original")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="拒绝再次派发"):
        ledger.mark_operation_dispatched("original")
    with pytest.raises(RuntimeError, match="不能普通释放"):
        ledger.release("original")
    assert ledger.release_turn_leases("holder") == [] and path.read_bytes() == before
    cold = ExternalResourceLeaseLedger(state_path=path)
    lease = cold.get_lease("original")
    assert lease is not None and lease.status == "dispatching"
    assert cold.leases_for_resource("terminal") == [lease]
    assert cold.leases_for_resources(["terminal"]) == {"terminal": [lease]}
    assert cold.leases_for_turn("holder") == [lease]
    await cold.reconcile({"terminal": "running"}, kind="terminal")
    pending = cold.get_lease("original")
    assert pending is not None and pending.status == "reconcile_required"
    assert cold.release_turn_leases("holder") == []
    with pytest.raises(RuntimeError, match="缺少本次待核实派发"):
        cold.finish_operation_dispatch("original")
