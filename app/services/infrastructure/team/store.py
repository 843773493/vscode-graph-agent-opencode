"""任务板只在所属 Session control ledger 中提交，不再维护 JSON/event 双写。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from app.abstractions.session_subagent import OwnerThreadCreationFactoryProtocol
from app.core.exceptions import NotFoundError
from app.core.execution_admission_context import current_execution_admission
from app.core.phase_observer import observe_phase_sync
from app.core.session_control_store import SessionControlStore
from app.schemas.internal_v2.team import TeamBoardDTO, TeamEventDTO

TEAM_ID_PATTERN = re.compile(r"^team_[0-9a-f]{32}$")


def _observe_read(session_id: str) -> None:
    admission = current_execution_admission()
    observe_phase_sync(admission.execution_id if admission is not None else session_id, "team.ledger.read")


class TeamStore:
    def __init__(self, *, factory: OwnerThreadCreationFactoryProtocol,
                 write_admission: Callable[[str], AbstractAsyncContextManager[None]]) -> None:
        self._factory = factory
        self._write_admission = write_admission

    def control(self, session_id: str) -> SessionControlStore:
        control = self._factory.for_owner_session(session_id).control_store
        if (control.database_path.parent / "teams").exists():
            raise RuntimeError("旧团队 JSON 存储不受支持，禁止作为第二权威读取或迁移")
        return control

    async def commit(self, board: TeamBoardDTO, event: TeamEventDTO,
                     *, wakeup: dict[str, object] | None = None) -> int:
        self._validate_team_id(board.team_id)
        async with self._write_admission(board.session_id):
            control = self.control(board.session_id)
            return control.commit_collaboration_board(
                session_id=board.session_id, team_id=board.team_id, version=board.version,
                board_json=board.model_copy(update={"members": [], "recent_events": []}).model_dump_json(),
                event_json=event.model_dump_json(),
                wakeup_json=None if wakeup is None else json.dumps(wakeup, ensure_ascii=False),
            )

    def get(self, session_id: str, team_id: str) -> TeamBoardDTO:
        self._validate_team_id(team_id)
        _observe_read(session_id)
        rows = self.control(session_id).collaboration_boards(team_id)
        if not rows:
            raise NotFoundError(f"团队不存在: {team_id}")
        board = TeamBoardDTO.model_validate(json.loads(rows[0]))
        if (board.session_id, board.team_id) != (session_id, team_id):
            raise RuntimeError("团队权威 board owner/ID 与 ledger 不一致")
        return board

    def list(self, session_id: str) -> list[TeamBoardDTO]:
        _observe_read(session_id)
        return [self.get(session_id, TeamBoardDTO.model_validate_json(row).team_id)
                for row in self.control(session_id).collaboration_boards()]

    def recent_events(self, session_id: str, team_id: str, *, limit: int = 20) -> list[TeamEventDTO]:
        self._validate_team_id(team_id)
        _observe_read(session_id)
        events = [TeamEventDTO.model_validate_json(row) for row in self.control(session_id).collaboration_recent_events(team_id, limit)]
        if any((event.session_id, event.team_id) != (session_id, team_id) for event in events):
            raise RuntimeError("团队事件与权威 owner/ID 不一致")
        return events

    def latest_task_event(self, session_id: str, team_id: str, task_id: str) -> TeamEventDTO | None:
        self._validate_team_id(team_id)
        _observe_read(session_id)
        row = self.control(session_id).collaboration_latest_task_event(team_id, task_id)
        if row is None:
            return None
        event = TeamEventDTO.model_validate_json(row)
        if (event.session_id, event.team_id, event.payload.get("task_id")) != (session_id, team_id, task_id):
            raise RuntimeError("团队任务事件与权威 owner/ID 不一致")
        return event

    @staticmethod
    def _validate_team_id(team_id: str) -> None:
        if not TEAM_ID_PATTERN.fullmatch(team_id):
            raise ValueError(f"非法 team_id: {team_id}")
