"""真实 Gateway／Workbench 消费 HTTP Skill accepted 快照，不执行 Agent。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_gateway_http_skill_flow import (
    HttpSkillServer,
)
from tests.integration.backend.agents.test_gateway_http_skill_flow import (
    http_skill_server as http_skill_server,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_workspace_http_skill_flow import _reload
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_configured_http_skills_discover_open_and_fail_without_runtime_or_refetch(
    request: pytest.FixtureRequest, http_skill_server: HttpSkillServer,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, provider = metadata_case
    source = http_skill_server
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Skill 来源入口验收需要 Chromium"
    base = source.url.split("/private/")[0]
    for label in ("a", "b"):
        for source_id in ("first", "higher"):
            marker = f"HTTP-SKILL-{label.upper()}-{source_id.upper()} Ω"
            source.variants[f"/{label}/{source_id}/SKILL.md"] = (
                f"---\nname: network-demo\ndescription: 原 HTTP 来源\n---\n{marker}\n".encode(), f'"{label}-{source_id}"',
            )

    def sources(label: str) -> list[dict[str, object]]:
        return [{"id": source_id, "name": "network-demo", "priority": priority,
                 "fetch": {"protocol": "http", "url": f"{base}/{label}/{source_id}/SKILL.md"}, "updates": {"mode": "manual"}}
                for source_id, priority in (("first", 0), ("higher", 7))]

    config_path = workspace / ".boxteam/workspace.jsonc"
    config = commentjson.loads(config_path.read_text())
    config.setdefault("resources", {}).setdefault("skill_sources", {})["sources"] = sources("a")
    await _reload(client, config_path, config)
    assert len(source.requests) == 2
    other = prepare_default_test_workspace(workspace_root=workspace.with_name("skill-B"),
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    other_config = install_test_workspace_config(workspace_root=other, config_path=config_path,
        schema_path=context.project_root / "configs/workspace_schema.jsonc")
    config["resources"]["skill_sources"]["sources"] = sources("b")
    other_config.write_text(json.dumps(config, ensure_ascii=False))
    (workspace / "AGENTS.md").write_text("# 工作区 A 根来源\n", encoding="utf-8")
    (other / "AGENTS.md").write_text("# 工作区 B 根来源\n", encoding="utf-8")
    registered = await client.post("/api/gateway/managed-workspaces", json={"root_path": str(other), "name": "Skill B"})
    assert registered.status_code == 200, registered.text
    assert len(source.requests) == 4
    result_path, fault_ready = context.artifacts_dir / "skill-sources.json", context.artifacts_dir / "fault-ready.json"
    initial_count = len(source.requests)
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "bun", "tests/integration/clients/web/quick_access/resources/skill_sources.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        task = asyncio.create_task(asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_SKILL_FAULT_READY": str(fault_ready),
                 "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "skill-sources.png"),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False))
        async with asyncio.timeout(185):
            while not task.done():
                if fault_ready.with_name(fault_ready.name + ".request").is_file():
                    assert len(source.requests) == initial_count, "浏览、筛选、取消和原只读打开不得重新获取 manual 来源"
                    # 具名外部来源故障：200 非 UTF-8，经原 refresh 接受为 unavailable，不改 accepted 存储。
                    source.variants["/b/higher/SKILL.md"] = (b"\xff", '"invalid-utf8"')
                    fault_ready.touch()
                    break
                await asyncio.sleep(0.05)
        completed = await task
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n")
    (context.artifacts_dir / "source-requests.json").write_text(json.dumps({
        "initial_count": initial_count, "final_count": len(source.requests), "requests": source.requests,
    }, ensure_ascii=False, indent=2) + "\n")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert all(result[key] for key in ("metadataOnly", "filterReuse", "scopeCancel", "sameNameSources", "originalOpen",
                                      "readOnly", "sideAndOwner", "explicitOwner", "originalFailure", "unavailable"))
    assert result["forbiddenWrites"] == []
    assert len(source.requests) == initial_count + 2, "只允许显式 refresh 获取两项；发现／错误展示不自动重试"
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "Skill 来源浏览与只读打开不得触发模型调用"
    assert not list((workspace / ".boxteam/sessions").rglob("session.json"))
    assert not list((other / ".boxteam/sessions").rglob("session.json")), "没有 Thread，不得启动 Agent runtime 编译"
