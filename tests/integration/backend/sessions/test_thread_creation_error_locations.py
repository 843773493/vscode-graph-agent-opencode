"""真实 child 创建物理失败必须保留诊断与原创建身份，不泄漏存储位置。"""

from __future__ import annotations

import errno
import logging
import os
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
)
from app.container import AppContainer
from app.core import thread_creation
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.thread_creation import ThreadCreationService
from app.schemas.internal_v2.session import SessionCreateRequest
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    startup_container as startup_container,  # noqa: PLC0414
)


@pytest.fixture
async def child_creation(startup_container: AppContainer):
    session = await startup_container.session_service.create(SessionCreateRequest(title="child物理错误属主"))
    service = startup_container.thread_initial_execution_service._factory.for_owner_session(session.session_id)
    metadata = {
        "graph_binding": asdict(DEEP_AGENT_GRAPH_BINDING),
        "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
        "task_seed": None, "task_reference": None,
    }

    async def create(key: str = "child-physical-error"):
        return await service.create(
            idempotency_key=key, session_id=session.session_id, thread_id=None,
            delegation_id=None, initial_state="idle", session_metadata=metadata,
            artifact_manifest={"notes/plan.md": "原始计划"},
        )

    return session.session_id, service, create


@pytest.mark.parametrize("locator", ["/private/thread.json", r"C:\private\thread.json", r"\\server\private\thread.json"])
@pytest.mark.parametrize("stage", ["prepare", "verify", "walk", "rename", "cleanup"])
async def test_child_physical_failure_is_safe_and_resumes_original_record(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, locator: str, stage: str,
) -> None:
    session_id, service, create = child_creation
    original_error = PermissionError(errno.EACCES, f"原生操作拒绝 {locator}", locator)
    write, read, rename, rmtree = thread_creation._atomic_write_bytes, Path.read_bytes, thread_creation.os.rename, thread_creation.shutil.rmtree
    walk = thread_creation.os.walk
    directory = startup_container.session_service.path_resolver.resolve_session_node(session_id)

    def fail_write(path, payload):
        if path.name == "thread.json":
            raise original_error
        return write(path, payload)

    def fail_read(path):
        if path.name == "thread.json" and ".staging" in path.parts:
            raise original_error
        return read(path)

    def fail_walk(path, *args, **kwargs):
        if ".staging" in Path(path).parts:
            if onerror := kwargs.get("onerror"):
                onerror(original_error)
            return iter(())
        return walk(path, *args, **kwargs)

    def fail_rename(source, target):
        if ".staging" in Path(source).parts and "threads" in Path(target).parts:
            raise original_error
        return rename(source, target)

    def fail_cleanup(path, *args, **kwargs):
        if "threads" in Path(path).relative_to(directory).parts:
            raise original_error
        return rmtree(path, *args, **kwargs)

    def refuse_publish(*args, **kwargs):
        raise RuntimeError("确定性发布冲突")

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR, logger="app.core.thread_creation"):
        if stage == "prepare":
            failure.setattr(thread_creation, "_atomic_write_bytes", fail_write)
        elif stage == "verify":
            failure.setattr(Path, "read_bytes", fail_read)
        elif stage == "walk":
            failure.setattr(thread_creation.os, "walk", fail_walk)
        elif stage == "rename":
            failure.setattr(thread_creation.os, "rename", fail_rename)
        else:
            failure.setattr(service.control_store, "publish_thread_creation_record", refuse_publish)
            failure.setattr(thread_creation.shutil, "rmtree", fail_cleanup)
        with pytest.raises(PermissionError if stage != "rename" else RuntimeError) as caught:
            await create()
    record = service.control_store.get_thread_creation_record("child-physical-error")
    detail = str(caught.value)
    for value in (locator, str(directory), str(startup_container.workspace_root)):
        assert value not in detail
    assert session_id in detail and record.child_thread_id in detail and "errno=13" in detail
    assert caught.value.__cause__ is original_error
    assert record.state == "preparing" and record.abort_reason is None
    assert any(log.exc_info and log.exc_info[1] is original_error for log in caplog.records)
    assert str(original_error) in caplog.text
    assert not service.control_store.list_child_thread_rows()
    assert not service.control_store.list_pending_initial_execution_intents()
    assert (directory / record.staging_locator).is_dir() == (stage != "cleanup")
    assert (directory / record.final_relative_locator).is_dir() == (stage == "cleanup")
    with closing(SessionControlStore(service.control_store.database_path)) as cold:
        assert cold.get_thread_creation_record("child-physical-error") == record
    resumed = await create()
    assert resumed.child_thread_id == record.child_thread_id and resumed.record_state == "published"
    assert len(service.control_store.list_child_thread_rows()) == 1
    assert (directory / resumed.final_relative_locator / "artifacts/notes/plan.md").read_text() == "原始计划"
    assert await create() == resumed


@pytest.mark.parametrize("corruption", ["directory", "file", "content", "manifest", "symlink"])
async def test_child_prepared_conflict_preserves_original_bytes_and_owner(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, corruption: str,
) -> None:
    session_id, service, create = child_creation
    observed = []

    def corrupt(self, staging, *args, **kwargs):
        if corruption == "directory":
            (staging / r"C:\private\unexpected").mkdir()
        elif corruption == "file":
            (staging / r"C:\private\unexpected").write_text("外来条目")
        elif corruption == "content":
            (staging / "thread.json").write_text(str(staging))
        elif corruption == "manifest":
            (staging / "artifact-manifest.json").write_text(str(staging))
        else:
            target = staging / "artifacts/notes/plan.md"
            target.unlink()
            target.symlink_to(staging / "thread.json")
        observed.append(staging)

    with monkeypatch.context() as failure:
        # 在已准备的产品目录上制造损坏，然后从同一创建入口重入校验。
        class Interrupted(BaseException):
            pass

        def interrupt(*args, **kwargs):
            corrupt(*args, **kwargs)
            raise Interrupted

        failure.setattr(ThreadCreationService, "_rename_staging_to_final", interrupt)
        with pytest.raises(Interrupted):
            await create()
    staging, = observed
    before = {str(p.relative_to(staging)): (p.is_symlink(), p.readlink().as_posix() if p.is_symlink() else p.read_bytes())
              for p in staging.rglob("*") if p.is_file() or p.is_symlink()}
    with caplog.at_level(logging.ERROR, logger="app.core.thread_creation"), pytest.raises(RuntimeError) as caught:
        await create()
    record = service.control_store.get_thread_creation_record("child-physical-error")
    assert session_id in str(caught.value) and record.child_thread_id in str(caught.value)
    assert str(startup_container.workspace_root) not in str(caught.value) and r"C:\private" not in str(caught.value)
    assert record.state == "preparing" and record.abort_reason is None
    assert {str(p.relative_to(staging)): (p.is_symlink(), p.readlink().as_posix() if p.is_symlink() else p.read_bytes())
            for p in staging.rglob("*") if p.is_file() or p.is_symlink()} == before
    assert str(staging) in caplog.text
    assert not service.control_store.list_child_thread_rows()


async def test_child_path_budget_fails_before_creation_record(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id, service, create = child_creation
    original_error = ValueError("路径组件超出预算: /private/thread.json")

    def fail(*args):
        raise original_error

    with monkeypatch.context() as failure:
        failure.setattr(thread_creation, "validate_path_budget", fail)
        with pytest.raises(ValueError) as caught:
            await create()
    assert "stage=path_budget" in str(caught.value) and session_id in str(caught.value)
    assert "/private/" not in str(caught.value) and str(startup_container.workspace_root) not in str(caught.value)
    assert caught.value.__cause__ is original_error
    assert not service.control_store.list_pending_thread_creations()
    assert not service.control_store.list_child_thread_rows()
    assert (await create()).record_state == "published"


@pytest.mark.parametrize("stage", ["rebuild", "redundant", "durability"])
async def test_child_physical_failure_preserves_recovery_window(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    session_id, service, create = child_creation
    directory = startup_container.session_service.path_resolver.resolve_session_node(session_id)
    original_error = OSError(errno.EIO, "存储失败 /private/thread", r"C:\private\thread")
    rename = ThreadCreationService._rename_staging_to_final
    rmtree, fsync, write = thread_creation.shutil.rmtree, thread_creation._fsync_directory, thread_creation._atomic_write_bytes
    if stage == "rebuild":
        def interrupt_write(path, payload):
            if path.name == "thread.json":
                raise original_error
            return write(path, payload)
        with monkeypatch.context() as failure:
            failure.setattr(thread_creation, "_atomic_write_bytes", interrupt_write)
            with pytest.raises(OSError):
                await create()

    def fail_rmtree(path, *args, **kwargs):
        if ".staging" in Path(path).relative_to(directory).parts:
            raise original_error
        return rmtree(path, *args, **kwargs)

    def redundant_final(self, staging, final, *args):
        thread_creation.shutil.copytree(staging, final)
        rename(self, staging, final, *args)

    def fail_fsync(path):
        relative = Path(path).relative_to(directory)
        if relative.parts and relative.parts[0] == "threads":
            raise original_error
        return fsync(path)

    with monkeypatch.context() as failure:
        if stage == "durability":
            failure.setattr(thread_creation, "_fsync_directory", fail_fsync)
        else:
            failure.setattr(thread_creation.shutil, "rmtree", fail_rmtree)
            if stage == "redundant":
                failure.setattr(ThreadCreationService, "_rename_staging_to_final", redundant_final)
        with pytest.raises(OSError) as caught:
            await create()
    record = service.control_store.get_thread_creation_record("child-physical-error")
    assert record.state == "preparing" and record.abort_reason is None
    assert caught.value.__cause__ is original_error
    assert "errno=5" in str(caught.value) and session_id in str(caught.value)
    assert "/private/" not in str(caught.value) and str(directory) not in str(caught.value)
    staging, final = directory / record.staging_locator, directory / record.final_relative_locator
    assert staging.is_dir() == (stage != "durability")
    assert final.is_dir() == (stage != "rebuild")
    assert not service.control_store.list_child_thread_rows()
    if stage == "redundant":
        # 外部同内容副本留下两目录时重入继续拒绝，不能把它静默吸收。
        with pytest.raises(RuntimeError, match="最终 locator 与 staging 同时存在"):
            await create()
        rmtree(staging)
    assert (await create()).child_thread_id == record.child_thread_id
    assert len(service.control_store.list_child_thread_rows()) == 1


@pytest.mark.skipif(os.name == "nt", reason="本用例核 POSIX 原生符号链接循环")
async def test_child_budget_rejects_real_symlink_loop_without_freezing_record(
    startup_container: AppContainer, child_creation,
) -> None:
    session_id, service, create = child_creation
    directory = startup_container.session_service.path_resolver.resolve_session_node(session_id)
    staging = directory / ".staging"
    staging.symlink_to(".staging", target_is_directory=True)
    with pytest.raises(RuntimeError) as caught:
        await create()
    assert "stage=path_budget" in str(caught.value) and session_id in str(caught.value)
    assert str(directory) not in str(caught.value)
    assert type(caught.value.__cause__) is RuntimeError and str(staging) in str(caught.value.__cause__)
    assert staging.is_symlink() and not service.control_store.list_pending_thread_creations()
    assert not service.control_store.list_child_thread_rows()
    staging.unlink()
    assert (await create()).record_state == "published"


@pytest.mark.parametrize("locator", ["/private/control.sqlite", r"C:\private\control.sqlite", r"\\server\private\control.sqlite"])
@pytest.mark.parametrize("failure_kind", ["cas", "deletion", "membership"])
async def test_delegated_publication_failure_keeps_safe_durable_reason(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, locator: str, failure_kind: str,
) -> None:
    session_id, service, create = child_creation
    session = await startup_container.session_service.get(session_id)
    parent_thread_id = await startup_container.session_service.resolve_main_thread(session_id)
    directory = startup_container.session_service.path_resolver.resolve_session_node(session_id)
    original_error = (SessionDeletionPendingError if failure_kind == "deletion" else RuntimeError)(f"发布冲突 {locator}")
    member_error = RuntimeError(f"成员读取失败 {locator}")
    get_member = service.control_store.get_collaboration_member
    publishing = False

    def refuse_publish(*args, **kwargs):
        nonlocal publishing
        publishing = True
        raise original_error

    def refuse_member(delegation_id):
        if publishing and failure_kind == "membership":
            raise member_error
        return get_member(delegation_id)

    async def delegate():
        # 创建、冻结身份和成员登记都由正式委派入口完成；故障先于 Job binding。
        return await startup_container.session_subagent_service.delegate(
            parent_session_id=session_id, parent_thread_id=parent_thread_id,
            parent_agent_id=session.current_agent_id, parent_job_id="job-publication-failure",
            parent_tool_call_id="tool-publication-failure", description="验证发布失败",
            subagent_type="general-purpose",
        )

    error_type = SessionDeletionPendingError if failure_kind == "deletion" else RuntimeError
    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR):
        failure.setattr(service.control_store, "publish_thread_creation_record", refuse_publish)
        failure.setattr(service.control_store, "get_collaboration_member", refuse_member)
        with pytest.raises(error_type) as caught:
            await delegate()
    record, = [service.control_store.get_thread_creation_record(row[0]) for row in
               service.control_store.connection.execute("SELECT thread_creation_idempotency_key FROM thread_creation_records")]
    assert record.state == "aborted" and record.abort_reason
    for detail in (str(caught.value), record.abort_reason):
        assert locator not in detail and str(directory) not in detail
        assert session_id in detail or "stage=abort_membership" in detail
        assert record.child_thread_id in detail
    assert caught.value.__cause__ is (member_error if failure_kind == "membership" else original_error)
    assert any(log.exc_info and log.exc_info[1] is original_error for log in caplog.records)
    if failure_kind == "membership":
        assert any(log.exc_info and log.exc_info[1] is member_error for log in caplog.records)
    if failure_kind == "deletion":
        assert "session_deletion_pending" in str(caught.value) and "session_deletion_pending" in record.abort_reason
    assert not (directory / record.staging_locator).exists()
    assert not (directory / record.final_relative_locator).exists()
    assert not service.control_store.list_child_thread_rows()
    assert not service.control_store.list_initial_execution_intents()
    member = service.control_store.get_collaboration_member(record.delegation_id)
    assert member.state == "cancelled" and member.child_thread_id == record.child_thread_id
    with closing(SessionControlStore(service.control_store.database_path)) as cold:
        assert cold.get_thread_creation_record(record.thread_creation_idempotency_key) == record
        assert cold.get_collaboration_member(record.delegation_id) == member
    # 委派重试首先校验不可重用的 cancelled member，不能换绑原 delegation。
    with pytest.raises(RuntimeError, match="collaboration member.*已取消") as repeated:
        await delegate()
    assert locator not in str(repeated.value)
    assert service.control_store.get_thread_creation_record(record.thread_creation_idempotency_key) == record
    assert (await create("fresh-after-failed-publication")).record_state == "published"


async def test_already_aborted_publication_failure_does_not_clean_again(
    startup_container: AppContainer, child_creation, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id, service, create = child_creation
    original_error = RuntimeError(r"发布后查询失败 C:\private\control.sqlite")

    def abort_then_fail(key, **kwargs):
        service.control_store.abort_thread_creation_record(key, "原 operation 已中止")
        raise original_error

    with monkeypatch.context() as failure, caplog.at_level(logging.ERROR):
        failure.setattr(service.control_store, "publish_thread_creation_record", abort_then_fail)
        with pytest.raises(RuntimeError, match="已非 preparing") as caught:
            await create()
    record = service.control_store.get_thread_creation_record("child-physical-error")
    directory = startup_container.session_service.path_resolver.resolve_session_node(session_id)
    final = directory / record.final_relative_locator
    assert record.state == "aborted" and record.abort_reason == "原 operation 已中止"
    assert r"C:\private" not in str(caught.value) and caught.value.__cause__ is original_error
    assert session_id in str(caught.value) and record.child_thread_id in str(caught.value)
    assert final.is_dir() and (final / "artifacts/notes/plan.md").read_text() == "原始计划"
    assert any(log.exc_info and log.exc_info[1] is original_error for log in caplog.records)
    assert not service.control_store.list_child_thread_rows()
    assert not service.control_store.list_initial_execution_intents()
    with pytest.raises(thread_creation.ThreadCreationAbortedError) as repeated:
        await create()
    assert repeated.value.record == record and final.is_dir()
