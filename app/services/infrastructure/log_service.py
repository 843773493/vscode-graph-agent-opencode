from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from app.core.atomic_fs import native_io_path
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.runtime import UiSnapshotResultDTO


def _write_snapshot_files(
    directory: Path,
    snapshot_id: str,
    html: str,
    raw_metadata: str,
) -> None:
    directory = native_io_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{snapshot_id}.html").write_text(html, encoding="utf-8")
    # metadata 是快照发布标记；读取端必须先确认它存在。
    (directory / f"{snapshot_id}.json").write_text(raw_metadata, encoding="utf-8")


def _read_snapshot_payload(
    directory: Path,
    snapshot_id: str,
    workspace_id: str,
    session_id: str | None,
    metadata: bool,
) -> str:
    directory = native_io_path(directory)
    meta_path = directory / f"{snapshot_id}.json"
    try:
        raw_metadata = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise KeyError(f"快照不存在: {snapshot_id}") from error
    try:
        value = json.loads(raw_metadata)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"快照 metadata 损坏: {snapshot_id}") from error
    if not isinstance(value, dict) or (
        value.get("snapshot_id") != snapshot_id
        or value.get("workspace_id") != workspace_id
        or value.get("session_id") != session_id
    ):
        raise RuntimeError(f"快照 metadata 与请求身份不一致: {snapshot_id}")
    if metadata:
        return raw_metadata
    return (directory / f"{snapshot_id}.html").read_text(encoding="utf-8")


@dataclass(slots=True)
class LogSnapshotRecord:
    session_id: str | None
    html: str
    page_title: str | None = None
    status: str | None = None
    source: str = "webview"


class LogService:
    def __init__(
        self, *, workspace_root: Path, path_resolver: SessionCatalogPathResolver
    ) -> None:
        self._workspace_root = workspace_root
        self._path_resolver = path_resolver
        self._lifecycle_gate = SessionLifecycleGate(path_resolver.sessions_root)

    @asynccontextmanager
    async def _snapshot_directory(self, session_id: str | None) -> AsyncIterator[Path]:
        if session_id is None:
            yield self._workspace_root / ".boxteam" / "logs" / "webview"
        else:
            async with self._lifecycle_gate.shared(session_id):
                node = self._path_resolver.resolve_session_node_for_runtime(session_id)
                yield node / "logs" / "webview"

    async def write_html_snapshot(
        self, record: LogSnapshotRecord
    ) -> UiSnapshotResultDTO:
        snapshot_id = create_prefixed_id("snapshot")
        metadata = {
            "snapshot_id": snapshot_id,
            "workspace_id": self._path_resolver.workspace_id,
            "session_id": record.session_id,
            "page_title": record.page_title,
            "status": record.status,
            "source": record.source,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        async with self._snapshot_directory(record.session_id) as directory:
            await run_sync_shielded(
                _write_snapshot_files,
                directory,
                snapshot_id,
                record.html,
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )
        base = (
            f"/api/v1/workspaces/{self._path_resolver.workspace_id}"
            f"/runtime/log-snapshot/{snapshot_id}"
        )
        query = (
            f"?{urlencode({'session_id': record.session_id})}"
            if record.session_id
            else ""
        )
        return UiSnapshotResultDTO(
            snapshot_id=snapshot_id,
            html_url=f"{base}/html{query}",
            metadata_url=f"{base}/metadata{query}",
        )

    async def read_html_snapshot(
        self,
        snapshot_id: str,
        *,
        session_id: str | None,
        metadata: bool,
    ) -> str:
        if re.fullmatch(r"snapshot_[0-9a-f]{32}", snapshot_id) is None:
            raise ValueError(f"非法 snapshot_id: {snapshot_id!r}")
        async with self._snapshot_directory(session_id) as directory:
            return await run_sync_shielded(
                _read_snapshot_payload,
                directory,
                snapshot_id,
                self._path_resolver.workspace_id,
                session_id,
                metadata,
            )
