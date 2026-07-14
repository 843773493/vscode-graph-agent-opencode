from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_core.messages import ToolMessage

from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.services.infrastructure.tool_output_store import (
    TOOL_OUTPUT_ARTIFACT_KEY,
    ToolOutputStore,
    extract_tool_output_reference,
)


def test_tool_output_store_keeps_small_result_unchanged(tmp_path: Path) -> None:
    store = ToolOutputStore(workspace_root=tmp_path)
    message = ToolMessage(content="small result", tool_call_id="call-small")

    result = store.bound(
        session_id="ses_small",
        tool_name="small_tool",
        tool_call_id="call-small",
        message=message,
    )

    assert result is message
    assert not (tmp_path / ".boxteam").exists()


def test_tool_output_store_persists_exact_large_result_and_returns_preview(
    tmp_path: Path,
) -> None:
    content = "\n".join(
        f"large-line-{index:04d}: {'内容' * 20}"
        for index in range(80)
    )
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )
    message = ToolMessage(
        content=content,
        tool_call_id="call-large",
        artifact={"source": "unit-test"},
    )

    result = store.bound(
        session_id="ses_large",
        tool_name="large_tool",
        tool_call_id="call-large",
        message=message,
    )

    assert isinstance(result.content, str)
    assert len(result.content.encode("utf-8")) <= 1_024
    assert result.content.count("\n") + 1 <= 20
    assert "工具输出过大" in result.content
    assert "large-line-0000" in result.content
    assert "large-line-0079" in result.content

    reference = extract_tool_output_reference(result)
    assert reference is not None
    assert reference["type"] == "tool_output"
    assert reference["read_path"] == f"/{reference['path']}"
    assert reference["tool_name"] == "large_tool"
    assert reference["tool_call_id"] == "call-large"
    assert reference["byte_count"] == len(content.encode("utf-8"))
    assert reference["line_count"] == 80
    assert reference["content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    output_path = tmp_path / str(reference["path"])
    assert output_path.read_text(encoding="utf-8") == content
    assert isinstance(result.artifact, dict)
    assert result.artifact["original_tool_artifact"] == {"source": "unit-test"}
    assert TOOL_OUTPUT_ARTIFACT_KEY in result.artifact


def test_tool_output_store_reuses_identical_tool_call_output(tmp_path: Path) -> None:
    content = "x" * 2_000
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )
    message = ToolMessage(content=content, tool_call_id="call-repeat")

    first = store.bound(
        session_id="ses_repeat",
        tool_name="repeat_tool",
        tool_call_id="call-repeat",
        message=message,
    )
    second = store.bound(
        session_id="ses_repeat",
        tool_name="repeat_tool",
        tool_call_id="call-repeat",
        message=message,
    )

    assert first.content == second.content
    assert extract_tool_output_reference(first) == extract_tool_output_reference(second)


def test_tool_output_middleware_uses_custom_target_name(tmp_path: Path) -> None:
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )
    middleware = ToolOutputMiddleware(session_id="ses_custom", store=store)
    request = type(
        "Request",
        (),
        {
            "tool_call": {
                "id": "call-custom",
                "name": "invoke_custom_tool",
                "args": {
                    "tool_name": "large_custom_tool",
                    "arguments": {},
                },
            }
        },
    )()
    source = ToolMessage(content="y" * 2_000, tool_call_id="call-custom")

    result = middleware.wrap_tool_call(request, lambda _: source)

    assert isinstance(result, ToolMessage)
    reference = extract_tool_output_reference(result)
    assert reference is not None
    assert reference["tool_name"] == "large_custom_tool"
