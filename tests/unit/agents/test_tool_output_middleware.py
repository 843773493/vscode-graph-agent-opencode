from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import ToolMessage

from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.core.resource_uri import session_artifact_uri
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.tool_output_store import (
    TOOL_OUTPUT_ARTIFACT_KEY,
    ToolOutputStore,
    extract_tool_output_reference,
)


def test_tool_output_missing_session_is_missing_resource(tmp_path: Path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam/sessions", "ses_019bd0aa8bb2784aaadbe51563ee17f0")
    missing_session = "ses_019c3567da377a6f855e1bddac8f1f76"
    uri = session_artifact_uri(read_workspace_id(tmp_path), missing_session, "tool-results", "tool_" + "a" * 24 + ".txt")
    store = ToolOutputStore(workspace_root=tmp_path)
    with pytest.raises(FileNotFoundError) as failure:
        store.read(missing_session, uri, 1024)
    assert missing_session in str(failure.value)
    assert str(tmp_path) not in str(failure.value)
    assert isinstance(failure.value.__cause__, KeyError)


def test_tool_output_store_keeps_small_result_unchanged(tmp_path: Path) -> None:
    store = ToolOutputStore(workspace_root=tmp_path)
    message = ToolMessage(content="small result", tool_call_id="call-small")

    result = store.bound(
        session_id="ses_019bbea3e8b7744984826ff1d2840e03",
        tool_name="small_tool",
        tool_call_id="call-small",
        message=message,
    )

    assert result is message
    if (tmp_path / ".boxteam").exists():
        # catalog 模式：ToolOutputStore 构造经 path_utils 工厂装配 SQLite
        # catalog（开关副作用），但不允许物化任何工具输出产物。
        assert not list((tmp_path / ".boxteam").rglob("tool-results"))
        assert not (tmp_path / ".boxteam" / "sessions").exists()
    # 旧模式：构造零副作用（.boxteam 不存在即完整锁定）。


def test_tool_output_store_persists_exact_large_result_and_returns_preview(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019bd0aa8bb2784aaadbe51563ee17f0"
    session_dir = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions",
        session_id,
    )
    content = "\n".join(f"large-line-{index:04d}: {'内容' * 20}" for index in range(80))
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
        session_id=session_id,
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
    output_name = Path(str(reference["read_path"])).name
    assert reference["path"] == (
        session_artifact_uri(read_workspace_id(tmp_path), session_id, "tool-results", output_name)
    )
    assert reference["read_path"] == (
        reference["path"]
    )
    assert reference["tool_name"] == "large_tool"
    assert reference["tool_call_id"] == "call-large"
    assert reference["byte_count"] == len(content.encode("utf-8"))
    assert reference["line_count"] == 80
    assert (
        reference["content_sha256"]
        == hashlib.sha256(content.encode("utf-8")).hexdigest()
    )
    output_path = session_dir / "tool-results" / output_name
    assert output_path.parent == session_dir / "tool-results"
    assert output_path.read_text(encoding="utf-8") == content
    assert isinstance(result.artifact, dict)
    assert result.artifact["original_tool_artifact"] == {"source": "unit-test"}
    assert TOOL_OUTPUT_ARTIFACT_KEY in result.artifact


def test_tool_output_store_reuses_identical_tool_call_output(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019c268530e971098bb8da438d000f00"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    content = "x" * 2_000
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )
    message = ToolMessage(content=content, tool_call_id="call-repeat")

    first = store.bound(
        session_id=session_id,
        tool_name="repeat_tool",
        tool_call_id="call-repeat",
        message=message,
    )
    second = store.bound(
        session_id=session_id,
        tool_name="repeat_tool",
        tool_call_id="call-repeat",
        message=message,
    )

    assert first.content == second.content
    assert extract_tool_output_reference(first) == extract_tool_output_reference(second)


def test_tool_output_store_keeps_large_exec_command_result_valid_json(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019bea2f102b7f6c805a92b787288ac0"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    full_payload = {
        "chunk_id": "term_large",
        "exit_code": 0,
        "output": "\n".join(f"line-{index}: 内容" for index in range(500)),
        "wall_time_seconds": 0.5,
    }
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )

    result = store.bound(
        session_id=session_id,
        tool_name="exec_command",
        tool_call_id="call-exec-command",
        message=ToolMessage(
            content=json.dumps(full_payload, ensure_ascii=False),
            tool_call_id="call-exec-command",
        ),
    )

    assert isinstance(result.content, str)
    assert len(result.content.encode("utf-8")) <= 1_024
    preview_payload = json.loads(result.content)
    assert preview_payload["chunk_id"] == "term_large"
    assert preview_payload["exit_code"] == 0
    assert "工具输出过大" in preview_payload["output"]


def test_tool_output_middleware_uses_custom_target_name(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_id = "ses_019c49d551f17a0686eed4c785689d2a"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    store = ToolOutputStore(
        workspace_root=tmp_path,
        max_lines=20,
        max_bytes=1_024,
    )
    middleware = ToolOutputMiddleware(session_id=session_id, store=store)
    request = type(
        "Request",
        (),
        {
            "tool_call": {
                "id": "call-custom",
                "name": "invoke_extension_tool",
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


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("declaration", [{"status": "error"}, {"ok": False}])
async def test_declared_failure_bypasses_output_preview(mode, declaration):
    # 失败正文必须与实时工具结果相同，不能只在模型链路替换成成功预览。
    store = Mock(spec=ToolOutputStore)
    middleware = ToolOutputMiddleware(session_id="ses_failure", store=store)
    request = SimpleNamespace(tool_call={"id": "call_failure", "name": "apply_patch", "args": {}})
    source = ToolMessage(content=json.dumps({**declaration, "error": "失败详情" * 2000}), tool_call_id="call_failure")
    if mode == "sync":
        result = middleware.wrap_tool_call(request, lambda _: source)
    else:
        async def completed(_request):
            return source
        result = await middleware.awrap_tool_call(request, completed)
    assert result is source
    store.bound.assert_not_called()
    store.abound.assert_not_called()


def test_tool_output_uri_reopens_after_owner_restart_and_rejects_other_session(tmp_path, session_bundle_factory):
    import pytest

    session_id = "ses_019bd0aa8bb2784aaadbe51563ee17f0"
    session_bundle_factory(tmp_path / ".boxteam/sessions", session_id)
    store = ToolOutputStore(workspace_root=tmp_path, max_lines=8, max_bytes=1024)
    body = "hit\r\n" * 1000
    bounded = store.bound(session_id=session_id, tool_name="large", tool_call_id="call-reopen",
                          message=ToolMessage(content=body, tool_call_id="call-reopen"))
    reference = extract_tool_output_reference(bounded)
    assert reference["path"] == reference["read_path"]
    cold = ToolOutputStore(workspace_root=tmp_path)
    assert cold.read(session_id, reference["path"], len(body.encode())).data == body.encode()
    with pytest.raises(PermissionError, match="当前工作区会话"):
        cold.read("ses_019bc4518d0079d3a53e974eea6f21a9", reference["path"], 10000)
    path = store._path_resolver.resolve_session_node(session_id) / "tool-results"
    original = path.with_name("original-results")
    path.rename(original)
    path.symlink_to(original, target_is_directory=True)
    with pytest.raises(PermissionError, match="符号链接"):
        cold.read(session_id, reference["path"], 10000)
