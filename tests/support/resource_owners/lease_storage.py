"""账本测试读取持久事实、观察事务边界；不绑定内部 SQL 执行次数。"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.services.infrastructure.external_resource_leases.database import LeaseDatabase

LeaseSnapshot = dict[str, list[dict[str, object]]]


def read_lease_snapshot(path: Path) -> LeaseSnapshot:
    records, leases = LeaseDatabase(path).read()
    return {"records": [asdict(record) for record in records.values()],
            "leases": [asdict(lease) for lease in leases.values()]}


def observe_lease_commits(
    monkeypatch: pytest.MonkeyPatch, observe: Callable[[Path, LeaseSnapshot], None],
) -> None:
    commit = LeaseDatabase.commit

    def committing(self: LeaseDatabase, *, records, leases) -> None:
        observe(self.path, {"records": [asdict(record) for record in records],
                            "leases": [asdict(lease) for lease in leases]})
        commit(self, records=records, leases=leases)

    monkeypatch.setattr(LeaseDatabase, "commit", committing)


def mutate_lease_payload(path: Path, section: str, key: str, change: Callable[[dict[str, object]], None]) -> None:
    """仅用于模拟绕过软件修改库，故意允许破坏身份约束。"""
    table, column = {"records": ("resource_records", "resource_id"), "leases": ("resource_leases", "lease_id")}[section]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        payload = json.loads(connection.execute(f"SELECT payload FROM {table} WHERE {column}=?", (key,)).fetchone()[0])
        change(payload)
        connection.execute(f"UPDATE {table} SET payload=? WHERE {column}=?", (json.dumps(payload), key))
        connection.commit()
