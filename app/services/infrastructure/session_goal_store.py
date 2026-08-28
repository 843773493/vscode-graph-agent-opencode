from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.session_paths import SessionPathResolver
from app.schemas.internal_v2.goal import SessionGoalDTO


class SessionGoalStore:
    FILE_NAME = "goal.json"

    def __init__(self, path_resolver: SessionPathResolver) -> None:
        self._path_resolver = path_resolver

    def _path(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node(session_id) / self.FILE_NAME

    def read(self, session_id: str) -> SessionGoalDTO | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return SessionGoalDTO.model_validate(raw)

    def write(self, goal: SessionGoalDTO) -> None:
        path = self._path(goal.session_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(goal.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def clear(self, session_id: str) -> bool:
        path = self._path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_existing(self) -> list[SessionGoalDTO]:
        goals: list[SessionGoalDTO] = []
        for node in self._path_resolver.refresh():
            if node.kind != "session":
                continue
            path = node.path / self.FILE_NAME
            if path.is_file():
                goals.append(
                    SessionGoalDTO.model_validate_json(path.read_text(encoding="utf-8"))
                )
        return goals
