"""Inspector 源码引用在构造及复制后发布时仍须拒绝原生位置。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from app.agents.tools.debugging.payloads import _success
from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugConfigurationSummaryDTO,
    NodeDebugStackFrameDTO,
    NodeDebugStateDTO,
)

FILE = "boxteam://workspace/22222222-2222-4222-8222-222222222222/resources/files/root/entry.mjs"
EXTERNAL = "boxteam://gateway/gateway_" + "a" * 32 + "/resources/files/00000000000070008000000000000002/entry.mjs"
NATIVE = (
    "/private/inspector-secret.mjs", r"C:\private\inspector-secret.mjs",
    r"\\target\private\inspector-secret.mjs", "file:///private/inspector-secret.mjs",
)
Mode = Literal["python", "json", "json-text", "tool"]


@pytest.fixture
def state() -> NodeDebugStateDTO:
    frame = NodeDebugStackFrameDTO(
        call_frame_id="frame", function_name="f", url=FILE, path="entry.mjs", line=2, column=3,
    )
    return NodeDebugStateDTO(
        session_id="ses_019b9dffc29a779e8564d196f78bb0a6",
        thread_id="thr_01a100c4a1897a2d8d40b568d23fcbe6", status="paused",
        script_path="src/entry.mjs", working_directory="src",
        source_changed_paths=["src/entry.mjs"],
        breakpoints=[NodeDebugBreakpointDTO(
            breakpoint_id="bp", path="src/entry.mjs", line=2,
            created_at=datetime(2026, 11, 2, tzinfo=UTC),
        )],
        configurations=[NodeDebugConfigurationSummaryDTO(
            configuration_id="cfg", name="方案", script_path="src/entry.mjs",
            updated_at=datetime(2026, 11, 2, tzinfo=UTC),
        )],
        call_stack=[frame], last_stopped_frame=frame.model_copy(deep=True),
    )


@pytest.mark.parametrize("field", ["script_path", "working_directory", "source_changed_paths",
                                 "breakpoints", "configurations"])
@pytest.mark.parametrize("native", NATIVE)
def test_state_construction_refuses_native_source(
    state: NodeDebugStateDTO, field: str, native: str,
) -> None:
    payload = state.model_dump()
    if field == "breakpoints":
        payload[field] = [{**state.breakpoints[0].model_dump(), "path": native}]
    elif field == "configurations":
        payload[field] = [{**state.configurations[0].model_dump(), "script_path": native}]
    else:
        payload[field] = [native] if field == "source_changed_paths" else native
    with pytest.raises(ValidationError):
        NodeDebugStateDTO.model_validate(payload)


@pytest.mark.parametrize("field", ["script_path", "working_directory", "source_changed_paths",
                                 "breakpoints", "configurations"])
@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("mode", ["python", "json", "json-text", "tool"])
def test_mutated_state_refuses_publication(
    state: NodeDebugStateDTO, field: str, native: str, mode: Mode,
) -> None:
    before = state.model_dump_json()
    damaged = state.model_copy(deep=True)
    if field == "breakpoints":
        damaged.breakpoints[0] = damaged.breakpoints[0].model_copy(update={"path": native})
    elif field == "configurations":
        damaged.configurations[0] = damaged.configurations[0].model_copy(update={"script_path": native})
    else:
        damaged = damaged.model_copy(update={field: [native] if field == "source_changed_paths" else native})
    with pytest.raises(PydanticSerializationError) as failure:
        if mode == "tool":
            _success("调试状态", damaged)
        elif mode == "json-text":
            damaged.model_dump_json()
        else:
            damaged.model_dump(mode=mode)
    assert native not in str(failure.value)
    assert state.model_dump_json() == before


def test_state_relative_paths_keep_wire_and_nullable_root(state: NodeDebugStateDTO) -> None:
    payload = state.model_dump(mode="json")
    assert NodeDebugStateDTO.model_validate_json(state.model_dump_json()).model_dump(mode="json") == payload
    assert payload["script_path"] == payload["source_changed_paths"][0] == "src/entry.mjs"
    assert payload["breakpoints"][0]["path"] == payload["configurations"][0]["script_path"] == "src/entry.mjs"
    assert payload["working_directory"] == "src"
    for directory in (None, "", "中文 子目录/100%"):
        copy = state.model_copy(update={"working_directory": directory, "script_path": None})
        assert NodeDebugStateDTO.model_validate_json(copy.model_dump_json()) == copy


@pytest.mark.parametrize("field", ["url", "path"])
@pytest.mark.parametrize("native", NATIVE)
def test_frame_construction_refuses_native_source(field: str, native: str) -> None:
    payload = {"call_frame_id": "frame", "function_name": "f", "url": FILE,
               "path": "entry.mjs", "line": 2, "column": 3, field: native}
    with pytest.raises(ValidationError):
        NodeDebugStackFrameDTO.model_validate(payload)


@pytest.mark.parametrize("field", ["url", "path"])
@pytest.mark.parametrize("native", NATIVE)
@pytest.mark.parametrize("container", ["frame", "call_stack", "last_stopped_frame"])
@pytest.mark.parametrize("mode", ["python", "json", "json-text", "tool"])
def test_mutated_frame_refuses_publication(
    state: NodeDebugStateDTO, field: str, native: str, container: str, mode: Mode,
) -> None:
    before = state.model_dump_json()
    damaged = state.model_copy(deep=True)
    frame = damaged.call_stack[0].model_copy(update={field: native})
    published: BaseModel = damaged
    if container == "frame":
        published = frame
    elif container == "call_stack":
        damaged.call_stack[0] = frame
    else:
        damaged.last_stopped_frame = frame
    with pytest.raises(PydanticSerializationError) as failure:
        if mode == "tool":
            # 工具复用同一 State 序列化入口，不能在模型响应里绕过检查。
            if container == "frame":
                damaged.call_stack[0] = frame
            _success("调用栈", damaged)
        elif mode == "json-text":
            published.model_dump_json()
        else:
            published.model_dump(mode=mode)
    assert native not in str(failure.value)
    assert state.model_dump_json() == before


@pytest.mark.parametrize("url", [FILE, EXTERNAL, "node:internal/modules/run_main", "evalmachine.<anonymous>",
                                 "https://example.test/source.js", "webpack:///src/entry.mjs", "", "u",
                                 FILE.replace("22222222-2222-4222-8222-222222222222", "unavailable")])
def test_source_uris_and_nonfile_inspector_labels_keep_wire(state: NodeDebugStateDTO, url: str) -> None:
    frame = state.call_stack[0].model_copy(update={"url": url})
    assert NodeDebugStackFrameDTO.model_validate_json(frame.model_dump_json()).url == url
    assert frame.model_dump(mode="python")["url"] == url
    assert frame.model_dump(mode="json")["url"] == url


def test_path_remains_optional_and_relative(state: NodeDebugStateDTO) -> None:
    for path in (None, "entry.mjs", "中文 子目录/100%.mjs", "file:entry.mjs", "C:entry.mjs"):
        frame = state.call_stack[0].model_copy(update={"path": path})
        assert NodeDebugStackFrameDTO.model_validate_json(frame.model_dump_json()).path == path


@pytest.mark.parametrize("url", [
    "boxteam://workspace//resources/files/root/entry.mjs",
    FILE.replace("/files/root/entry.mjs", "/config/workspace_inline"),
    FILE + "?revision=1", FILE + "#L1", "FILE:///private/inspector-secret.mjs",
])
def test_source_url_rejects_wrong_domain_or_noncanonical_address(state: NodeDebugStateDTO, url: str) -> None:
    with pytest.raises(ValidationError):
        NodeDebugStackFrameDTO.model_validate({**state.call_stack[0].model_dump(), "url": url})
    with pytest.raises(PydanticSerializationError):
        state.call_stack[0].model_copy(update={"url": url}).model_dump_json()
