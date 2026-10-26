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
