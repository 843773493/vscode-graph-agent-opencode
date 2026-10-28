"""工具测试 API、失败诊断与持久结果的故障注入；不代替真实 Provider 验收。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.api import tools as tools_api
from app.api.deps import get_tool_test_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.tool_test import ToolTestRunDTO
from app.tool_testing.definitions import PreparedToolTest, ToolTestEvaluation
from app.tool_testing.model_invocation import ToolCallProbeInvocationError
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.service import ToolTestService
from app.tool_testing.store import ToolTestStore
from tests.support.tool_test_faults import (
    RuntimeInjectedArgumentBoundModel,
    RuntimeInjectedArgumentCase,
    RuntimeInjectedArgumentModel,
    SingleProviderRuntimeConfig,
)


@pytest.fixture
def tool_test_http_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(tools_api.router, prefix="/api/v1")
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    return app


@pytest.mark.parametrize("native", ["/private/native-failure.txt", r"C:\private\native-failure.txt", r"\\server\share\native-failure.txt"])
@pytest.mark.parametrize("stage", ["directory", "prepare", "model_build", "model_call", "execute", "tool_error", "evaluate"])
@pytest.mark.asyncio
async def test_failure_records_keep_native_details_in_diagnostics(
    tmp_path: Path, tool_test_http_app: FastAPI, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native: str, stage: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ToolTestStore(root=workspace / ".boxteam/tool_tests")
    original = PermissionError(13, f"原生失败正文包含 {native}", native)
    failing = True
    calls: list[str] = []

    def fail_at(current: str) -> None:
        calls.append(current)
        if failing and stage == current:
            raise original

    prepare_directory = store.prepare_case_dir

    def directory(**kwargs: str) -> Path:
        fail_at("directory")
        return prepare_directory(**kwargs)

    prepare = RuntimeInjectedArgumentCase.prepare

    def prepare_case(self: RuntimeInjectedArgumentCase, **kwargs: Path) -> PreparedToolTest:
        fail_at("prepare")
        prepared = prepare(self, **kwargs)

        def callback() -> str:
            fail_at("execute")
            return "injected"

        if failing and stage == "tool_error":
            @tool
            async def runtime_aware(value: str) -> ToolMessage:
                """返回明确的工具失败回执。"""
                del value
                calls.append("tool_error")
                return ToolMessage(content=str(original), status="error", tool_call_id="call-runtime-aware")

            return PreparedToolTest(prompt=prepared.prompt, tool=runtime_aware)
        prepared.injected_arguments["callback"] = callback
        return prepared

    evaluate = RuntimeInjectedArgumentCase.evaluate

    def evaluate_case(
        self: RuntimeInjectedArgumentCase, *, attempt_root: Path, tool_result: object,
    ) -> ToolTestEvaluation:
        fail_at("evaluate")
        return evaluate(self, attempt_root=attempt_root, tool_result=tool_result)

    invoke = RuntimeInjectedArgumentBoundModel.ainvoke

    async def invoke_model(self: RuntimeInjectedArgumentBoundModel, messages: list[object]) -> AIMessage:
        fail_at("model_call")
        return await invoke(self, messages)

    def build_model(**_kwargs: object) -> RuntimeInjectedArgumentModel:
        fail_at("model_build")
        return RuntimeInjectedArgumentModel()

    monkeypatch.setattr(store, "prepare_case_dir", directory)
    monkeypatch.setattr(RuntimeInjectedArgumentCase, "prepare", prepare_case)
    monkeypatch.setattr(RuntimeInjectedArgumentCase, "evaluate", evaluate_case)
    monkeypatch.setattr(RuntimeInjectedArgumentBoundModel, "ainvoke", invoke_model)
    service = ToolTestService(
        config_service=SingleProviderRuntimeConfig(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[RuntimeInjectedArgumentCase()]),
        store=store, workspace_root=workspace, asset_root=tmp_path / "assets", model_builder=build_model,
    )
    tool_test_http_app.dependency_overrides[get_tool_test_service] = lambda: service
    caplog.set_level(logging.ERROR, logger="app.tool_testing.service")

    async with AsyncClient(transport=ASGITransport(app=tool_test_http_app), base_url="http://test") as client:
        def assert_no_native(value: object) -> None:
            if isinstance(value, str):
                assert native not in value
            elif isinstance(value, dict):
                for item in value.values():
                    assert_no_native(item)
            elif isinstance(value, list):
                for item in value:
                    assert_no_native(item)

        async def run(request_id: str) -> ToolTestRunDTO:
            response = await client.post("/api/v1/tools/runtime_aware/tests", json={}, headers={"X-Request-ID": request_id})
            assert response.status_code == 200, response.text
            run_id = response.json()["data"]["run_id"]
            async with asyncio.timeout(3):
                while True:
                    response = await client.get(f"/api/v1/tools/tests/{run_id}")
                    assert response.status_code == 200, response.text
                    assert response.headers["X-Request-ID"] == response.json()["request_id"]
                    result = ToolTestRunDTO.model_validate(response.json()["data"])
                    if result.status in {"completed", "failed"}:
                        return result
                    await asyncio.sleep(0.01)

        result = await run("tool-test-original-failure")
        assert result.status == ("failed" if stage == "directory" else "completed")
        if stage == "directory":
            assert not result.attempts
            failure = result.error
        else:
            attempt, = result.attempts
            assert attempt.status == "failed" and not attempt.passed
            assert attempt.execution_succeeded is (stage == "evaluate")
            assert attempt.tool_called is (stage in {"execute", "tool_error", "evaluate"})
            assert attempt.model_calls == int(stage in {"model_call", "execute", "tool_error", "evaluate"})
            assert attempt.transient_retries == 0
            assert result.providers[0].failed == 1
            failure = attempt.error
        failure_stage = "run" if stage == "directory" else "execute" if stage == "tool_error" else stage
        error_type = "RuntimeError" if stage == "tool_error" else "PermissionError"
        expected = f"工具测试失败: stage={failure_stage}, type={error_type}"
        assert failure == expected + ("" if stage == "tool_error" else ", errno=13")
        assert calls.count(stage) == 1
        assert_no_native(result.model_dump(mode="json"))
        for path in store.root.rglob("*.json"):
            assert_no_native(json.loads(path.read_text(encoding="utf-8")))
        assert ToolTestRunDTO.model_validate(ToolTestStore(root=store.root).read_run(result.run_id)) == result
        records = [record for record in caplog.records if record.name == "app.tool_testing.service"]
        record, = records
        assert record.__dict__["request_id"] == "tool-test-original-failure"
        assert record.exc_info is not None
        error = record.exc_info[1]
        if stage == "tool_error":
            assert isinstance(error, RuntimeError) and str(original) in str(error)
        elif stage == "model_call":
            assert isinstance(error, ToolCallProbeInvocationError)
            assert error.cause is original
        else:
            assert error is original

        failing = False
        calls.clear()
        retried = await run("tool-test-explicit-retry")
        assert retried.run_id != result.run_id
        assert retried.status == "completed" and retried.error is None
        attempt, = retried.attempts
        assert attempt.passed and attempt.execution_succeeded and attempt.error is None
        assert attempt.model_calls == 1 and retried.providers[0].passed == 1
    await service.shutdown()
