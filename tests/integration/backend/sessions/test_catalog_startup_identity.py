"""真实 Session 创建后，启动必须先拒绝 catalog 中漂移的冻结存储身份。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store import CatalogMaintenanceRequiredError
from app.core.session_catalog_store.queries import _register_session_list_functions
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
)
from tests.support.workspaces import (
    prepare_test_workspace,
    render_test_workspace_config,
)


@pytest.fixture
def startup_container(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> AppContainer:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    # 身份/恢复测试不调用模型，不要求本机提供个人Provider凭据。
    config = json.loads(render_test_workspace_config(
        (Path.cwd() / "configs/tests/workspace/default.jsonc").read_text(encoding="utf-8"),
    ))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "startup-contract-unused-key"
        provider.pop("auth", None)
    (context.workspace_root / ".boxteam").mkdir()
    (context.workspace_root / ".boxteam/workspace.jsonc").write_text(
        json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(context.workspace_root))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    return _build_owned_container(context.workspace_root, monkeypatch, request)


def _catalog_dump(path: Path) -> str:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


@pytest.mark.parametrize("owner", ["node", "preparing"])
@pytest.mark.parametrize("corruption", ["date", "naive_time", "locator_leaf"])
async def test_container_startup_rejects_storage_identity_drift_without_repair(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, owner: str, corruption: str,
) -> None:
    container = startup_container
    service = container.session_service
    # 通过产品创建入口生成 catalog、manifest、main 和完整控制库。
    source = await service.create(SessionCreateRequest(title="启动身份核验"))
    if owner == "preparing":
        _, record = await service.prepare_context_fork(
            idempotency_key="startup-identity-prepared", title="未发布副本",
            agent_id=source.current_agent_id, parent_session_id=None,
            context_source_session_id=source.session_id,
        )
        session_id, created_at = record.session_id, datetime.fromisoformat(record.created_at)
        directory = service.path_resolver.resolve_prepared_session_node(record)
        table, identity_column = "session_creation_records", "session_id"
    else:
        session_id, created_at = source.session_id, source.created_at
        directory = service.path_resolver.resolve_session_node(session_id)
        table, identity_column = "nodes", "node_id"
    store = service.path_resolver.catalog_store
    path = store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    # 人工损坏是测试输入，启动恢复不得替测试修复它或移动目录。
    with closing(sqlite3.connect(path)) as connection:
        _register_session_list_functions(connection)
        if corruption == "locator_leaf":
            other_id = create_prefixed_id("ses")
            locator = f"sessions/{created_at:%Y/%m/%d}/{other_id}"
            if owner == "node":
                target = directory.with_name(other_id)
                directory.rename(target)
                directory = target
            column, value = "storage_relative_locator", locator
        else:
            value = (created_at + timedelta(days=1)).isoformat() if corruption == "date" else created_at.replace(tzinfo=None).isoformat()
            column = "created_at"
            if owner == "node":
                manifest = directory / "session.json"
                payload = json.loads(manifest.read_text())
                payload["created_at"] = value
                manifest.write_text(json.dumps(payload))
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE {identity_column}=?", (value, session_id),
        )
        connection.commit()
    before = _catalog_dump(path)
    artifacts = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    with pytest.raises(CatalogMaintenanceRequiredError, match="存储身份") as error:
        _build_owned_container(container.workspace_root, monkeypatch, request)
    assert session_id in str(error.value)
    assert _catalog_dump(path) == before
    assert directory.is_dir()
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == artifacts


@pytest.mark.parametrize("renamed", [False, True])
async def test_container_startup_accepts_original_preparing_storage_identity(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, renamed: bool,
) -> None:
    container = startup_container
    source = await container.session_service.create(SessionCreateRequest(title="保留原创建意图"))
    _, record = await container.session_service.prepare_context_fork(
        idempotency_key="healthy-prepared", title="仍待发布的副本",
        agent_id=source.current_agent_id, parent_session_id=None,
        context_source_session_id=source.session_id,
    )
    if renamed:
        creation = container.session_catalog.creation_service
        creation._rename_staging_to_target(record, creation.read_prepared_session_metadata(record))
    path = container.session_service.path_resolver.catalog_store.database_path
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    before = _catalog_dump(path)
    restarted = _build_owned_container(container.workspace_root, monkeypatch, request)
    assert restarted.session_service.path_resolver.catalog_store.get_creation_record("healthy-prepared") == record
    assert _catalog_dump(path) == before
    assert (await restarted.session_service.get(source.session_id)).session_id == source.session_id
