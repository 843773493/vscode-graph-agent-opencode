from __future__ import annotations

import json
from pathlib import Path

from app.core.atomic_fs import atomic_write_bytes
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCreationRecord
from app.schemas.internal_v2.goal import SessionGoalDTO


class SessionGoalStore:
    FILE_NAME = "goal.json"
    # Goal 恒属于 main thread；thread 身份的唯一权威是 workspace catalog，
    # goal.json 只保存业务字段，不落第二 main 指针。
    _DERIVED_KEYS = ("thread_id",)

    def __init__(self, path_resolver: SessionCatalogPathResolver) -> None:
        self._path_resolver = path_resolver

    def _goal_file_path(
        self,
        session_id: str,
        creation: SessionCreationRecord | None = None,
    ) -> Path:
        if creation is not None:
            if creation.session_id != session_id or creation.purpose != "context_fork" or creation.state != "preparing":
                raise RuntimeError("fork Goal 缺少精确 preparing main creation capability")
            return self._path_resolver.resolve_prepared_session_node(creation) / self.FILE_NAME
        return self._path_resolver.resolve_session_node(session_id) / self.FILE_NAME

    def read(self, session_id: str, *, creation: SessionCreationRecord | None = None) -> SessionGoalDTO | None:
        goal_file_path = self._goal_file_path(session_id, creation)
        if not goal_file_path.exists():
            return None
        raw = json.loads(goal_file_path.read_text(encoding="utf-8"))
        thread_id = creation.main_thread_id if creation is not None else self._path_resolver.main_thread_id(session_id)
        return SessionGoalDTO.model_validate({**raw, "thread_id": thread_id})

    def write(self, goal: SessionGoalDTO, *, creation: SessionCreationRecord | None = None) -> None:
        goal_file_path = self._goal_file_path(goal.session_id, creation)
        thread_id = creation.main_thread_id if creation is not None else self._path_resolver.main_thread_id(goal.session_id)
        if goal.thread_id != thread_id:
            raise ValueError("Goal 只能写入权威 main Thread")
        atomic_write_bytes(
            goal_file_path,
            json.dumps(
                goal.model_dump(mode="json", exclude=set(self._DERIVED_KEYS)),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )

    def clear(self, session_id: str) -> bool:
        goal_file_path = self._goal_file_path(session_id)
        if not goal_file_path.exists():
            return False
        goal_file_path.unlink()
        return True

    def list_existing(self) -> list[SessionGoalDTO]:
        goals: list[SessionGoalDTO] = []
        for node in self._path_resolver.list_nodes():
            if node.kind != "session":
                continue
            goal = self.read(node.node_id)
            if goal is not None:
                goals.append(goal)
        return goals
