from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import InjectedToolArg, tool
from pydantic.errors import PydanticInvalidForJsonSchema

import app.tool_testing.service as tool_test_service_module
from app.schemas.internal_v2.tool_test import ToolTestRunDTO, ToolTestStartRequest
from app.tool_testing.cases.apply_patch_case import create_apply_patch_cases
from app.tool_testing.definitions import (
    PreparedToolTest,
    ToolTestEvaluation,
    get_model_tool_parameters,
)
from app.tool_testing.registry import ToolTestRegistry
from app.tool_testing.service import ToolTestService
from app.tool_testing.store import ToolTestStore


class _TwoProviderConfigService:
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


class _ConcurrentApplyPatchBoundModel:
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
                        "args": {
                            "input": patch,
                            "explanation": "测试补丁",
                            "expected_versions": json.loads(
                                re.search(r"expected_versions = (.+)", prompt).group(1)
                            ),
                        },
                        "id": f"call-{self._provider_id}",
                        "type": "tool_call",
                    }
                ],
            )
        finally:
            self._concurrency[self._provider_id] -= 1
            self._concurrency["active"] -= 1


class _ConcurrentApplyPatchModel:
    def __init__(self, provider_id: str, concurrency: dict[str, int]) -> None:
        self._provider_id = provider_id
        self._concurrency = concurrency

    def bind_tools(self, tools: list[object]) -> _ConcurrentApplyPatchBoundModel:
        assert len(tools) == 1
        return _ConcurrentApplyPatchBoundModel(self._provider_id, self._concurrency)


@pytest.fixture
def apply_patch_test_roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    asset_root = tmp_path / "assets"
    seed_root = asset_root / "apply_patch" / "update"
    seed_root.mkdir(parents=True)
    (seed_root / "target.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return workspace_root, asset_root


@dataclass(frozen=True, slots=True)
class _RuntimeInjectedArgumentCase:
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


class _RuntimeInjectedArgumentBoundModel:
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


class _RuntimeInjectedArgumentModel:
    def __init__(self) -> None:
        self.bound_parameters: dict[str, Any] | None = None

    def bind_tools(self, tools: list[object]) -> _RuntimeInjectedArgumentBoundModel:
        assert len(tools) == 1
        candidate = tools[0]
        self.bound_parameters = get_model_tool_parameters(candidate)  # type: ignore[arg-type]
        return _RuntimeInjectedArgumentBoundModel()


class _SingleProviderRuntimeConfig:
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
    model = _RuntimeInjectedArgumentModel()
    case = _RuntimeInjectedArgumentCase()
    prepared = case.prepare(
        workspace_root=workspace_root,
        attempt_root=tmp_path / "schema-check",
        asset_root=tmp_path / "assets",
    )
    with pytest.raises(PydanticInvalidForJsonSchema):
        prepared.tool.args_schema.model_json_schema()

    service = ToolTestService(
        config_service=_SingleProviderRuntimeConfig(),  # type: ignore[arg-type]
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


@pytest.mark.asyncio
async def test_tool_tests_run_models_concurrently_and_each_model_serially(
    apply_patch_test_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root, asset_root = apply_patch_test_roots
    concurrency = {
        "provider_a": 0,
        "provider_b": 0,
        "active": 0,
        "max_active": 0,
        "max:provider_a": 0,
        "max:provider_b": 0,
    }
    model_builds = {"provider_a": 0, "provider_b": 0}
    redacted_provider_count = 0
    redact_secrets = tool_test_service_module._redact_secrets

    def count_provider_redaction(value: object) -> object:
        nonlocal redacted_provider_count
        if isinstance(value, dict) and value.get("id") in {
            "provider_a",
            "provider_b",
        }:
            redacted_provider_count += 1
        return redact_secrets(value)

    monkeypatch.setattr(
        tool_test_service_module, "_redact_secrets", count_provider_redaction
    )

    def build_model(
        *, provider: dict[str, Any], runtime_config: dict[str, Any]
    ) -> _ConcurrentApplyPatchModel:
        assert runtime_config["temperature"] == 0.2
        provider_id = str(provider["id"])
        model_builds[provider_id] += 1
        return _ConcurrentApplyPatchModel(provider_id, concurrency)

    store = ToolTestStore(root=workspace_root / ".boxteam" / "tool_tests")
    read_run_calls = 0
    read_run = store.read_run

    def count_run_reads(run_id: str) -> dict[str, Any]:
        nonlocal read_run_calls
        read_run_calls += 1
        return read_run(run_id)

    monkeypatch.setattr(store, "read_run", count_run_reads)
    service = ToolTestService(
        config_service=_TwoProviderConfigService(),  # type: ignore[arg-type]
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
    assert model_builds == {"provider_a": 1, "provider_b": 1}
    assert redacted_provider_count == 2
    assert read_run_calls <= 1
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


@pytest.mark.asyncio
async def test_active_run_cache_serves_copies_and_recovers_after_write_failure(
    apply_patch_test_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root, asset_root = apply_patch_test_roots
    store = ToolTestStore(root=workspace_root / ".boxteam" / "tool_tests")
    store.reset_tool("apply_patch")
    now = datetime.now(UTC)
    run = ToolTestRunDTO(
        run_id="tooltest_write_failure",
        tool_name="apply_patch",
        status="running",
        created_at=now,
        updated_at=now,
        repetitions=1,
    )
    store.write_run(run.run_id, run.model_dump(mode="json"))
    service = ToolTestService(
        config_service=_SingleProviderRuntimeConfig(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=create_apply_patch_cases()[:1]),
        store=store,
        workspace_root=workspace_root,
        asset_root=asset_root,
    )
    service._active_run_states[run.run_id] = run.model_copy(deep=True)

    persisted_read_run = store.read_run
    read_run_calls = 0

    def count_run_reads(run_id: str) -> dict[str, Any]:
        nonlocal read_run_calls
        read_run_calls += 1
        return persisted_read_run(run_id)

    monkeypatch.setattr(store, "read_run", count_run_reads)

    returned_run = service.get(run.run_id)
    returned_run.progress = 90

    assert service.get(run.run_id).progress == 0
    assert read_run_calls == 0

    write_run = store.write_run
    write_attempts = 0

    def fail_first_write(run_id: str, payload: dict[str, Any]) -> None:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise OSError("模拟 run 状态写入失败")
        write_run(run_id, payload)

    monkeypatch.setattr(store, "write_run", fail_first_write)

    def increment_progress(state: ToolTestRunDTO) -> None:
        state.progress += 1

    with pytest.raises(OSError, match="模拟 run 状态写入失败"):
        await service._mutate_run(run.run_id, increment_progress)

    assert run.run_id not in service._active_run_states
    assert service.get(run.run_id).progress == 0
    assert read_run_calls == 1

    await service._mutate_run(run.run_id, increment_progress)

    assert service.get(run.run_id).progress == 1
    assert read_run_calls == 2
    assert persisted_read_run(run.run_id)["progress"] == 1


@pytest.mark.asyncio
async def test_failed_model_construction_is_retried_on_next_attempt(
    apply_patch_test_roots: tuple[Path, Path],
) -> None:
    workspace_root, asset_root = apply_patch_test_roots
    concurrency = {
        "provider_a": 0,
        "active": 0,
        "max_active": 0,
        "max:provider_a": 0,
    }
    model_builds = 0

    def build_model(
        *, provider: dict[str, Any], runtime_config: dict[str, Any]
    ) -> _ConcurrentApplyPatchModel:
        nonlocal model_builds
        assert runtime_config["temperature"] == 0.2
        model_builds += 1
        if model_builds == 1:
            raise RuntimeError("模型客户端暂时不可用")
        return _ConcurrentApplyPatchModel(str(provider["id"]), concurrency)

    store = ToolTestStore(root=workspace_root / ".boxteam" / "tool_tests")
    service = ToolTestService(
        config_service=_TwoProviderConfigService(),  # type: ignore[arg-type]
        registry=ToolTestRegistry(cases=[create_apply_patch_cases()[0]]),
        store=store,
        workspace_root=workspace_root,
        asset_root=asset_root,
        model_builder=build_model,
    )
    started = await service.start(
        tool_name="apply_patch",
        request=ToolTestStartRequest(provider_ids=["provider_a"], repetitions=2),
    )
    for _ in range(100):
        result = service.get(started.run_id)
        if result.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("工具测试没有在预期时间内结束")

    assert result.status == "completed"
    assert model_builds == 2
    assert [attempt.status for attempt in result.attempts] == ["failed", "completed"]
    assert result.attempts[0].error == "RuntimeError: 模型客户端暂时不可用"
    assert result.attempts[1].passed is True
