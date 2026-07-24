from __future__ import annotations

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
