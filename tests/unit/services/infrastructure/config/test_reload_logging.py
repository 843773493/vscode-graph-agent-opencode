from __future__ import annotations

import asyncio
import logging

import pytest

from app.services.infrastructure.config import (
    ConfigSnapshotStore,
    build_config_snapshot,
)


@pytest.mark.asyncio
async def test_config_reload_success_emits_info_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidates = iter(
        (
            build_config_snapshot({"value": "before"}, source_paths=()),
            build_config_snapshot({"value": "after"}, source_paths=()),
        )
    )
    store = ConfigSnapshotStore(candidate_builder=lambda: next(candidates))
    store.initialize()
    caplog.set_level(
        logging.INFO,
        logger="app.services.infrastructure.config.store",
    )

    assert await store.reload() is True

    assert "配置热重载成功" in caplog.text
    assert store.current().revision in caplog.text


@pytest.mark.asyncio
async def test_config_reload_failure_emits_exception_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    initial = build_config_snapshot({"value": "before"}, source_paths=())

    def build_candidate():
        raise ValueError("配置内容无效")

    store = ConfigSnapshotStore(candidate_builder=build_candidate)
    store.initialize(initial)
    caplog.set_level(
        logging.ERROR,
        logger="app.services.infrastructure.config.store",
    )

    with pytest.raises(ValueError, match="配置内容无效"):
        await store.reload()

    assert "配置热重载失败" in caplog.text
    assert "配置内容无效" in caplog.text


@pytest.mark.asyncio
async def test_snapshot_publication_precedes_drain_and_survives_cancellation() -> None:
    candidates = iter(build_config_snapshot({"version": value}, source_paths=()) for value in (1, 2))
    store = ConfigSnapshotStore(candidate_builder=lambda: next(candidates))
    previous = store.current()
    entered = asyncio.Event()

    async def publish_candidate(_previous, candidate, publish) -> None:
        publish()
        assert store.current() is candidate
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(store.reload(candidate_applier=publish_candidate))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert store.current().revision != previous.revision
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert store.current().to_dict() == {"version": 2}


@pytest.mark.asyncio
async def test_snapshot_publisher_returning_without_commit_is_explicit_failure() -> None:
    candidates = iter(build_config_snapshot({"version": value}, source_paths=()) for value in (1, 2))
    store = ConfigSnapshotStore(candidate_builder=lambda: next(candidates))
    previous = store.current()

    async def missing_commit(_previous, _candidate, _publish) -> None:
        pass

    with pytest.raises(RuntimeError, match="尚未发布候选"):
        await store.reload(candidate_applier=missing_commit)
    assert store.current() is previous and not store.status().healthy


@pytest.mark.asyncio
async def test_cancelled_cleanup_group_is_recorded_without_publishing_candidate() -> None:
    candidates = iter(build_config_snapshot({"version": value}, source_paths=()) for value in (1, 2))
    store = ConfigSnapshotStore(candidate_builder=lambda: next(candidates))
    previous = store.current()

    async def fail_cleanup(_previous, _candidate, _publish) -> None:
        raise BaseExceptionGroup("候选准备和等待回收取消", [RuntimeError("准备失败"), asyncio.CancelledError()])

    with pytest.raises(BaseExceptionGroup):
        await store.reload(candidate_applier=fail_cleanup)
    assert store.current() is previous and not store.status().healthy
    assert store.status().reason == "apply_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_path", ["/private-config/source.jsonc", r"C:\private-config\source.jsonc", r"\\server\private-config\source.jsonc"])
@pytest.mark.parametrize("phase", ["build", "prepare", "drain", "cancel"])
async def test_reload_failure_state_uses_facts_and_preserves_original_exception(
    native_path: str, phase: str, caplog: pytest.LogCaptureFixture,
) -> None:
    error = asyncio.CancelledError(native_path) if phase == "cancel" else PermissionError(13, native_path, native_path)
    failed = True
    before = build_config_snapshot({"value": "before"}, source_paths=())
    after = build_config_snapshot({"value": "after"}, source_paths=())

    def build_candidate():
        if failed and phase == "build":
            raise error
        return after

    async def apply_candidate(_previous, _candidate, publish) -> None:
        if phase == "drain":
            publish()
        if failed:
            raise error
        if phase != "drain":
            publish()

    store = ConfigSnapshotStore(candidate_builder=build_candidate)
    store.initialize(before)
    with pytest.raises(type(error)) as caught:
        await store.reload(candidate_applier=apply_candidate)
    assert caught.value is error
    assert store.current() is (after if phase == "drain" else before)
    status = store.status()
    assert not status.healthy
    assert status.reason == ("invalid_config" if phase == "build" else "apply_failed")
    assert status.last_error is not None
    assert "private-config" not in status.last_error
    assert type(error).__name__ in status.last_error
    if isinstance(error, OSError):
        assert "errno=13" in status.last_error
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    failed = False
    await store.reload(candidate_applier=apply_candidate)
    assert store.current() is after
    assert store.status().healthy and store.status().last_error is None
