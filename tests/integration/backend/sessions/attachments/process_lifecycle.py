"""真实进程恢复用例只读附件 owner 的状态，不替产品结算。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
)


def directory_snapshot(case: CommunicationCase) -> dict[str, object]:
    database = case.workspace / ".boxteam/attachments/catalog.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row, = connection.execute(
            "SELECT d.attachment_id,d.relative_locator,d.created_at,d.release_pending,d.release_reason,d.availability,"
            "a.binding_token,a.state AS association_state "
            "FROM attachment_directories d JOIN attachment_directory_associations a USING(attachment_id)",
        ).fetchall()
        return dict(row)
