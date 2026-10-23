from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support.e2e_control import harness as e2e_test_control


@pytest.fixture(params=["cp1252", "gbk"])
def control_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> Iterator[e2e_test_control.E2ETestControlHarness]:
    """只替换文本文件的默认编码和传输生命周期，仍读写真实隔离文件。"""
    original_open = Path.open

    def open_with_locale(
        path: Path, mode: str = "r", buffering: int = -1,
        encoding: str | None = None, errors: str | None = None,
        newline: str | None = None,
    ):
        if path.is_relative_to(tmp_path) and "b" not in mode and encoding in {None, "locale"}:
            encoding = request.param
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_with_locale)
    monkeypatch.setattr(e2e_test_control, "_ControlServer", Mock())
    thread = Mock()
    thread.is_alive.return_value = False
    monkeypatch.setattr(e2e_test_control.threading, "Thread", Mock(return_value=thread))
    control = e2e_test_control.E2ETestControlHarness(tmp_path / "control")
    try:
        control.start()
        yield control
    finally:
        control.close()


def test_control_close_saves_unicode_events_as_utf8(
    control_files: e2e_test_control.E2ETestControlHarness,
) -> None:
    control = control_files
    control._dispatch({
        "action": "phase", "operation_id": "fixture-operation", "phase": "已完成",
        "pid": 1, "details": {"text": "会话中文🙂"},
    })
    expected = list(control.events)

    control.close()

    raw = (control.root / "events.json").read_bytes()
    assert json.loads(raw.decode("utf-8")) == expected
    assert "会话中文🙂".encode() in raw
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("unicode_in_existing", [False, True])
def test_model_plan_preserves_utf8_cassette_and_new_interactions(
    control_files: e2e_test_control.E2ETestControlHarness, unicode_in_existing: bool,
) -> None:
    control = control_files
    existing = {"response": "会话中文🙂" if unicode_in_existing else "original"}
    appended = {"response": "next" if unicode_in_existing else "新回复🙂"}
    cassette = control.root / "cassette.json"
    cassette.write_bytes(json.dumps({"interactions": [existing]}, ensure_ascii=False).encode("utf-8"))
    control.bind_cassette(cassette)

    call_ids = control.plan_model_calls("fixture-session", "fixture-thread", [appended])

    assert len(call_ids) == 1
    raw = cassette.read_bytes()
    assert json.loads(raw.decode("utf-8")) == {"interactions": [existing, {
        **appended, "identity": {
            "session_id": "fixture-session", "thread_id": "fixture-thread", "model_call_id": call_ids[0],
        },
    }]}
    assert raw.endswith(b"\n")
    assert not cassette.with_suffix(".writing").exists()


def test_invalid_utf8_cassette_is_rejected_without_replacing_source(
    control_files: e2e_test_control.E2ETestControlHarness,
) -> None:
    control = control_files
    cassette = control.root / "cassette.json"
    original = b'{"interactions":[{"response":"\xff\xfe"}]}'
    cassette.write_bytes(original)
    control.bind_cassette(cassette)

    with pytest.raises(UnicodeDecodeError):
        control.plan_model_calls("fixture-session", "fixture-thread", [])

    assert cassette.read_bytes() == original
    assert not cassette.with_suffix(".writing").exists()


def test_chat_plan_uses_shared_protocol_assets_and_exact_owner(
    control_files: e2e_test_control.E2ETestControlHarness,
) -> None:
    from app.testing.model_stream import load_cassette

    control = control_files
    cassette = control.root / "planned.json"
    cassette.write_bytes((Path.cwd() / "tests/fixtures/model_stream/handwritten/openai_chat/basic-chat-tool-loop.json").read_bytes())
    control.bind_cassette(cassette)
    planned = control._dispatch({
        "action": "plan_chat", "session_id": "ses_00000000400070008000000000000001", "thread_id": "thr_00000000400070008000000000000002",
        "responses": [{
            "response_id": "planned-tool", "deltas": [
                {"role": "assistant", "reasoning_content": "先读取"},
                {"tool_calls": [{"index": 0, "id": "call-read", "type": "function",
                                 "function": {"name": "read_file", "arguments": '{"path":"中文.md"}'}}]},
                {"tool_calls": [{"index": 1, "id": "call-list", "type": "function",
                                 "function": {"name": "list_dir", "arguments": '{}'}}]},
            ], "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18},
        }],
    })
    interaction = load_cassette(cassette).interactions[-1]
    assert interaction.identity.to_dict() == {
        "session_id": "ses_00000000400070008000000000000001", "thread_id": "thr_00000000400070008000000000000002", "model_call_id": planned[0],
    }
    frames = interaction.response.frames
    # 真实 loader 校验完整协议，另核对内容、工具顺序和独立 usage 帧。
    assert [frame.payload["choices"][0]["delta"] for frame in frames[:3]] == [
        {"role": "assistant", "reasoning_content": "先读取"},
        {"tool_calls": [{"index": 0, "id": "call-read", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"中文.md"}'}}]},
        {"tool_calls": [{"index": 1, "id": "call-list", "type": "function",
                         "function": {"name": "list_dir", "arguments": '{}'}}]},
    ]
    assert frames[3].payload["choices"][0]["finish_reason"] == "tool_calls"
    assert frames[4].payload["usage"] == {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}
    assert frames[5].kind == "done"
