"""Inspector 源码引用在构造及复制后发布时仍须拒绝原生位置。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from app.agents.tools.debugging.payloads import _success
from app.schemas.internal_v2.node_debug import NodeDebugStackFrameDTO, NodeDebugStateDTO

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
        call_stack=[frame], last_stopped_frame=frame.model_copy(deep=True),
    )


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
