"""较早回答的前缀分支回归；不是 Section 8 完整生命周期的第二个 owner。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 复用正式构建装具
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    browser_backend as browser_backend,  # noqa: PLC0414 - request 决定本文件的独立工作区及端口
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.sealed_request_audit import audit_sealed_requests


def _cassettes(model_url: str) -> list[dict[str, object]]:
    responses = [
        ([{"role": "assistant", "tool_calls": [{"index": 0, "id": call, "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]}], "tool_calls")
        if call else ([{"role": "assistant", "content": text}], "stop")
        for call, text in (
            ("prefix-kept-read", ""), ("", "较早回答：保留这次工具结果。"),
            ("prefix-excluded-read", ""), ("", "后续回答：不应进入较早分支。"),
            ("", "分支续聊：只使用较早的完整工具历史。"),
        )
    ]
    return [build_cassette(
        asset_id="fork-prefix-recovery", model="browser-chat-tool-loop-stub", url=model_url,
        frames=chat_completion_frames(
            response_id=f"fork-prefix-{index}", model="browser-chat-tool-loop-stub",
            deltas=deltas, finish_reason=finish,
        ),
    ).raw for index, (deltas, finish) in enumerate(responses)]


@pytest.mark.asyncio
async def test_earlier_answer_fork_excludes_later_tools_after_browser_refresh(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path,
) -> None:
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    ports = integration_port_block_for_file(Path(request.node.path))
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "前缀分支 Web 回归需要真实 Chromium"
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url, port=ports.port(20),
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    command = ["node", "tests/integration/clients/web/itemized_context/fork_prefix_recovery.mjs"]
    try:
        environment = {
            **os.environ, **replay.environment,
            "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
            "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts),
            "BOXTEAM_BROWSER_TEST_OWNER": str(request.node.path),
            "BOXTEAM_BROWSER_MODEL_CASSETTES": json.dumps(_cassettes(
                f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
            )),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        with (
            chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary,
            (artifacts / "browser.log").open("x", encoding="utf-8") as output,
            capture_test_processes(),
        ):
            environment.update(temporary)
            process = await asyncio.to_thread(
                subprocess.Popen, command, env=environment, stdout=output, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
            managed = ManagedProcess(process=process, log_file=output)
            register_test_process(managed, managed.close)
            returncode = await asyncio.to_thread(process.wait, timeout=240)
        (artifacts / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": returncode,
        }, indent=2) + "\n", encoding="utf-8")
        assert returncode == 0, (artifacts / "browser.log").read_text()
        facts = json.loads((artifacts / "fork-prefix-result.json").read_text())
        assert facts["phase"] == "complete"
        assert facts["pageErrors"] == []
        assert facts["refreshedHistory"] == facts["importedHistory"]
        source, child = facts["source"], facts["child"]
        assert source["sessionId"] == child["sessionId"]
        assert source["threadId"] != child["threadId"]

        calls = facts["modelCalls"]
        assert len(calls) == len(set(calls)) == 5
        captures = {call: json.loads((replay.artifacts / f"{call}.json").read_text()) for call in calls}
        first, second, later, later_final, successor = [captures[call]["request"]["messages"] for call in calls]
        assert second[:-2] == first
        assert later_final[:-2] == later
        kept = [message for message in second if message["role"] in {"assistant", "tool"}]
        assert [message["role"] for message in kept] == ["assistant", "tool"]
        assert kept[0]["tool_calls"][0]["id"] == kept[1]["tool_call_id"] == "prefix-kept-read"
        assert "# 统一测试工作区" in kept[1]["content"]
        assert successor[0] == first[0]
        inherited = [message for message in successor if message["role"] in {"assistant", "tool"}]
        assert inherited == [*kept, {"role": "assistant", "content": [
            {"text": "较早回答：保留这次工具结果。", "type": "text"},
        ]}]
        for excluded in ("prefix-excluded-read", facts["prompts"][1], "后续回答：不应进入较早分支。"):
            assert excluded not in json.dumps(successor, ensure_ascii=False)
        for included in (facts["prompts"][0], facts["prompts"][2]):
            assert any(message["role"] == "user" and message["content"] == included for message in successor)

        resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
        indexes = {owner["threadId"]: resolver.resolve_thread_node(owner["sessionId"], owner["threadId"]) / "rollout/index.sqlite"
                   for owner in (source, child)}
        reports = {thread: audit_sealed_requests(
            index, (resolver.resolve_session_node(source["sessionId"]) / "logs/llm_requests").glob("*.json"), captures,
        ) for thread, index in indexes.items()}
        audited_count = 0
        for report in reports.values():
            audited = report["requests"]
            assert isinstance(audited, list)
            audited_count += len(audited)
        assert audited_count == 5
        # 只读核对历史来源身份；不能把 fork 继承的 canonical 正文改为 child 新产生的记录。
        rows = {}
        for thread, index in indexes.items():
            with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
                rows[thread] = connection.execute(
                    "SELECT i.item_id,i.item_sequence,i.execution_id,i.turn_id,i.content_hash,i.source_revision,e.session_id,e.thread_id "
                    "FROM item_catalog i JOIN executions e USING(execution_id) WHERE i.turn_id=? ORDER BY i.item_sequence",
                    (facts["sourceTurnIds"][0],),
                ).fetchall()
                if thread == child["threadId"]:
                    assert connection.execute("SELECT count(*) FROM item_catalog WHERE turn_id=?", (facts["sourceTurnIds"][1],)).fetchone() == (0,)
        assert rows[source["threadId"]]
        assert rows[source["threadId"]] == rows[child["threadId"]]
        assert all(row[-2:] == (source["sessionId"], source["threadId"]) for row in rows[child["threadId"]])
        (artifacts / "sealed-request-audit.json").write_text(
            json.dumps({"threads": reports, "historical_rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    finally:
        close_gateway_process(gateway)
