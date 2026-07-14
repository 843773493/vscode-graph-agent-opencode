from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.core.background_task_registry import BackgroundTaskHandle


class BackgroundTaskHistoryStore:
    """在工作区 .boxteam 中保存后台任务生命周期留痕。"""

    def __init__(self, *, boxteam_root: Path) -> None:
        self._root = boxteam_root / "background_tasks"

    def upsert(self, handle: BackgroundTaskHandle) -> None:
        records = {
            record.task_id: record
            for record in self.list_session(handle.session_id)
        }
        records[handle.task_id] = handle
        self._write_session(handle.session_id, list(records.values()))

    def list_session(self, session_id: str) -> list[BackgroundTaskHandle]:
        path = self._session_file(session_id)
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise RuntimeError(f"后台任务历史必须是数组: {path}")
        records: list[BackgroundTaskHandle] = []
        for index, record in enumerate(value):
            if not isinstance(record, dict):
                raise RuntimeError(f"后台任务历史第 {index} 项必须是对象: {path}")
            records.append(BackgroundTaskHandle.from_dict(record))
        return records

    def mark_active_tasks_lost(self) -> None:
        if not self._root.exists():
            return
        for path in self._root.glob("*.json"):
            session_id = path.stem
            records = self.list_session(session_id)
            changed = False
            for record in records:
                if record.status not in {"pending", "running"}:
                    continue
                record.status = "lost"
                record.ended_at = datetime.now()
                record.metadata["status_note"] = "后端进程结束，后台任务已失去运行实体。"
                changed = True
            if changed:
                self._write_session(session_id, records)

    def delete_session(self, session_id: str) -> None:
        path = self._session_file(session_id)
        if path.exists():
            path.unlink()

    def _session_file(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or "\\" in session_id:
            raise ValueError(f"非法 session_id: {session_id!r}")
        return self._root / f"{session_id}.json"

    def _write_session(
        self,
        session_id: str,
        records: list[BackgroundTaskHandle],
    ) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._session_file(session_id)
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(
                [record.to_dict() for record in records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(path)
