from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.core.identifier import create_prefixed_id
from app.gateway.control.coordinator import SessionGeneratorCoordinator
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.schemas import GeneratorDefinitionDTO


logger = logging.getLogger(__name__)


class SessionGeneratorScheduler:
    def __init__(
        self,
        *,
        store: SessionGeneratorStore,
        coordinator: SessionGeneratorCoordinator,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._definition_errors: dict[str, str] = {}

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("会话生成器调度器已经启动")
        self._stop_event.clear()
        self._fatal_error = None
        self._task = asyncio.create_task(
            self._run_loop(),
            name="session-generator-scheduler",
        )
        self._task.add_done_callback(self._task_finished)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    def assert_healthy(self) -> None:
        if self._fatal_error is not None:
            raise RuntimeError(
                "会话生成器调度主循环已经异常退出: "
                f"{type(self._fatal_error).__name__}: {self._fatal_error}"
            )
        if self._definition_errors:
            details = "; ".join(
                f"{generator_id}={error}"
                for generator_id, error in sorted(self._definition_errors.items())
            )
            raise RuntimeError(f"会话生成器调度存在失败定义: {details}")
        if self._task is None or self._task.done():
            raise RuntimeError("会话生成器调度主循环未运行")

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stop_event.is_set():
            return
        error = task.exception()
        self._fatal_error = error or RuntimeError("调度主循环意外正常退出")
        logger.error(
            "会话生成器调度主循环退出",
            exc_info=(
                (type(error), error, error.__traceback__)
                if error is not None
                else None
            ),
        )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._run_due(datetime.now(timezone.utc))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _run_due(self, now: datetime) -> None:
        for definition in self._store.list_definitions().items:
            try:
                if not definition.enabled or definition.status != "ready":
                    self._definition_errors.pop(definition.generator_id, None)
                    continue
                if definition.trigger.type == "manual":
                    self._definition_errors.pop(definition.generator_id, None)
                    continue
                due, skipped, next_run_at = self._evaluate_schedule(definition, now)
                for scheduled_for in skipped:
                    run = self._store.create_run(
                        generator_id=definition.generator_id,
                        idempotency_key=(
                            f"{definition.generator_id}:{definition.trigger.type}:"
                            f"{scheduled_for.isoformat()}"
                        ),
                        trigger_type=definition.trigger.type,
                        scheduled_for=scheduled_for,
                    )
                    if run.status != "skipped":
                        self._store.write_run(
                            run.model_copy(
                                update={
                                    "status": "skipped",
                                    "ended_at": now,
                                    "error": "按 misfire=skip 跳过积压触发",
                                }
                            )
                        )
                for scheduled_for in due:
                    await self._coordinator.run_scheduled(
                        definition.generator_id,
                        scheduled_for=scheduled_for,
                        trigger_type=definition.trigger.type,
                        request_id=create_prefixed_id("req"),
                    )
                self._store.write_schedule_state(
                    definition.generator_id,
                    {
                        "schema_version": 1,
                        "definition_revision": definition.revision,
                        "last_evaluated_at": now.isoformat(),
                        "next_run_at": next_run_at.isoformat(),
                    },
                )
                self._definition_errors.pop(definition.generator_id, None)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                self._definition_errors[definition.generator_id] = message
                logger.exception(
                    "定时会话生成定义处理失败: generator_id=%s",
                    definition.generator_id,
                )

    def _evaluate_schedule(
        self,
        definition: GeneratorDefinitionDTO,
        now: datetime,
    ) -> tuple[list[datetime], list[datetime], datetime]:
        state = self._store.read_schedule_state(definition.generator_id)
        if state is not None and state.get("definition_revision") == definition.revision:
            raw_next = state.get("next_run_at")
            if not isinstance(raw_next, str):
                raise RuntimeError(
                    f"生成器调度状态缺少 next_run_at: {definition.generator_id}"
                )
            candidate = datetime.fromisoformat(raw_next)
        else:
            candidate = self._next_time(definition, definition.created_at)
        due: list[datetime] = []
        while candidate <= now and len(due) < 100:
            due.append(candidate)
            candidate = self._next_time(definition, candidate)
        if candidate <= now:
            if definition.policies.misfire == "catch_up":
                return due, [], candidate
            latest_due = self._latest_due_time(definition, now)
            candidate = self._next_time(definition, latest_due)
            if definition.policies.misfire == "skip":
                return [], [latest_due], candidate
            return [latest_due], [], candidate
        if len(due) <= 1 or definition.policies.misfire == "catch_up":
            return due, [], candidate
        if definition.policies.misfire == "skip":
            return [], [due[-1]], candidate
        return [due[-1]], [], candidate

    @staticmethod
    def _latest_due_time(
        definition: GeneratorDefinitionDTO,
        now: datetime,
    ) -> datetime:
        trigger = definition.trigger
        if trigger.type == "interval":
            if trigger.interval_seconds is None:
                raise RuntimeError(
                    f"interval 生成器缺少 interval_seconds: {definition.generator_id}"
                )
            elapsed = max(0, (now - definition.created_at).total_seconds())
            intervals = int(elapsed // trigger.interval_seconds)
            return definition.created_at + timedelta(
                seconds=intervals * trigger.interval_seconds
            )
        if trigger.type != "cron" or trigger.expression is None:
            raise RuntimeError(
                f"不支持的定时触发器: {definition.generator_id}/{trigger.type}"
            )
        zone = ZoneInfo(trigger.timezone)
        latest = croniter(trigger.expression, now.astimezone(zone)).get_prev(datetime)
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=zone)
        return latest.astimezone(timezone.utc)

    @staticmethod
    def _next_time(
        definition: GeneratorDefinitionDTO,
        previous: datetime,
    ) -> datetime:
        trigger = definition.trigger
        if trigger.type == "interval":
            if trigger.interval_seconds is None:
                raise RuntimeError(
                    f"interval 生成器缺少 interval_seconds: {definition.generator_id}"
                )
            return previous + timedelta(seconds=trigger.interval_seconds)
        if trigger.type != "cron" or trigger.expression is None:
            raise RuntimeError(
                f"不支持的定时触发器: {definition.generator_id}/{trigger.type}"
            )
        zone = ZoneInfo(trigger.timezone)
        localized_previous = previous.astimezone(zone)
        next_local = croniter(trigger.expression, localized_previous).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=zone)
        return next_local.astimezone(timezone.utc)
