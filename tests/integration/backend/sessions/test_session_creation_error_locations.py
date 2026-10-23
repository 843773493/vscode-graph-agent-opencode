"""真实创建失败的公开诊断与 durable record 不复制原生存储位置。"""

from __future__ import annotations

import errno
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app import main
from app.api.deps import get_session_service, verify_local_token
from app.api.sessions import router
from app.container import AppContainer
from app.core import session_creation
from app.core.session_catalog_store import SessionCatalogStore
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)


@pytest.mark.parametrize("locator", ["/private/system/session.json", r"C:\private\session.json", r"\\server\private\session.json"])
@pytest.mark.parametrize("stage", ["prepare", "verify", "rename", "quarantine"])
async def test_creation_failure_keeps_native_location_in_diagnostics_only(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, locator: str, stage: str,
) -> None:
    container = startup_container
    service = container.session_service
    creation = container.session_catalog.creation_service
    catalog = service.path_resolver.catalog_store
    original_error = PermissionError(errno.EACCES, f"原生调用拒绝 {locator}", locator)
    parent = await service.create(SessionCreateRequest(title="创建错误测试父会话"))
    write = session_creation._atomic_write_bytes
    read = Path.read_bytes
    rename = session_creation.os.rename
    publish_rename = creation._rename_staging_to_target

    def fail_write(path, payload):
        if path.name == "session.json":
            raise original_error
        return write(path, payload)

    def fail_read(path):
        if path.name == "session.json" and ".staging" in path.parts:
            raise original_error
        return read(path)

    def fail_rename(source, target):
        target = Path(target)
        if ((stage == "rename" and ".staging" in Path(source).parts and "orphaned" not in target.parts)
                or (stage == "quarantine" and "orphaned" in target.parts)):
            raise original_error
        return rename(source, target)

    def drift_parent(record, metadata):
        publish_rename(record, metadata)
        catalog.rename_node(parent.session_id, "父会话版本已变化")

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR, logger="app.core.session_creation"):
        if stage == "prepare":
            failure.setattr(session_creation, "_atomic_write_bytes", fail_write)
        elif stage == "verify":
            failure.setattr(Path, "read_bytes", fail_read)
        else:
            failure.setattr(session_creation.os, "rename", fail_rename)
        if stage == "quarantine":
            failure.setattr(creation, "_rename_staging_to_target", drift_parent)
        with pytest.raises(PermissionError if stage == "prepare" else RuntimeError) as caught:
            await service.create(SessionCreateRequest(title="失败的创建", folder_id=parent.session_id))

    records = [catalog.get_creation_record(row[0]) for row in catalog.connection.execute(
        "SELECT session_creation_idempotency_key FROM session_creation_records WHERE session_id != ?",
        (parent.session_id,),
    )]
    record, = records
    detail = str(caught.value)
    assert locator not in detail and str(container.workspace_root) not in detail
    assert record.session_id in detail and "errno=13" in detail
    assert record.state == ("aborted" if stage == "rename" else "preparing")
    assert record.abort_reason == (detail if stage == "rename" else None)
    assert caught.value.__cause__ is original_error
    assert any(log.exc_info and log.exc_info[1] is original_error for log in caplog.records)
    assert str(original_error) in caplog.text
    with pytest.raises(KeyError):
        catalog.get_node(record.session_id)
    staging = catalog.sessions_root / ".staging" / record.session_id
    target = catalog.sessions_root / record.storage_relative_locator.removeprefix("sessions/")
    quarantine = catalog.sessions_root.parent / "orphaned/session-creation" / record.session_id
    assert staging.exists() == (stage in {"prepare", "verify"})
    assert target.exists() == (stage == "quarantine")
    assert quarantine.exists() == (stage == "rename")
    retried = await service.create(SessionCreateRequest(title="修复后再次创建"))
    assert retried.session_id != record.session_id
    # 释放原 owner 后冷读同一 record，禁止用内存副本代替持久化断言。
    container.workspace_activity_service.store.close()
    container.session_catalog.close()
    with closing(SessionCatalogStore(catalog.database_path, catalog.sessions_root)) as reopened:
        assert reopened.get_creation_record(record.session_creation_idempotency_key) == record


@pytest.mark.parametrize("corruption", ["entries", "threads", "main", "manifest", "binding", "fence", "sqlite"])
async def test_prepared_integrity_errors_preserve_original_files_and_logical_identity(
    startup_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, corruption: str,
) -> None:
    service = startup_container.session_service
    creation = startup_container.session_catalog.creation_service
    catalog = service.path_resolver.catalog_store
    prepare = creation._prepare_staging
    observed = []
    before = {}

    def corrupt_prepared(record, metadata):
        prepare(record, metadata)
        directory = catalog.sessions_root / ".staging" / record.session_id
        main_thread = directory / "threads" / record.main_thread_id
        if corruption == "entries":
            (directory / r"C:\private\unexpected.json").write_text("外部条目")
        elif corruption == "threads":
            (directory / "threads" / "unexpected").mkdir()
        elif corruption == "main":
            (main_thread / "unexpected").write_text("外部条目")
        elif corruption == "manifest":
            (directory / "session.json").write_text(str(directory))
        elif corruption == "sqlite":
            (directory / "session-control.sqlite").write_bytes(b"not a SQLite database")
        else:
            with closing(sqlite3.connect(directory / "session-control.sqlite")) as connection:
                connection.execute("UPDATE thread_catalog SET graph_revision=?" if corruption == "binding"
                                   else "UPDATE lifecycle_fence SET generation=?", ("a" * 64 if corruption == "binding" else 2,))
                connection.commit()
        observed.append(record)
        before.update({str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()})

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR, logger="app.core.session_creation"):
        failure.setattr(creation, "_prepare_staging", corrupt_prepared)
        with pytest.raises(RuntimeError) as caught:
            await service.create(SessionCreateRequest(title="校验必须拒绝的真实准备目录"))
    record, = observed
    detail = str(caught.value)
    assert record.session_id in detail and str(startup_container.workspace_root) not in detail
    assert r"C:\private" not in detail
    assert catalog.get_creation_record(record.session_creation_idempotency_key).state == "preparing"
    with pytest.raises(KeyError):
        catalog.get_node(record.session_id)
    directory = catalog.sessions_root / ".staging" / record.session_id
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    if corruption == "sqlite":
        assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
        assert "sqlite_errorcode=26" in detail
    else:
        assert str(directory) in caplog.text


@pytest.fixture
async def creation_client(startup_container: AppContainer):
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.add_exception_handler(HTTPException, main.workspace_http_exception_handler)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_session_service] = lambda: startup_container.session_service
    application.dependency_overrides[verify_local_token] = lambda: "creation-error-test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("stage", ["path_budget", "rename"])
async def test_create_http_error_and_explicit_retry(
    creation_client: httpx.AsyncClient, startup_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    original = session_creation.os.rename

    def fail_budget(*args):
        raise ValueError("SQLite 路径超限: /private/workspace/session-control.sqlite")

    def fail_rename(source, target):
        if ".staging" in Path(source).parts and "orphaned" not in Path(target).parts:
            raise PermissionError(errno.EACCES, "目标拒绝", str(target))
        return original(source, target)

    with monkeypatch.context() as failure:
        if stage == "path_budget":
            failure.setattr(session_creation, "validate_sqlite_path_budget", fail_budget)
        else:
            failure.setattr(session_creation.os, "rename", fail_rename)
        response = await creation_client.post("/api/v1/sessions", json={"title": "HTTP创建失败"},
                                              headers={"X-Request-ID": "creation-error-request"})
    assert response.status_code == (400 if stage == "path_budget" else 500)
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "creation-error-request"
    assert str(startup_container.workspace_root) not in response.text and "/private/" not in response.text
    assert f"stage={stage}" in response.text
    catalog = startup_container.session_service.path_resolver.catalog_store
    records = catalog.connection.execute("SELECT state, abort_reason FROM session_creation_records").fetchall()
    if stage == "path_budget":
        assert not records
    else:
        assert len(records) == 1 and records[0][0] == "aborted"
        assert records[0][1] in response.json()["message"]
    retried = await creation_client.post("/api/v1/sessions", json={"title": "HTTP修复重试"},
                                        headers={"X-Request-ID": "creation-retry-request"})
    assert retried.status_code == 200, retried.text
    assert retried.headers["X-Request-ID"] == retried.json()["request_id"] == "creation-retry-request"
