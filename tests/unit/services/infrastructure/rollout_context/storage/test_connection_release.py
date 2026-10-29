"""JSONL 尾部回滚失败仍必须关闭同一 SQLite 事务连接。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.storage import primitives
from app.services.infrastructure.rollout_context.storage.guards import (
    JsonlTransactionGuard,
    register_jsonl_guard,
)


@pytest.mark.parametrize("truncate_fails", [False, True])
def test_transaction_exit_closes_connection_after_tail_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, truncate_fails: bool,
) -> None:
    index = tmp_path / "index.sqlite"
    canonical = tmp_path / "rollout.jsonl"
    canonical.write_bytes(b"committed\n")
    connection = primitives._RolloutSQLiteConnection.open(index, read_only=False, frozen_source=False)
    connection.execute("CREATE TABLE marker(value TEXT)")
    original = RuntimeError("原事务失败")
    failure = OSError("JSONL 尾部回滚失败")
    truncate = primitives.truncate_uncommitted_tail
    guards: list[JsonlTransactionGuard] = []

    def cleanup(guard: JsonlTransactionGuard) -> None:
        guards.append(guard)
        if truncate_fails:
            raise failure
        truncate(guard)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(primitives, "truncate_uncommitted_tail", cleanup)
            with pytest.raises((OSError, RuntimeError)) as caught, connection:
                connection.execute("INSERT INTO marker VALUES ('未提交')")
                register_jsonl_guard(connection, canonical, original_size=canonical.stat().st_size)
                with canonical.open("ab") as stream:
                    stream.write(b"uncommitted\n")
                raise original
        assert caught.value is (failure if truncate_fails else original)
        if truncate_fails:
            assert caught.value.__context__ is original
        assert len(guards) == 1
        assert canonical.read_bytes() == (b"committed\nuncommitted\n" if truncate_fails else b"committed\n")
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
        with sqlite3.connect(index) as cold:
            assert cold.execute("SELECT * FROM marker").fetchall() == []
    finally:
        connection.close()
