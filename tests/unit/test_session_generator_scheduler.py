from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.control.schemas import (
    GeneratorDefinitionCreateRequest,
    GeneratorPlacementDTO,
    GeneratorPoliciesDTO,
    GeneratorTriggerDTO,
)


class _UnusedCoordinator:
    pass


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
