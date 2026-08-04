from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.api_waiters import wait_for_job_done
from tests.support.trace import get_trace_payload
from tests.support.paths import e2e_output_root_for_test
from tests.support.processes import close_backend_process, start_backend_process


def _chatgpt_auth_sources() -> tuple[Path, Path]:
    user_home = Path.home()
    boxteam_home = Path(
        os.environ.get("BOXTEAM_HOME", str(user_home / ".boxteams"))
    ).expanduser()
    return (
        boxteam_home / "auth" / "chatgpt" / "auth.json",
        user_home / ".codex" / "auth.json",
    )


async def _send_agent_message(
    client: httpx.AsyncClient,
    session_id: str,
    content: str,
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
                "max_steps": 4,
                "timeout_seconds": 180,
            },
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=180)
    assert job["status"] in {"completed", "succeeded"}, job
    return job_id


def _contains_mapping_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(
            _contains_mapping_key(item, target) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_mapping_key(item, target) for item in value)
    return False


def _cache_read_tokens(log: dict) -> int | None:
    results = log.get("response", {}).get("result", [])
    assert results, "ChatGPT OAuth LLM 日志缺少响应消息"
    usage = next(
        (
            result.get("usage_metadata")
            for result in reversed(results)
            if isinstance(result.get("usage_metadata"), dict)
        ),
        None,
    )
    # TODO: LiteLLM 稳定返回首轮 usage 后，恢复每轮都必须存在 usage 的严格断言。
    # ChatGPT 流式首轮偶尔不返回 usage；缓存断言只依赖后续轮次的真实 usage。
    if usage is None:
        return None
    details = usage.get("input_token_details") or {}
    assert isinstance(details, dict), "ChatGPT OAuth input_token_details 类型无效"
    cached_tokens = details.get("cache_read", 0)
    assert isinstance(cached_tokens, int) and cached_tokens >= 0
    return cached_tokens


@pytest.fixture(scope="module")
def e2e_backend_process(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_workspace_config_path: str,
    e2e_backend_port: int,
    is_debug: bool,
) -> Generator[subprocess.Popen[str], None, None]:
    """使用隔离 BOXTEAM_HOME 启动真实后端，并在结束后删除测试凭据。"""
    litellm_auth, codex_auth = _chatgpt_auth_sources()
    if not litellm_auth.is_file() and not codex_auth.is_file():
        pytest.skip("需要 BoxTeam ChatGPT OAuth 或 Codex OAuth 凭据")

    output_root = e2e_output_root_for_test(Path(request.node.fspath))
    isolated_home = output_root / "artifacts" / "boxteam-home"
    token_dir = isolated_home / "auth" / "chatgpt"
    if isolated_home.exists():
        shutil.rmtree(isolated_home)
    token_dir.mkdir(parents=True, mode=0o700)
    isolated_config_dir = isolated_home / "config"
    isolated_config_dir.mkdir(parents=True, exist_ok=True)
    workspace_config = Path(e2e_workspace_config_path)
    shutil.copy2(
        workspace_config,
        isolated_config_dir / "workspace.jsonc",
    )
    shutil.copy2(
        workspace_config.parent / "workspace_config.jsonc",
        isolated_config_dir / "workspace_config.jsonc",
    )

    if litellm_auth.is_file():
        target_auth = token_dir / "auth.json"
        shutil.copyfile(litellm_auth, target_auth)
        target_auth.chmod(0o600)

    debugpy_port = (
        int(os.environ["BOXTEAM_E2E_BACKEND_DEBUGPY_PORT"])
        if is_debug
        else None
    )
    handle = start_backend_process(
        workspace_root=e2e_workspace_root_path,
        port=e2e_backend_port,
        log_name="chatgpt-oauth-provider-e2e",
        debugpy_port=debugpy_port,
        env_overrides={
            "BOXTEAM_HOME": str(isolated_home),
            "CHATGPT_TOKEN_DIR": str(token_dir),
            "CHATGPT_AUTH_FILE": "auth.json",
        },
    )
    try:
        yield handle.process
    finally:
        close_backend_process(handle)
        shutil.rmtree(isolated_home, ignore_errors=True)


@pytest.mark.asyncio
async def test_backup_4_chatgpt_oauth_hits_prompt_cache_with_stable_session(
    client: httpx.AsyncClient,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "ChatGPT OAuth Provider E2E", "agent_id": "default"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]

    provider_response = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"provider_id": "backup_4"},
    )
    assert provider_response.status_code == 200, provider_response.text
    assert provider_response.json()["data"]["current_provider_id"] == "backup_4"

    stable_material = "\n".join(
        f"ChatGPT OAuth 缓存固定资料-{index:04d}：这一行必须保持原样。"
        for index in range(220)
    )
    first_expected = "CHATGPT_OAUTH_CACHE_FIRST"
    first_job_id = await _send_agent_message(
        client,
        session_id,
        (
            "必须先调用 read_file 工具读取工作区 README.md；工具返回后，"
            f"请记住以下资料，只回复 {first_expected}。\n{stable_material}"
        ),
    )
    second_expected = "CHATGPT_OAUTH_CACHE_SECOND"
    second_job_id = await _send_agent_message(
        client,
        session_id,
        f"继续之前的会话，只回复 {second_expected}，不要调用工具。",
    )
    third_expected = "CHATGPT_OAUTH_CACHE_THIRD"
    third_job_id = await _send_agent_message(
        client,
        session_id,
        f"再次继续，只回复 {third_expected}，不要调用工具。",
    )

    messages_response = await client.get(
        f"/api/v1/sessions/{session_id}/messages"
    )
    assert messages_response.status_code == 200, messages_response.text
    assistant_message = next(
        message
        for message in reversed(messages_response.json()["data"]["items"])
        if message["role"] == "assistant"
    )
    assert third_expected in assistant_message["content"]
    assert assistant_message["metadata"]["provider_id"] == "backup_4"
    assert assistant_message["metadata"]["custom_llm_provider"] == "chatgpt"

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200, logs_response.text
    logs = logs_response.json()["data"]
    logs_by_job = {
        job_id: [log for log in logs if log.get("job_id") == job_id]
        for job_id in (first_job_id, second_job_id, third_job_id)
    }
    assert all(logs_by_job.values())
    selected_job_ids = {first_job_id, second_job_id, third_job_id}
    selected_logs = [
        log for log in logs if log.get("job_id") in selected_job_ids
    ]
    assert selected_logs
    assert all(
        log.get("request", {}).get("model_name") == "gpt-5.6-luna"
        for log in selected_logs
    ), selected_logs
    assert all(log.get("response", {}).get("error") is None for log in selected_logs), (
        selected_logs
    )
    for log in selected_logs:
        attempts = log.get("upstream", {}).get("attempts") or []
        assert attempts, log
        for attempt in attempts:
            upstream_request = attempt.get("request") or {}
            assert not _contains_mapping_key(
                upstream_request,
                "provider_part_id",
            ), upstream_request

    traces_response = await client.get(
        f"/api/v1/sessions/{session_id}/traces",
        params={"limit": 200},
    )
    assert traces_response.status_code == 200, traces_response.text
    traces = traces_response.json()["data"]["items"]
    assert not [
        trace
        for trace in traces
        if trace.get("job_id") in selected_job_ids
        and trace.get("type") == "model_failed"
    ], traces
    assert any(
        trace.get("job_id") == first_job_id
        and trace.get("type") == "tool_call_start"
        and get_trace_payload(trace).get("tool_name") == "read_file"
        for trace in traces
    ), traces
    first_log = logs_by_job[first_job_id][-1]
    second_log = logs_by_job[second_job_id][-1]
    third_log = logs_by_job[third_job_id][-1]
    assert first_log["request"]["model_name"] == "gpt-5.6-luna"

    cached_tokens = [
        _cache_read_tokens(first_log),
        _cache_read_tokens(second_log),
        _cache_read_tokens(third_log),
    ]
    upstream_cache_keys = [
        log["upstream"]["attempts"][-1]["response"]["prompt_cache_key"]
        for log in (first_log, second_log, third_log)
    ]
    print(f"\n[chatgpt-oauth-prompt-cache] cached_tokens={cached_tokens}")
    assert upstream_cache_keys == [session_id, session_id, session_id]
    subsequent_cached_tokens = [
        value for value in cached_tokens[1:] if value is not None
    ]
    assert subsequent_cached_tokens, {
        "message": "ChatGPT OAuth 后续请求没有返回可验证的 usage_metadata",
        "cached_tokens": cached_tokens,
        "session_id": session_id,
    }
    assert max(subsequent_cached_tokens) > 0, {
        "message": "稳定 ChatGPT session_id 的后续请求没有命中 Prompt Cache",
        "cached_tokens": cached_tokens,
        "session_id": session_id,
    }
