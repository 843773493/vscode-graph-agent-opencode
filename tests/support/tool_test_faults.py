"""工具测试失败注入共用的最小模型与工具；用于核对原异常身份，不实现 SSE 协议。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from langchain_core.messages import AIMessage
from langchain_core.tools import InjectedToolArg, tool

from app.tool_testing.definitions import (
    PreparedToolTest,
    ToolTestEvaluation,
    get_model_tool_parameters,
)


@dataclass(frozen=True, slots=True)
class RuntimeInjectedArgumentCase:
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


class RuntimeInjectedArgumentBoundModel:
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


class RuntimeInjectedArgumentModel:
    def __init__(self) -> None:
        self.bound_parameters: dict[str, Any] | None = None

    def bind_tools(self, tools: list[object]) -> RuntimeInjectedArgumentBoundModel:
        assert len(tools) == 1
        candidate = tools[0]
        self.bound_parameters = get_model_tool_parameters(candidate)  # type: ignore[arg-type]
        return RuntimeInjectedArgumentBoundModel()


class SingleProviderRuntimeConfig:
    def get_agent_runtime_config(self, agent_id: str) -> dict[str, Any]:
        assert agent_id == "default"
        return {
            "providers": [
                {"id": "provider_only", "model": "model-only", "api_key": "secret"}
            ]
        }
