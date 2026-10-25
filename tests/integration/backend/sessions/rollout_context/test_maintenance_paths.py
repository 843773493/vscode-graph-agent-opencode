"""产品生成的历史在维护拒绝后保持原归属、正文和可恢复性。"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import workspace_context
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_cold_rollout_history import (
    RuntimeCase,
    _history_request,
    history_client,
    materialized_history_owner,
    runner_case,
    runtime_case,
)

__all__ = ["history_client", "materialized_history_owner", "runner_case", "runtime_case"]


@pytest.mark.parametrize("damage", ["manifest", "segment", "symlink", "directory", "inspection"])
async def test_initialize_rejection_preserves_product_history_then_recovers(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str, str], damage: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    if damage == "symlink" and os.name == "nt":
        pytest.skip("Windows 符号链接权限留待实机验收")
    session_id, thread_id, turn_id = materialized_history_owner
    container = runtime_case.runner.container
    storage = runtime_case.runner.saver._storage.storage
    root = storage.root(session_id, thread_id=thread_id)
    canonical = root / "rollout.jsonl"
    backup = root / "original-jsonl-for-test"
    marker = root / ("manifest.json" if damage == "manifest" else "segment-001.jsonl")
    before = canonical.read_bytes()
    failure = PermissionError(errno.EACCES, f"原生检查故障 {canonical}", str(canonical))
    original = Path.is_symlink

    def inspect(path):
        if path == canonical:
            raise failure
        return original(path)

    def database_state():
        with storage._connect(thread_id, "", session_id=session_id, read_only=True) as connection:
            return tuple(connection.iterdump())

    with workspace_context(container.workspace_id, runtime_case.runner.workspace, session_catalog=container.session_catalog):
        baseline = await _history_request(history_client, session_id, thread_id, "history")
        assert baseline.status_code == 200, baseline.text
        original_database = database_state()
        original_items = storage.read_items(session_id, thread_id)
        if damage in {"manifest", "segment"}:
            marker.write_text("旧布局哨兵")
        elif damage in {"symlink", "directory"}:
            canonical.rename(backup)
            if damage == "symlink":
                canonical.symlink_to(backup)
            else:
                canonical.mkdir()
        try:
            with monkeypatch.context() as patch:
                if damage == "inspection":
                    patch.setattr(Path, "is_symlink", inspect)
                expected = PermissionError if damage == "inspection" else RuntimeError
                with pytest.raises(expected) as caught:
                    storage.initialize(session_id, thread_id=thread_id)
            reason = "已移除的旧布局" if damage in {"manifest", "segment"} else ("初始化失败" if damage == "inspection" else "不是安全普通文件")
            assert reason in str(caught.value)
            assert "artifact=rollout_" in str(caught.value)
            assert str(root) not in str(caught.value) and str(root) in caplog.text
            if damage == "inspection":
                assert caught.value.__cause__ is failure and caught.value.errno == errno.EACCES
                assert caught.value.filename is None
            assert (backup if backup.exists() else canonical).read_bytes() == before
            assert database_state() == original_database
        finally:
            if damage in {"manifest", "segment"}:
                marker.unlink()
            elif damage in {"symlink", "directory"}:
                canonical.unlink() if damage == "symlink" else canonical.rmdir()
                backup.rename(canonical)
        storage.initialize(session_id, thread_id=thread_id)
        with RolloutCheckpointSaver(storage.sessions_dir) as cold:
            assert cold._storage.storage.read_items(session_id, thread_id) == original_items
        recovered = await _history_request(history_client, session_id, thread_id, "history")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"]["items"] == baseline.json()["data"]["items"]
        assert recovered.json()["data"]["thread_id"] == thread_id
        assert recovered.json()["data"]["items"][-1]["turn_id"] == turn_id
        assert canonical.read_bytes() == before and database_state() == original_database
