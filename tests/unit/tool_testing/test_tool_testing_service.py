from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Annotated, Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import InjectedToolArg, tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic.errors import PydanticInvalidForJsonSchema
import pytest

from app.agents.tools.apply_patch import create_apply_patch_tool
from app.schemas.public_v2.tool_test import ToolTestStartRequest
from app.tool_testing.cases.apply_patch_case import create_apply_patch_cases
from app.tool_testing.definitions import (
    PreparedToolTest,
    ToolTestEvaluation,
    get_model_tool_parameters,
)
from app.tool_testing.model_invocation import (
    MAX_MODEL_CALLS_PER_ATTEMPT,
    MAX_TRANSIENT_RETRIES,
    ToolCallProbeInvocationError,
    is_transient_provider_error,
    probe_tool_call,
    probe_tool_call_with_transient_retries,
)
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.service import ToolTestService
from app.tool_testing.store import ToolTestStore


class _ConfigService:
    def get_agent_runtime_config(self, agent_id: str) -> dict[str, Any]:
        assert agent_id == "default"
        return {
            "providers": [
                {"id": "provider_a", "model": "model-a", "api_key": "secret-a"},
                {"id": "provider_b", "model": "model-b", "api_key": "secret-b"},
            ],
            "temperature": 0.2,
            "top_p": 1,
            "max_output_tokens": 1000,
        }


def test_apply_patch_defaults_to_ten_distinct_cases_and_one_repetition() -> None:
    cases = ToolTestRegistry().cases_for("apply_patch")

    assert ToolTestStartRequest().repetitions == 1
    assert len(cases) == 10
    assert len({case.case_id for case in cases}) == 10


def test_transient_provider_error_classification_uses_http_status() -> None:
    class _ProviderHttpError(RuntimeError):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    assert is_transient_provider_error(_ProviderHttpError(503)) is True
    assert is_transient_provider_error(_ProviderHttpError(429)) is True
    assert is_transient_provider_error(_ProviderHttpError(400)) is False


class _BoundModel:
    def __init__(self, provider_id: str, concurrency: dict[str, int]) -> None:
        self._provider_id = provider_id
        self._concurrency = concurrency

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self._concurrency[self._provider_id] += 1
        self._concurrency["active"] += 1
        self._concurrency["max_active"] = max(
            self._concurrency["max_active"],
            self._concurrency["active"],
        )
        self._concurrency[f"max:{self._provider_id}"] = max(
            self._concurrency[f"max:{self._provider_id}"],
            self._concurrency[self._provider_id],
        )
        try:
            await asyncio.sleep(0.02)
            prompt = str(getattr(messages[-1], "content", ""))
            match = re.search(r"(target\.txt)", prompt)
            assert match is not None
            patch = (
                "*** Begin Patch\n"
                f"*** Update File: {match.group(1)}\n"
                "@@\n"
                " alpha\n"
                "-beta\n"
                "+gamma\n"
                "+delta\n"
                "*** End Patch"
            )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "apply_patch",
                        "args": {"input": patch, "explanation": "测试补丁"},
                        "id": f"call-{self._provider_id}",
                        "type": "tool_call",
                    }
                ],
            )
        finally:
            self._concurrency[self._provider_id] -= 1
            self._concurrency["active"] -= 1


class _Model:
    def __init__(self, provider_id: str, concurrency: dict[str, int]) -> None:
        self._provider_id = provider_id
        self._concurrency = concurrency

    def bind_tools(self, tools: list[object]) -> _BoundModel:
        assert len(tools) == 1
        return _BoundModel(self._provider_id, self._concurrency)


@dataclass(frozen=True, slots=True)
class _InjectedArgumentCase:
    case_id: str = "runtime_injected_argument"
    tool_name: str = "runtime_aware"
    title: str = "隐藏运行时参数"

    def prepare(
        self,
        *,
        workspace_root: Path,
        attempt_root: Path,
        asset_root: Path,
    ) -> PreparedToolTest:
        del workspace_root, asset_root
        attempt_root.mkdir(parents=True)

        @tool
        def runtime_aware(
            value: str,
            callback: Annotated[Callable[[], str], InjectedToolArg],
        ) -> str:
            """调用由后端注入的回调并拼接模型提供的公开参数。"""
            return f"{callback()}:{value}"

        return PreparedToolTest(
            prompt="调用 runtime_aware，并将 value 设为 public。",
            tool=runtime_aware,
            injected_arguments={"callback": lambda: "injected"},
        )

    def evaluate(
        self,
        *,
        attempt_root: Path,
        tool_result: object,
    ) -> ToolTestEvaluation:
        del attempt_root
        return ToolTestEvaluation(
            passed=tool_result == "injected:public",
            detail=f"tool_result={tool_result!r}",
        )


class _InjectedArgumentBoundModel:
    async def ainvoke(self, messages: list[object]) -> AIMessage:
        del messages
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "runtime_aware",
                    "args": {"value": "public"},
                    "id": "call-runtime-aware",
                    "type": "tool_call",
                }
            ],
        )


class _InjectedArgumentModel:
    def __init__(self) -> None:
        self.bound_parameters: dict[str, Any] | None = None

    def bind_tools(self, tools: list[object]) -> _InjectedArgumentBoundModel:
        assert len(tools) == 1
        candidate = tools[0]
        self.bound_parameters = get_model_tool_parameters(candidate)  # type: ignore[arg-type]
        return _InjectedArgumentBoundModel()


class _SingleProviderConfigService:
    def get_agent_runtime_config(self, agent_id: str) -> dict[str, Any]:
        assert agent_id == "default"
        return {
            "providers": [
                {"id": "provider_only", "model": "model-only", "api_key": "secret"}
            ]
        }


@pytest.mark.asyncio
async def test_tool_test_uses_public_schema_and_injects_runtime_only_at_execution(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    store = ToolTestStore(root=workspace_root / ".boxteam" / "tool_tests")
    model = _InjectedArgumentModel()
    case = _InjectedArgumentCase()
    prepared = case.prepare(
        workspace_root=workspace_root,
        attempt_root=tmp_path / "schema-check",
        asset_root=tmp_path / "assets",
    )
    with pytest.raises(PydanticInvalidForJsonSchema):
        prepared.tool.args_schema.model_json_schema()

    service = ToolTestService(
        config_service=_SingleProviderConfigService(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[case]),
        store=store,
        workspace_root=workspace_root,
        asset_root=tmp_path / "assets",
        model_builder=lambda **_: model,
    )
    started = await service.start(
        tool_name=case.tool_name,
        request=ToolTestStartRequest(),
    )
    for _ in range(100):
        result = service.get(started.run_id)
        if result.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("隐藏参数工具测试没有在预期时间内结束")

    assert result.status == "completed"
    assert result.attempts[0].passed is True
    expected_parameters = {
        "value": {"title": "Value", "type": "string"},
    }
    assert model.bound_parameters is not None
    assert model.bound_parameters["properties"] == expected_parameters
    request_path = (
        store.case_dir(case.tool_name, "provider_only", case.case_id) / "request.json"
    )
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert request_payload["tool"]["parameters"]["properties"] == expected_parameters
    assert "callback" not in request_path.read_text(encoding="utf-8")


def test_model_tool_parameters_reports_missing_public_schema() -> None:
    class _ToolWithoutPublicSchema:
        name = "missing_schema"
        tool_call_schema = None

    with pytest.raises(
        TypeError,
        match=(
            "tool_name=missing_schema "
            "tool_type=_ToolWithoutPublicSchema"
        ),
    ):
        get_model_tool_parameters(_ToolWithoutPublicSchema())  # type: ignore[arg-type]


def test_model_tool_parameters_excludes_tool_runtime() -> None:
    @tool
    def runtime_tool(value: str, runtime: ToolRuntime) -> str:
        """返回公开值，运行时对象只由 LangGraph 注入。"""
        del runtime
        return value

    with pytest.raises(PydanticInvalidForJsonSchema):
        runtime_tool.args_schema.model_json_schema()

    parameters = get_model_tool_parameters(runtime_tool)

    assert parameters["properties"] == {
        "value": {"title": "Value", "type": "string"},
    }
    assert parameters["required"] == ["value"]


@pytest.mark.asyncio
async def test_tool_tests_run_models_concurrently_and_each_model_serially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    asset_root = tmp_path / "assets"
    seed_root = asset_root / "apply_patch" / "update"
    seed_root.mkdir(parents=True)
    (seed_root / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    concurrency = {
        "provider_a": 0,
        "provider_b": 0,
        "active": 0,
        "max_active": 0,
        "max:provider_a": 0,
        "max:provider_b": 0,
    }

    def build_model(*, provider: dict[str, Any], runtime_config: dict[str, Any]) -> _Model:
        assert runtime_config["temperature"] == 0.2
        return _Model(str(provider["id"]), concurrency)

    store = ToolTestStore(root=workspace_root / ".boxteam" / "tool_tests")
    service = ToolTestService(
        config_service=_ConfigService(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[create_apply_patch_cases()[0]]),
        store=store,
        workspace_root=workspace_root,
        asset_root=asset_root,
        model_builder=build_model,
    )
    started = await service.start(
        tool_name="apply_patch",
        request=ToolTestStartRequest(repetitions=2),
    )
    for _ in range(100):
        result = service.get(started.run_id)
        if result.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("工具测试没有在预期时间内结束")

    assert result.status == "completed"
    assert result.progress == 100
    assert len(result.attempts) == 4
    assert all(attempt.passed for attempt in result.attempts)
    assert all(attempt.model_calls == 1 for attempt in result.attempts)
    assert all(attempt.reasoning_only_calls == 0 for attempt in result.attempts)
    assert all(provider.success_rate == 100 for provider in result.providers)
    assert all(provider.model_calls == 2 for provider in result.providers)
    assert all(provider.reasoning_only_calls == 0 for provider in result.providers)
    assert concurrency["max_active"] >= 2
    assert concurrency["max:provider_a"] == 1
    assert concurrency["max:provider_b"] == 1
    assert (store.tool_dir("apply_patch") / "run.json").is_file()
    case_dir = store.case_dir(
        "apply_patch",
        "provider_a",
        "apply_patch_update_single_file",
    )
    assert (case_dir / "request.json").is_file()
    assert (case_dir / "response.json").is_file()
    assert (case_dir / "result.json").is_file()
    response_payload = json.loads(
        (case_dir / "response.json").read_text(encoding="utf-8")
    )
    request_payload = json.loads(
        (case_dir / "request.json").read_text(encoding="utf-8")
    )
    assert request_payload["provider"]["api_key"] == "***REDACTED***"
    assert request_payload["messages"][-1]["content"].startswith(
        "必须真实调用一次 apply_patch"
    )
    assert response_payload["calls"][0]["response"]["tool_calls"][0]["name"] == (
        "apply_patch"
    )


class _ScriptedBoundModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = responses
        self.calls = 0
        self.received_messages: list[list[object]] = []

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.received_messages.append(list(messages))
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _ScriptedModel:
    def __init__(self, bound_model: _ScriptedBoundModel) -> None:
        self._bound_model = bound_model

    def bind_tools(self, tools: list[object]) -> _ScriptedBoundModel:
        assert len(tools) == 1
        return self._bound_model


def _tool_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "apply_patch",
                "args": {"input": "*** Begin Patch\n*** End Patch"},
                "id": "call-scripted",
                "type": "tool_call",
            }
        ],
    )


@pytest.mark.asyncio
async def test_probe_tool_call_continues_after_reasoning_only_response(
    tmp_path: Path,
) -> None:
    bound_model = _ScriptedBoundModel(
        [
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "reasoning": "先确认目标文件和补丁上下文。",
                    }
                ]
            ),
            _tool_call_message(),
        ]
    )
    result = await probe_tool_call(
        model=_ScriptedModel(bound_model),
        tool=create_apply_patch_tool(workspace_root=tmp_path),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is not None
    assert result.model_calls == 2
    assert result.reasoning_only_calls == 1
    assert bound_model.calls == 2
    assert len(bound_model.received_messages[1]) == 4


@pytest.mark.asyncio
async def test_probe_tool_call_does_not_retry_plain_text_response(tmp_path: Path) -> None:
    bound_model = _ScriptedBoundModel([AIMessage(content="我将使用工具修改文件。")])
    result = await probe_tool_call(
        model=_ScriptedModel(bound_model),
        tool=create_apply_patch_tool(workspace_root=tmp_path),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is None
    assert result.model_calls == 1
    assert result.reasoning_only_calls == 0
    assert result.failure is not None and "不是纯 reasoning" in result.failure
    assert bound_model.calls == 1


@pytest.mark.asyncio
async def test_probe_tool_call_limits_consecutive_reasoning_responses(
    tmp_path: Path,
) -> None:
    reasoning_response = AIMessage(
        content=[{"type": "reasoning", "reasoning": "继续分析补丁格式。"}]
    )
    bound_model = _ScriptedBoundModel(
        [reasoning_response] * MAX_MODEL_CALLS_PER_ATTEMPT
    )
    result = await probe_tool_call(
        model=_ScriptedModel(bound_model),
        tool=create_apply_patch_tool(workspace_root=tmp_path),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
    )

    assert result.tool_call is None
    assert result.model_calls == MAX_MODEL_CALLS_PER_ATTEMPT
    assert result.reasoning_only_calls == MAX_MODEL_CALLS_PER_ATTEMPT
    assert result.failure is not None and "连续 3 次" in result.failure
    assert bound_model.calls == MAX_MODEL_CALLS_PER_ATTEMPT


@pytest.mark.asyncio
async def test_probe_tool_call_reports_invocation_count_on_provider_error(
    tmp_path: Path,
) -> None:
    class _FailingBoundModel(_ScriptedBoundModel):
        def __init__(self) -> None:
            super().__init__([])

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            raise ConnectionError("上游连接中断")

    failing_model = _ScriptedModel(_FailingBoundModel())
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call(
            model=failing_model,
            tool=create_apply_patch_tool(workspace_root=tmp_path),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
        )

    assert captured.value.model_calls == 1
    assert captured.value.reasoning_only_calls == 0
    assert "ConnectionError: 上游连接中断" in str(captured.value)


@pytest.mark.asyncio
async def test_probe_tool_call_retries_transient_provider_error_three_times(
    tmp_path: Path,
) -> None:
    class _FlakyBoundModel(_ScriptedBoundModel):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.calls += 1
            if self.calls <= MAX_TRANSIENT_RETRIES:
                raise ConnectionError("上游连接中断")
            return _tool_call_message()

    bound_model = _FlakyBoundModel()
    result = await probe_tool_call_with_transient_retries(
        model=_ScriptedModel(bound_model),
        tool=create_apply_patch_tool(workspace_root=tmp_path),
        messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
        retry_delays=(0, 0, 0),
    )

    assert result.tool_call is not None
    assert result.model_calls == 4
    assert result.transient_retries == 3
    assert bound_model.calls == 4


@pytest.mark.asyncio
async def test_probe_tool_call_does_not_retry_non_transient_provider_error(
    tmp_path: Path,
) -> None:
    class _InvalidRequestBoundModel(_ScriptedBoundModel):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        async def ainvoke(self, messages: list[object]) -> AIMessage:
            self.calls += 1
            raise ValueError("请求参数无效")

    bound_model = _InvalidRequestBoundModel()
    with pytest.raises(ToolCallProbeInvocationError) as captured:
        await probe_tool_call_with_transient_retries(
            model=_ScriptedModel(bound_model),
            tool=create_apply_patch_tool(workspace_root=tmp_path),
            messages=[SystemMessage("测试"), HumanMessage("请修改文件")],
            retry_delays=(0, 0, 0),
        )

    assert captured.value.model_calls == 1
    assert captured.value.transient_retries == 0
    assert bound_model.calls == 1


def test_tool_test_store_lists_readable_run_files(tmp_path: Path) -> None:
    store = ToolTestStore(root=tmp_path / "tool_tests")
    store.reset_tool("apply_patch")
    store.write_run("run_a", {"run_id": "run_a", "tool_name": "apply_patch", "created_at": "2"})
    store.reset_tool("edit_file")
    store.write_run("run_b", {"run_id": "run_b", "tool_name": "edit_file", "created_at": "1"})

    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == ["run_a"]

    store.reset_tool("apply_patch")
    store.write_run("run_c", {"run_id": "run_c", "tool_name": "apply_patch", "created_at": "3"})
    with pytest.raises(FileNotFoundError, match="已被新测试覆盖"):
        store.read_run("run_a")
    assert [item["run_id"] for item in store.list_runs(tool_name="apply_patch")] == ["run_c"]
