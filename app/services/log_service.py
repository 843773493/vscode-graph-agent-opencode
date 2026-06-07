from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.path_utils import get_workspace_root
from app.schemas.public_v2.common import LogSnapshotResultDTO


@dataclass(slots=True)
class LogSnapshotRecord:
    workspace_root: str
    session_id: str | None
    html: str
    page_title: str | None = None
    status: str | None = None
    source: str = "webview"
    category: str = "webview"


class LogService:
    def __init__(self) -> None:
        self._base_dir = get_workspace_root() / ".boxteam" / "logs"

    def _ensure_dir(self, *parts: str) -> Path:
        path = self._base_dir.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _build_file_stem(self, session_id: str | None) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_session_id = session_id.strip() if session_id else "no-session"
        safe_session_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in safe_session_id)
        return f"{timestamp}_{safe_session_id}"

    def write_html_snapshot(self, record: LogSnapshotRecord) -> LogSnapshotResultDTO:
        workspace_root = Path(record.workspace_root).expanduser().resolve()
        base_dir = workspace_root / ".boxteam" / "logs" / record.category
        base_dir.mkdir(parents=True, exist_ok=True)
        file_stem = self._build_file_stem(record.session_id)

        html_path = base_dir / f"{file_stem}.html"
        meta_path = base_dir / f"{file_stem}.json"

        meta = {
            "workspace_root": record.workspace_root,
            "session_id": record.session_id,
            "page_title": record.page_title,
            "status": record.status,
            "source": record.source,
            "category": record.category,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "html_file": html_path.name,
        }

        html_path.write_text(record.html, encoding="utf-8")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return LogSnapshotResultDTO(html_path=str(html_path), meta_path=str(meta_path))