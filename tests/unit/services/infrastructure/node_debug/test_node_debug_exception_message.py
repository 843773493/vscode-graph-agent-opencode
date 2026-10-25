"""Node Inspector 运行时表达式异常内容的评价映射测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.internal_v2.node_debug import (
    NodeDebugEvaluateParams,
    NodeDebugStackFrameDTO,
)
from app.services.infrastructure.node_debug.process.evaluation import (
    NodeDebugEvaluation,
)
from app.services.infrastructure.node_debug.process.inspector import (
    NodeDebugInspector,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from tests.support.node_debug_dependencies import TEST_THREAD_ID, node_debug_file_paths

REAL_CDP_RESPONSE = (
    '{"id":3,"result":{"result":{"type":"string","value":"token xyz"},'
    '"exceptionDetails":{"exceptionId":1,"text":"Uncaught",'
    '"lineNumber":0,"columnNumber":9,"scriptId":"84",'
    '"exception":{"type":"string","value":"token xyz"}}}}'
)


@pytest.fixture
def inspector_runtime(tmp_path: Path) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id="ses_019bf141964978208ce9100774167f90",
        thread_id=TEST_THREAD_ID,
        configuration_id="dbgcfg_" + "1" * 32,
        workspace_root=tmp_path,
        script_path=tmp_path / "entry.mjs",
        relative_script_path="entry.mjs",
        status="paused",
    )
    runtime.call_stack = [
        NodeDebugStackFrameDTO(
            call_frame_id="3011414141483895718.1.0",
            function_name="",
            url="",
            line=1,
            column=1,
        )
    ]
    return runtime


@pytest.fixture
def inspector(tmp_path: Path) -> NodeDebugInspector:
    return NodeDebugInspector(
        workspace_root=tmp_path,
        file_paths=node_debug_file_paths(tmp_path),
        append_action=lambda *args, **kwargs: None,
        clear_stop_snapshot=lambda runtime: None,
    )


@pytest.fixture
def evaluation(inspector: NodeDebugInspector) -> NodeDebugEvaluation:
    return NodeDebugEvaluation(
        inspector=inspector,
        append_action=lambda *args, **kwargs: None,
    )


def _respond_with(
    monkeypatch: pytest.MonkeyPatch,
    inspector: NodeDebugInspector,
    response: dict[str, object],
) -> None:
    async def command(
        runtime: NodeDebugRuntime,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del runtime, params
        assert method == "Debugger.evaluateOnCallFrame"
        return response

    monkeypatch.setattr(inspector, "command", command)


async def _evaluate(
    evaluation: NodeDebugEvaluation,
    runtime: NodeDebugRuntime,
    expression: str,
) -> None:
    await evaluation.evaluate(
        runtime,
        NodeDebugEvaluateParams(expression=expression),
        actor="human",
        tool_name=None,
        tool_call_id=None,
    )


@pytest.mark.asyncio
async def test_evaluate_preserves_primitive_exception_from_real_cdp_response(
    evaluation: NodeDebugEvaluation,
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实CDP throw字符串的value必须出现在评价错误内容中。"""
    response = json.loads(REAL_CDP_RESPONSE)["result"]
    assert isinstance(response, dict)
    _respond_with(monkeypatch, inspector, response)

    await _evaluate(evaluation, inspector_runtime, "(() => { throw 'token xyz'; })()")

    assert inspector_runtime.last_evaluation is not None
    assert inspector_runtime.last_evaluation.value == '"token xyz"'
    assert inspector_runtime.last_evaluation.error == '"token xyz"'


@pytest.mark.asyncio
async def test_evaluate_prefers_error_description_over_exception_text(
    evaluation: NodeDebugEvaluation,
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留既有Error.description优先级。"""
    _respond_with(
        monkeypatch,
        inspector,
        {
            "result": {"type": "object"},
            "exceptionDetails": {
                "text": "Uncaught",
                "exception": {"type": "object", "description": "TypeError: boom"},
            },
        },
    )

    await _evaluate(evaluation, inspector_runtime, "throw new TypeError('boom')")

    assert inspector_runtime.last_evaluation is not None
    assert inspector_runtime.last_evaluation.error == "TypeError: boom"


@pytest.mark.asyncio
async def test_evaluate_uses_exception_text_when_remote_value_is_absent(
    evaluation: NodeDebugEvaluation,
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _respond_with(
        monkeypatch,
        inspector,
        {"result": {}, "exceptionDetails": {"text": "SyntaxError: bad token"}},
    )

    await _evaluate(evaluation, inspector_runtime, "invalid syntax")

    assert inspector_runtime.last_evaluation is not None
    assert inspector_runtime.last_evaluation.error == "SyntaxError: bad token"


@pytest.mark.asyncio
async def test_evaluate_keeps_generic_message_when_exception_has_no_details(
    evaluation: NodeDebugEvaluation,
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _respond_with(
        monkeypatch,
        inspector,
        {"result": {}, "exceptionDetails": {}},
    )

    await _evaluate(evaluation, inspector_runtime, "throw undefined")

    assert inspector_runtime.last_evaluation is not None
    assert inspector_runtime.last_evaluation.error == "表达式求值失败"


@pytest.mark.asyncio
async def test_evaluate_preserves_non_exception_result_contract(
    evaluation: NodeDebugEvaluation,
    inspector: NodeDebugInspector,
    inspector_runtime: NodeDebugRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _respond_with(monkeypatch, inspector, {"result": {"type": "number", "value": 7}})

    await _evaluate(evaluation, inspector_runtime, "6 + 1")

    assert inspector_runtime.last_evaluation is not None
    assert inspector_runtime.last_evaluation.value == "7"
    assert inspector_runtime.last_evaluation.type == "number"
    assert inspector_runtime.last_evaluation.error is None
