from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.exceptions import NotFoundError
from app.schemas.public_v2.team import TeamBoardDTO, TeamEventDTO


TEAM_ID_PATTERN = re.compile(r"^team_[0-9a-f]{32}$")


class TeamStore:
    """持久化工作区级团队面板；团队跨多个 Session，因此不属于单一会话目录。"""

    def __init__(self, *, workspace_root: Path) -> None:
        self._teams_root = workspace_root.resolve() / ".boxteam" / "teams"

    def create(self, board: TeamBoardDTO) -> TeamBoardDTO:
        team_dir = self._team_dir(board.team_id)
        if team_dir.exists():
            raise FileExistsError(f"团队已存在: {board.team_id}")
        team_dir.mkdir(parents=True)
        self.save(board)
        return board

    def get(self, team_id: str) -> TeamBoardDTO:
        board_path = self._board_path(team_id)
        if not board_path.is_file():
            raise NotFoundError(f"团队不存在: {team_id}")
        with board_path.open("r", encoding="utf-8") as file:
            return TeamBoardDTO.model_validate(json.load(file))

    def list(self) -> list[TeamBoardDTO]:
        if not self._teams_root.exists():
            return []
        boards: list[TeamBoardDTO] = []
        for team_dir in sorted(self._teams_root.iterdir()):
            if not team_dir.is_dir():
                raise RuntimeError(f"团队存储目录出现非法文件: {team_dir}")
            boards.append(self.get(team_dir.name))
        return boards

    def save(self, board: TeamBoardDTO) -> TeamBoardDTO:
        team_dir = self._team_dir(board.team_id)
        if not team_dir.is_dir():
            raise NotFoundError(f"团队目录不存在: {board.team_id}")
        target = team_dir / "team.json"
        temporary = team_dir / "team.json.tmp"
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(
                board.model_dump(mode="json"),
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.flush()
        temporary.replace(target)
        return board

    def append_event(self, event: TeamEventDTO) -> None:
        event_path = self._team_dir(event.team_id) / "events.jsonl"
        if not event_path.parent.is_dir():
            raise NotFoundError(f"团队目录不存在: {event.team_id}")
        with event_path.open("a", encoding="utf-8") as file:
            file.write(event.model_dump_json())
            file.write("\n")
            file.flush()

    def recent_events(self, team_id: str, *, limit: int = 20) -> list[TeamEventDTO]:
        event_path = self._team_dir(team_id) / "events.jsonl"
        if not event_path.exists():
            return []
        with event_path.open("r", encoding="utf-8") as file:
            lines = file.readlines()
        return [TeamEventDTO.model_validate_json(line) for line in lines[-limit:]]

    def _board_path(self, team_id: str) -> Path:
        return self._team_dir(team_id) / "team.json"

    def _team_dir(self, team_id: str) -> Path:
        if not TEAM_ID_PATTERN.fullmatch(team_id):
            raise ValueError(f"非法 team_id: {team_id}")
        return self._teams_root / team_id
