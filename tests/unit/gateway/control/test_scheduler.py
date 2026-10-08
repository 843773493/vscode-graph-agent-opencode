from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.schemas.gateway_control import (
    GeneratorDefinitionCreateRequest,
    GeneratorPlacementDTO,
    GeneratorPoliciesDTO,
    GeneratorTriggerDTO,
)


class _UnusedCoordinator:
    pass


class _FailingCoordinator:
    async def run_scheduled(
        self,
        generator_id: str,
        *,
        scheduled_for: datetime,
        trigger_type: str,
        request_id: str,
    ) -> None:
        del generator_id, scheduled_for, trigger_type, request_id
        raise RuntimeError("调度测试协调器失败")


def test_scheduler_runtime_config_updates_next_poll_and_wakes_loop(tmp_path: Path) -> None:
    store = SessionGeneratorStore(root=tmp_path)
    scheduler = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )

    scheduler.update_runtime_config(poll_interval_seconds=2.5)

    assert scheduler._poll_interval_seconds == 2.5
    assert scheduler._wake_event.is_set()


def test_deleted_definition_clears_scheduler_error_on_next_poll(
    tmp_path: Path,
) -> None:
    async def exercise_lifecycle() -> None:
        now = datetime.now(UTC)
        store, definition = _definition(
            tmp_path,
            misfire="run_latest",
            created_at=now - timedelta(minutes=2),
        )
        store.write_schedule_state(
            definition.generator_id,
            {
                "schema_version": 1,
                "definition_revision": definition.revision,
                "last_evaluated_at": (now - timedelta(minutes=2)).isoformat(),
                "next_run_at": (now - timedelta(seconds=1)).isoformat(),
            },
        )
        scheduler = SessionGeneratorScheduler(
            store=store,
            coordinator=_FailingCoordinator(),  # type: ignore[arg-type]
            poll_interval_seconds=0.01,
        )
        await scheduler.start()
        try:
            failure = None
            for _ in range(100):
                try:
                    scheduler.assert_healthy()
                except RuntimeError as error:
                    if definition.generator_id in str(error):
                        failure = error
                        break
                await asyncio.sleep(0.01)
            assert failure is not None
            assert "调度测试协调器失败" in str(failure)

            # 失败定义仍在 store 中时，实际轮询保留其诊断。
            await asyncio.sleep(0.03)
            try:
                scheduler.assert_healthy()
            except RuntimeError as error:
                assert definition.generator_id in str(error)
                assert "调度测试协调器失败" in str(error)
            else:
                raise AssertionError("仍存在的失败定义不应清除健康错误")

            store.delete_definition(definition.generator_id)
            for _ in range(100):
                try:
                    scheduler.assert_healthy()
                except RuntimeError:
                    await asyncio.sleep(0.01)
                else:
                    break
            else:
                raise AssertionError("后续实际轮询未清理已删除定义的健康错误")
        finally:
            await scheduler.stop()

    asyncio.run(exercise_lifecycle())


def _definition(
    root: Path,
    *,
    misfire: str,
    created_at: datetime,
):
    store = SessionGeneratorStore(root=root)
    definition = store.create_definition(
        GeneratorDefinitionCreateRequest(
            name="调度测试",
            trigger=GeneratorTriggerDTO(
                type="interval",
                interval_seconds=60,
                timezone="UTC",
            ),
            placement=GeneratorPlacementDTO(
                kind="workspace",
                workspace_id="gw_scheduler",
            ),
            execution_workspace_id="gw_scheduler",
            policies=GeneratorPoliciesDTO(misfire=misfire),
            config={"prompt": "调度测试"},
        )
    )
    return store, definition.model_copy(update={"created_at": created_at})


def test_interval_run_latest_persists_next_evaluation(tmp_path: Path) -> None:
    started_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    store, definition = _definition(
        tmp_path,
        misfire="run_latest",
        created_at=started_at,
    )
    scheduler = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )

    due, skipped, next_run_at = scheduler._evaluate_schedule(
        definition,
        started_at + timedelta(seconds=185),
    )

    assert due == [started_at + timedelta(seconds=180)]
    assert skipped == []
    assert next_run_at == started_at + timedelta(seconds=240)

    store.write_schedule_state(
        definition.generator_id,
        {
            "schema_version": 1,
            "definition_revision": definition.revision,
            "last_evaluated_at": (started_at + timedelta(seconds=185)).isoformat(),
            "next_run_at": next_run_at.isoformat(),
        },
    )
    restarted = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )
    due_after_restart, skipped_after_restart, persisted_next = (
        restarted._evaluate_schedule(
            definition,
            started_at + timedelta(seconds=230),
        )
    )
    assert due_after_restart == []
    assert skipped_after_restart == []
    assert persisted_next == next_run_at


def test_interval_skip_records_latest_missed_trigger(tmp_path: Path) -> None:
    started_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    store, definition = _definition(
        tmp_path,
        misfire="skip",
        created_at=started_at,
    )
    scheduler = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )

    due, skipped, next_run_at = scheduler._evaluate_schedule(
        definition,
        started_at + timedelta(seconds=185),
    )

    assert due == []
    assert skipped == [started_at + timedelta(seconds=180)]
    assert next_run_at == started_at + timedelta(seconds=240)


def test_large_catch_up_is_batched_without_stopping_scheduler(tmp_path: Path) -> None:
    started_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    store, definition = _definition(
        tmp_path,
        misfire="catch_up",
        created_at=started_at,
    )
    scheduler = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )

    due, skipped, next_run_at = scheduler._evaluate_schedule(
        definition,
        started_at + timedelta(seconds=60 * 250),
    )

    assert len(due) == 100
    assert due[0] == started_at + timedelta(seconds=60)
    assert due[-1] == started_at + timedelta(seconds=60 * 100)
    assert skipped == []
    assert next_run_at == started_at + timedelta(seconds=60 * 101)


def test_large_skip_never_executes_latest_missed_trigger(tmp_path: Path) -> None:
    started_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
    store, definition = _definition(
        tmp_path,
        misfire="skip",
        created_at=started_at,
    )
    scheduler = SessionGeneratorScheduler(
        store=store,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
    )

    due, skipped, next_run_at = scheduler._evaluate_schedule(
        definition,
        started_at + timedelta(seconds=60 * 250),
    )

    assert due == []
    assert skipped == [started_at + timedelta(seconds=60 * 250)]
    assert next_run_at == started_at + timedelta(seconds=60 * 251)
