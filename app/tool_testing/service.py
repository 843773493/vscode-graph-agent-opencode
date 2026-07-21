from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.agents.agent_factory import build_model_from_provider
from app.core.identifier import create_prefixed_id
from app.schemas.public_v2.tool_test import (
    ToolTestAttemptDTO,
    ToolTestProviderResultDTO,
    ToolTestRunDTO,
    ToolTestStartRequest,
)
from app.services.infrastructure.config_service import ConfigService
from app.tool_testing.definitions import ToolTestCase, get_model_tool_parameters
from app.tool_testing.model_invocation import (
    ToolCallProbeInvocationError,
    probe_tool_call_with_transient_retries,
)
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.store import ToolTestStore


ModelBuilder = Callable[[dict[str, Any], dict[str, Any]], Any]


class ToolTestService:
    SYSTEM_PROMPT = (
        "你正在执行一次独立的工具调用准确率测试。允许先进行内部 reasoning，"
        "不要求跳过思考；但 reasoning 不是最终结果。完成思考后，必须使用本次唯一提供的"
        "工具完成用户指令。只有真实 tool call 才算完成；普通正文、Markdown、JSON 或"
        "推理文字中的模拟调用均不算成功。不要调用未提供的工具，也不要虚构用户未提供的事实。"
    )

    def __init__(
        self,
        *,
        config_service: ConfigService,
        registry: ToolTestRegistry,
        store: ToolTestStore,
        workspace_root: Path,
        asset_root: Path,
        model_builder: ModelBuilder = build_model_from_provider,
    ) -> None:
        self._config_service = config_service
        self._registry = registry
        self._store = store
        self._workspace_root = workspace_root
        self._asset_root = asset_root
        self._model_builder = model_builder
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_tools: set[str] = set()
        self._provider_locks: dict[str, asyncio.Lock] = {}
        self._state_lock = asyncio.Lock()

    @property
    def supported_tools(self) -> set[str]:
        return self._registry.supported_tools()

    async def start(
        self,
        *,
        tool_name: str,
        request: ToolTestStartRequest,
    ) -> ToolTestRunDTO:
        cases = self._registry.cases_for(tool_name)
        if not cases:
            raise ValueError(f"工具尚未提供模型调用测试: {tool_name}")
        runtime_config = self._config_service.get_agent_runtime_config(request.agent_id)
        configured_providers = runtime_config.get("providers")
        if not isinstance(configured_providers, list) or not configured_providers:
            raise ValueError(f"Agent 没有可测试的 provider: {request.agent_id}")
        provider_map = {
            str(provider.get("id")): provider
            for provider in configured_providers
            if isinstance(provider, dict) and provider.get("id")
        }
        provider_ids = request.provider_ids or list(provider_map)
        unknown = set(provider_ids) - set(provider_map)
        if unknown:
            raise ValueError(f"包含未配置的 provider: {', '.join(sorted(unknown))}")
        if tool_name in self._active_tools:
            raise RuntimeError(f"工具测试正在运行，不能重复启动: {tool_name}")

        now = datetime.now(UTC)
        total_per_provider = len(cases) * request.repetitions
        run = ToolTestRunDTO(
            run_id=create_prefixed_id("tooltest"),
            tool_name=tool_name,
            status="queued",
            progress=0,
            created_at=now,
            updated_at=now,
            repetitions=request.repetitions,
            providers=[
                ToolTestProviderResultDTO(
                    provider_id=provider_id,
                    model=str(provider_map[provider_id].get("model") or ""),
                    status="queued",
                    total=total_per_provider,
                )
                for provider_id in provider_ids
            ],
        )
        self._store.reset_tool(tool_name)
        self._store.write_run(run.run_id, run.model_dump(mode="json"))
        self._active_tools.add(tool_name)
        task = asyncio.create_task(
            self._run(
                run_id=run.run_id,
                cases=cases,
                providers=[provider_map[provider_id] for provider_id in provider_ids],
                runtime_config=runtime_config,
                repetitions=request.repetitions,
            ),
            name=f"tool-test:{run.run_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._active_tools.discard(tool_name))
        return run

    def get(self, run_id: str) -> ToolTestRunDTO:
        return ToolTestRunDTO.model_validate(self._store.read_run(run_id))

    def list(self, *, tool_name: str | None = None, limit: int = 20) -> list[ToolTestRunDTO]:
        return [
            ToolTestRunDTO.model_validate(item)
            for item in self._store.list_runs(tool_name=tool_name, limit=limit)
        ]

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(
        self,
        *,
        run_id: str,
        cases: list[ToolTestCase],
        providers: list[dict[str, Any]],
        runtime_config: dict[str, Any],
        repetitions: int,
    ) -> None:
        await self._mutate_run(run_id, lambda run: self._set_run_status(run, "running"))
        try:
            await asyncio.gather(
                *[
                    self._run_provider(
                        run_id=run_id,
                        provider=provider,
                        runtime_config=runtime_config,
                        cases=cases,
                        repetitions=repetitions,
                    )
                    for provider in providers
                ]
            )
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            error_message = str(error)
            await self._mutate_run(
                run_id,
                lambda run: self._fail_run(run, error_message),
            )
            return
        await self._mutate_run(run_id, lambda run: self._set_run_status(run, "completed"))

    async def _run_provider(
        self,
        *,
        run_id: str,
        provider: dict[str, Any],
        runtime_config: dict[str, Any],
        cases: list[ToolTestCase],
        repetitions: int,
    ) -> None:
        provider_id = str(provider["id"])
        lock = self._provider_locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            await self._mutate_run(
                run_id,
                lambda run: self._set_provider_status(run, provider_id, "running"),
            )
            for case in cases:
                for _ in range(repetitions):
                    attempt = await self._run_attempt(
                        run_id=run_id,
                        provider=provider,
                        runtime_config=runtime_config,
                        case=case,
                    )
                    await self._mutate_run(
                        run_id,
                        lambda run, item=attempt: self._append_attempt(run, item),
                    )
            await self._mutate_run(
                run_id,
                lambda run: self._set_provider_status(run, provider_id, "completed"),
            )

    async def _run_attempt(
        self,
        *,
        run_id: str,
        provider: dict[str, Any],
        runtime_config: dict[str, Any],
        case: ToolTestCase,
    ) -> ToolTestAttemptDTO:
        provider_id = str(provider["id"])
        model_name = str(provider.get("model") or "")
        attempt_id = create_prefixed_id("attempt")
        case_dir = self._store.prepare_case_dir(
            tool_name=case.tool_name,
            provider_id=provider_id,
            case_id=case.case_id,
        )
        attempt_root = case_dir / "workspace"
        started = time.monotonic()
        tool_called = False
        execution_succeeded = False
        model_calls = 0
        reasoning_only_calls = 0
        transient_retries = 0
        exchanges = []
        tool_result: object | None = None
        request_payload: dict[str, Any] = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "tool_name": case.tool_name,
            "case_id": case.case_id,
            "provider": _redact_secrets(provider),
        }
        try:
            prepared = case.prepare(
                workspace_root=self._workspace_root,
                attempt_root=attempt_root,
                asset_root=self._asset_root,
            )
            messages = [
                SystemMessage(self.SYSTEM_PROMPT),
                HumanMessage(prepared.prompt),
            ]
            request_payload.update(
                {
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "tool": {
                        "name": prepared.tool.name,
                        "description": prepared.tool.description,
                        "parameters": get_model_tool_parameters(prepared.tool),
                    },
                }
            )
            model = self._model_builder(
                provider=provider,
                runtime_config=runtime_config,
            )
            probe = await probe_tool_call_with_transient_retries(
                model=model,
                tool=prepared.tool,
                messages=messages,
            )
            exchanges = list(probe.exchanges)
            model_calls = probe.model_calls
            reasoning_only_calls = probe.reasoning_only_calls
            transient_retries = probe.transient_retries
            tool_call = probe.tool_call
            if tool_call is None:
                raise RuntimeError(probe.failure or "模型未产生工具调用")
            tool_called = True
            arguments = tool_call.get("args")
            if not isinstance(arguments, dict):
                raise TypeError(f"工具参数不是对象: {arguments!r}")
            # arguments 只来自模型可见的 tool_call_schema；运行时对象等后端参数
            # 由测试用例在执行边界注入，不能写进模型请求或请求回放。
            tool_result = await prepared.tool.ainvoke(
                {**arguments, **prepared.injected_arguments}
            )
            if isinstance(tool_result, ToolMessage) and tool_result.status == "error":
                raise RuntimeError(f"工具执行失败: {tool_result.content}")
            execution_succeeded = True
            evaluation = case.evaluate(
                attempt_root=attempt_root,
                tool_result=tool_result,
            )
            attempt = ToolTestAttemptDTO(
                attempt_id=attempt_id,
                case_id=case.case_id,
                provider_id=provider_id,
                model=model_name,
                status="completed",
                passed=evaluation.passed,
                tool_called=True,
                execution_succeeded=True,
                model_calls=model_calls,
                reasoning_only_calls=reasoning_only_calls,
                transient_retries=transient_retries,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=evaluation.detail,
            )
        except Exception as error:
            if isinstance(error, ToolCallProbeInvocationError):
                model_calls = error.model_calls
                reasoning_only_calls = error.reasoning_only_calls
                transient_retries = error.transient_retries
                exchanges = list(error.exchanges)
            attempt = ToolTestAttemptDTO(
                attempt_id=attempt_id,
                case_id=case.case_id,
                provider_id=provider_id,
                model=model_name,
                status="failed",
                passed=False,
                tool_called=tool_called,
                execution_succeeded=execution_succeeded,
                model_calls=model_calls,
                reasoning_only_calls=reasoning_only_calls,
                transient_retries=transient_retries,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail="工具测试失败",
                error=f"{type(error).__name__}: {error}",
            )
        self._store.write_case_json(
            tool_name=case.tool_name,
            provider_id=provider_id,
            case_id=case.case_id,
            file_name="request.json",
            payload=request_payload,
        )
        self._store.write_case_json(
            tool_name=case.tool_name,
            provider_id=provider_id,
            case_id=case.case_id,
            file_name="response.json",
            payload={
                "run_id": run_id,
                "attempt_id": attempt_id,
                "calls": [asdict(exchange) for exchange in exchanges],
                "tool_result": tool_result,
                "error": attempt.error,
            },
        )
        self._store.write_case_json(
            tool_name=case.tool_name,
            provider_id=provider_id,
            case_id=case.case_id,
            file_name="result.json",
            payload=attempt.model_dump(mode="json"),
        )
        return attempt

    async def _mutate_run(
        self,
        run_id: str,
        mutator: Callable[[ToolTestRunDTO], None],
    ) -> None:
        async with self._state_lock:
            run = self.get(run_id)
            mutator(run)
            run.updated_at = datetime.now(UTC)
            self._store.write_run(run_id, run.model_dump(mode="json"))

    @staticmethod
    def _set_run_status(run: ToolTestRunDTO, status: str) -> None:
        run.status = status  # type: ignore[assignment]
        if status == "completed":
            run.progress = 100

    @staticmethod
    def _fail_run(run: ToolTestRunDTO, error: str) -> None:
        run.status = "failed"
        run.error = error

    @staticmethod
    def _set_provider_status(
        run: ToolTestRunDTO,
        provider_id: str,
        status: str,
    ) -> None:
        provider = next(item for item in run.providers if item.provider_id == provider_id)
        provider.status = status  # type: ignore[assignment]

    @staticmethod
    def _append_attempt(run: ToolTestRunDTO, attempt: ToolTestAttemptDTO) -> None:
        run.attempts.append(attempt)
        provider = next(
            item for item in run.providers if item.provider_id == attempt.provider_id
        )
        provider.completed += 1
        provider.model_calls += attempt.model_calls
        provider.reasoning_only_calls += attempt.reasoning_only_calls
        provider.transient_retries += attempt.transient_retries
        if attempt.passed:
            provider.passed += 1
        else:
            provider.failed += 1
        provider.success_rate = round(provider.passed / provider.completed * 100, 1)
        completed = sum(item.completed for item in run.providers)
        total = sum(item.total for item in run.providers)
        run.progress = int(completed / total * 100) if total else 0


_SECRET_KEY_PARTS = ("api_key", "authorization", "password", "secret", "token")


def _redact_secrets(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): (
                "***REDACTED***"
                if any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value
