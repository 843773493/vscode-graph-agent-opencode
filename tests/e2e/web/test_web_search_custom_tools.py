from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from tests.e2e.utils import get_trace_payload, last_assistant_message, wait_for_job_done


CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS = (
    "AGENTS.md",
    ".boxteam/boxteam.json",
    ".boxteam/skills",
)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(
    request: pytest.FixtureRequest,
    e2e_session_marker: str,
) -> str:
    project_root = Path.cwd().resolve()
    tests_root = project_root / "tests" / "e2e"
    test_file_path = Path(request.node.fspath).resolve()
    relative_test_path = test_file_path.relative_to(tests_root).with_suffix("")
    workspace_root = project_root / "out" / "tests" / "e2e" / relative_test_path
    template_root = project_root / "asset" / "custom_tool_test_workspace"
    lock_file = workspace_root / ".e2e_session_lock"

    same_session = (
        lock_file.exists()
        and lock_file.read_text(encoding="utf-8").strip() == e2e_session_marker
    )
    if workspace_root.exists() and not same_session:
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    for item in workspace_root.iterdir():
        if item.resolve() == lock_file.resolve():
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for relative_item in CUSTOM_TOOL_WORKSPACE_TEMPLATE_ITEMS:
        item = template_root / relative_item
        if not item.exists():
            raise FileNotFoundError(f"custom tool e2e 模板缺少必要文件: {item}")
        target = workspace_root / relative_item
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    lock_file.write_text(e2e_session_marker, encoding="utf-8")
    return str(workspace_root)


def _json_object_from_text(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AssertionError(f"未在文本中找到 JSON object: {text!r}")
    parsed = json.loads(text[start:end + 1])
    assert isinstance(parsed, dict)
    return parsed


def _read_file_path_from_trace(trace: dict[str, Any]) -> str:
    args = get_trace_payload(trace).get("args", {})
    if not isinstance(args, dict):
        return ""
    return str(args.get("file_path") or args.get("path") or "")


@pytest.mark.asyncio
async def test_model_searches_web_then_fetches_selected_result(
    client: httpx.AsyncClient,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Web Search Fetch E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    prompt = (
        "请先读取当前工作区 AGENTS.md。根据其中说明找到并完整读取 Web 搜索与网页抓取 skill，"
        "然后必须发起真实扩展工具调用，不能只描述。"
        "先调用 web_search，query='Python programming language official website'，"
        "max_results=3，search_type='text'，time_range=null。"
        "优先选择结果中 python.org 的 URL；如果没有才选择第一条 URL。"
        "再调用 fetch_webpage 抓取这个 URL，query='Python programming language'，"
        "max_chars_per_page=2000。"
        "最终只回复一个 JSON 对象：search_result_count 使用搜索实际返回数量；"
        "fetched_url、status_code、title 使用抓取页面的实际字段；"
        "content_contains_python 表示抓取 content 忽略大小写后是否包含 python。不要解释。"
    )
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=180)
    assert job["status"] in {"completed", "succeeded"}

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    result = _json_object_from_text(
        last_assistant_message(messages_response.json()["data"]["items"])
    )
    assert result["search_result_count"] >= 1
    assert result["status_code"] == 200
    assert str(result["fetched_url"]).startswith(("http://", "https://"))
    assert result["content_contains_python"] is True
    assert str(result["title"]).strip()

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    read_file_paths = [
        _read_file_path_from_trace(trace)
        for trace in traces
        if trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "read_file"
    ]
    assert any(
        path.endswith("/.boxteam/skills/web-search-fetch/SKILL.md")
        for path in read_file_paths
    )
    for tool_name in ("web_search", "fetch_webpage"):
        tool_starts = [
            trace
            for trace in traces
            if trace.get("type") == "tool_call_start"
            and get_trace_payload(trace).get("tool_name") == tool_name
        ]
        assert tool_starts
        assert tool_starts[-1].get("skill_names", []) == ["web-search-fetch"]
        assert (
            get_trace_payload(tool_starts[-1]).get("invocation_tool_name")
            == CUSTOM_TOOL_INVOKER_NAME
        )

    fetch_ends = [
        trace
        for trace in traces
        if trace.get("type") == "tool_call_end"
        and get_trace_payload(trace).get("tool_name") == "fetch_webpage"
    ]
    assert fetch_ends
    fetch_result = json.loads(str(get_trace_payload(fetch_ends[-1])["result"]))
    assert fetch_result["content_selection"] == {
        "strategy": "semantic_embedding",
        "provider_id": "backup_2",
        "model": "openai/text-embedding-3-small",
    }
    assert fetch_result["pages"][0]["content_selection"] == "semantic_embedding"
