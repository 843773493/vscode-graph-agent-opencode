"""工具测试 API、失败诊断与持久结果的故障注入；不代替真实 Provider 验收。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.api import tools as tools_api
from app.api.deps import get_tool_test_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
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


@pytest.fixture(params=[False, True], ids=["log-ok", "log-failed"])
def tool_test_diagnostic_sink(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[bool, list[tuple[BaseException, OSError]]]]:
    failures: list[tuple[BaseException, OSError]] = []
    blocker = tmp_path / "diagnostic-sink-private"
    blocker.write_text("普通文件不能作为日志目录")

    call_handlers = logging.Logger.callHandlers

    def emit(logger: logging.Logger, record: logging.LogRecord) -> None:
        call_handlers(logger, record)
        if request.param and record.name in {"app.tool_testing.store", "app.tool_testing.service"}:
            assert record.exc_info is not None and record.exc_info[1] is not None
            try:
                (blocker / "service.log").write_text(record.getMessage())
            except OSError as error:
                failures.append((record.exc_info[1], error))
                raise

    monkeypatch.setattr(logging.Logger, "callHandlers", emit)
    yield bool(request.param), failures


@pytest.fixture
def tool_test_http_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.exception_handler(HTTPException)(workspace_http_exception_handler)
    app.include_router(tools_api.router, prefix="/api/v1")
    app.dependency_overrides[verify_local_token] = lambda: "test-token"
    return app


@pytest.mark.parametrize("native", ["/private/native-failure.txt", r"C:\private\native-failure.txt", r"\\server\share\native-failure.txt"])
@pytest.mark.parametrize("stage", ["directory", "prepare", "model_build", "model_call", "execute", "tool_error", "evaluate"])
@pytest.mark.asyncio
async def test_failure_records_keep_native_details_in_diagnostics(
    tmp_path: Path, tool_test_http_app: FastAPI, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native: str, stage: str,
    tool_test_diagnostic_sink: tuple[bool, list[tuple[BaseException, OSError]]],
) -> None:
    sink_fails, sink_errors = tool_test_diagnostic_sink
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
        expected += "" if stage == "tool_error" else ", errno=13"
        if sink_fails:
            expected += ", diagnostic_sink_error=NotADirectoryError"
        assert failure == expected
        assert calls.count(stage) == 1
        assert_no_native(result.model_dump(mode="json"))
        for path in store.root.rglob("*.json"):
            assert_no_native(json.loads(path.read_text(encoding="utf-8")))
        assert ToolTestRunDTO.model_validate(ToolTestStore(root=store.root).read_run(result.run_id)) == result
        records = [record for record in caplog.records if record.name == "app.tool_testing.service"]
        record, = records
        assert record.__dict__["request_id"] == "tool-test-original-failure"
        assert record.__dict__["run_id"] == result.run_id
        assert result.run_id in caplog.text
        if stage != "directory":
            attempt, = result.attempts
            assert record.__dict__["attempt_id"] == attempt.attempt_id
            assert record.__dict__["provider_id"] == attempt.provider_id
            assert record.__dict__["case_id"] == attempt.case_id
            assert attempt.attempt_id in caplog.text
        assert record.exc_info is not None
        error = record.exc_info[1]
        if stage == "tool_error":
            assert isinstance(error, RuntimeError) and str(original) in str(error)
        elif stage == "model_call":
            assert isinstance(error, ToolCallProbeInvocationError)
            assert error.cause is original
        else:
            assert error is original
        if sink_fails:
            (primary, secondary), = sink_errors
            assert primary is error
            assert str(secondary) in "\n".join(primary.__notes__)
            assert "diagnostic-sink-private" not in json.dumps(result.model_dump(mode="json"))

        failing = False
        calls.clear()
        retried = await run("tool-test-explicit-retry")
        assert retried.run_id != result.run_id
        assert retried.status == "completed" and retried.error is None
        attempt, = retried.attempts
        assert attempt.passed and attempt.execution_succeeded and attempt.error is None
        assert attempt.model_calls == 1 and retried.providers[0].passed == 1
    await service.shutdown()


@pytest.mark.parametrize("native", ["/private/tool-test.json", r"C:\private\tool-test.json", r"\\server\share\tool-test.json"])
@pytest.mark.parametrize("listing", [False, True])
@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
@pytest.mark.asyncio
async def test_store_read_failure_uses_original_api_and_diagnostics(
    tmp_path: Path, tool_test_http_app: FastAPI, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, native: str, listing: bool, error_type: type[OSError],
    tool_test_diagnostic_sink: tuple[bool, list[tuple[BaseException, OSError]]],
) -> None:
    sink_fails, sink_errors = tool_test_diagnostic_sink
    # 此处只验查询；局部历史夹具不代替执行、fork 或恢复验收。
    workspace = tmp_path / "workspace"
    store = ToolTestStore(root=workspace / ".boxteam/tool_tests")
    directory = store.reset_tool("runtime_aware")
    now = datetime.now(UTC)
    run = ToolTestRunDTO(run_id="run_a", tool_name="runtime_aware", status="completed",
                         created_at=now, updated_at=now, repetitions=1)
    store.write_run(run.run_id, run.model_dump(mode="json"))
    record_path = directory / "run.json"
    before = record_path.read_bytes()
    service = ToolTestService(
        config_service=SingleProviderRuntimeConfig(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[]), store=store, workspace_root=workspace, asset_root=tmp_path / "assets",
    )
    tool_test_http_app.dependency_overrides[get_tool_test_service] = lambda: service
    original = error_type(2 if error_type is FileNotFoundError else 13, f"原生存储错误 {native}", native)
    read = Path.read_text

    def fail(path: Path, *args: object, **kwargs: object) -> str:
        if path == record_path:
            raise original
        return read(path, *args, **kwargs)  # type: ignore[arg-type]

    endpoint = "/api/v1/tools/tests?tool_name=runtime_aware" if listing else f"/api/v1/tools/tests/{run.run_id}"
    request_id = "tool-test-store-original-request"
    async with AsyncClient(transport=ASGITransport(app=tool_test_http_app), base_url="http://test") as client:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", fail)
            response = await client.get(endpoint, headers={"X-Request-ID": request_id})
        assert response.status_code == (404 if not listing and error_type is FileNotFoundError else 500)
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        message = response.json().get("detail", response.json().get("message"))
        assert isinstance(message, str) and native not in message and str(workspace) not in message
        if response.status_code == 404:
            assert "stage=read_run" in message and f"type={error_type.__name__}" in message
        else:
            # 未映射的500按现行TraceMiddleware只公开请求阶段；owner细节仍在同request_id诊断中。
            assert message == (
                f"请求处理失败: stage=http_request, error={error_type.__name__}, errno={original.errno}"
            )
        record, = [entry for entry in caplog.records if entry.name == "app.tool_testing.store"]
        assert record.exc_info is not None and record.exc_info[1] is original
        assert record.__dict__["request_id"] == request_id
        assert f"stage={'list_runs' if listing else 'read_run'}" in record.getMessage()
        if sink_fails:
            (primary, secondary), = sink_errors
            assert primary is original
            assert str(secondary) in "\n".join(original.__notes__)
            if response.status_code == 404:
                assert "diagnostic_sink_error=NotADirectoryError" in message
            assert "diagnostic-sink-private" not in response.text
        assert record_path.read_bytes() == before
        recovered = await client.get(endpoint)
        assert recovered.status_code == 200
        value = recovered.json()["data"]
        assert ToolTestRunDTO.model_validate(value["items"][0] if listing else value) == run
