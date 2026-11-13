"""只读核验真实终端SQLite状态，包含尚在WAL中的已提交记录。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def read_terminal_state(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        workspace_id, updated_at = connection.execute(
            "SELECT workspace_id, updated_at FROM terminal_state WHERE singleton=1",
        ).fetchone()
        return {
            "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "workspace_id": workspace_id,
            "updated_at": updated_at,
            "terminals": [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM terminal_records ORDER BY position",
            )],
        }
