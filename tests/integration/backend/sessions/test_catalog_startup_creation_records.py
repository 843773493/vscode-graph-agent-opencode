"""真实创建与中止生成原始 envelope，启动核其冻结正文而非当前业务配置。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.session_catalog_store import (
    CatalogMaintenanceRequiredError,
    SessionCreationRecord,
)
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_directories import (
    _catalog_dump,
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)


class CreationPaused(BaseException):
    """完整 staging 已由真实创建入口写入，rename 前模拟退出而不触发补偿。"""


async def _create_record(
    container: AppContainer, monkeypatch: pytest.MonkeyPatch, state: str,
) -> SessionCreationRecord:
    creation = container.session_catalog.creation_service
    records = []

    def before_rename(record, metadata):
        records.append(record)
        raise CreationPaused

    if state == "published":
        session = await container.session_service.create(SessionCreateRequest(title="原始创建意图"))
        store = container.session_service.path_resolver.catalog_store
        with store.read_transaction() as connection:
            key = connection.execute(
                "SELECT session_creation_idempotency_key FROM session_creation_records WHERE session_id=?",
                (session.session_id,),
            ).fetchone()[0]
        return store.get_creation_record(key)
    with monkeypatch.context() as interruption:
        interruption.setattr(creation, "_rename_staging_to_target", before_rename)
        with pytest.raises(CreationPaused):
            await container.session_service.create(SessionCreateRequest(title="尚未发布的创建意图"))
    record, = records
    if state == "aborted":
        record = await creation.abort_prepared(record, "保留原创建意图供人工核对")
    assert record.state == state
    return record


def _close_and_files(container: AppContainer) -> tuple[Path, dict[str, bytes]]:
    path = container.session_service.path_resolver.catalog_store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    root = container.workspace_root / ".boxteam"
    # catalog/WAL只比较逻辑dump；Session、staging与隔离的原正文逐字节比较。
    return path, {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
                  for p in (root / directory).rglob("*") if p.is_file()}


@pytest.mark.parametrize("state,corruption", [
    ("published", "title"),
    ("preparing", "metadata-json"),
    ("aborted", "metadata-keys"),
    ("published", "metadata-canonical"),
    ("preparing", "purpose"),
    ("published", "graph-shape"),
    ("aborted", "graph-revision"),
    ("published", "request-json"),
    ("aborted", "request-not-object"),
    ("published", "request-canonical"),
    ("preparing", "preimage"),
])
async def test_container_rejects_creation_envelope_corruption_without_repair(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str, corruption: str,
) -> None:
    container = startup_container
    record = await _create_record(container, monkeypatch, state)
    path, files = _close_and_files(container)
    metadata = json.loads(record.session_metadata_json)
    if corruption == "title":
        column, value = "display_name", "被外部改写的创建标题"
    elif corruption.startswith("metadata-"):
        column = "session_metadata_json"
        if corruption == "metadata-json":
            value = "{broken"
        elif corruption == "metadata-keys":
            metadata["unexpected"] = True
            value = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        else:
            value = json.dumps(metadata, sort_keys=True, indent=2)
    elif corruption == "purpose":
        column, value = "purpose", "context_fork"
    elif corruption == "graph-shape":
        column, value = "graph_schema_hash", "invalid-digest"
    elif corruption == "graph-revision":
        column, value = "graph_revision", record.graph_revision + 1
    elif corruption.startswith("request-"):
        column = "request_intent_json"
        value = {"request-json": "{broken", "request-not-object": "[]", "request-canonical": "{ }"}[corruption]
    else:
        column, value = "preimage_hash", "0" * 64
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"UPDATE session_creation_records SET {column}=? WHERE session_creation_idempotency_key=?",
            (value, record.session_creation_idempotency_key),
        )
        connection.commit()
    before = _catalog_dump(path)
    with pytest.raises(CatalogMaintenanceRequiredError, match="creation record envelope"):
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert _catalog_dump(path) == before
    root = container.workspace_root / ".boxteam"
    assert {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
            for p in (root / directory).rglob("*") if p.is_file()} == files


@pytest.mark.parametrize("state", ["preparing", "published", "aborted"])
async def test_container_accepts_original_creation_envelope_after_business_changes(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, state: str,
) -> None:
    container = startup_container
    record = await _create_record(container, monkeypatch, state)
    if state == "published":
        container.session_service.path_resolver.catalog_store.rename_node(record.session_id, "当前导航标题")
    path, files = _close_and_files(container)
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert restarted.session_service.path_resolver.catalog_store.get_creation_record(record.session_creation_idempotency_key) == record
    assert _catalog_dump(path) == before
    root = container.workspace_root / ".boxteam"
    assert {str(p.relative_to(root)): p.read_bytes() for directory in ("sessions", "orphaned")
            for p in (root / directory).rglob("*") if p.is_file()} == files
