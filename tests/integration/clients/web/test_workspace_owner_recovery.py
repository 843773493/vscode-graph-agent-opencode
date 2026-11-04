"""正式工作台在同 UUID 的两个原生 Gateway 间保持文件、搜索与恢复归属。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def workspace_owner_browser_environment(
    monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path,
) -> None:
    # autouse 先于网络装具运行；两台 Gateway 沿原静态页面挂载服务同一正式产物。
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")


def test_real_workbench_restores_distinct_gateway_owners(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("工作区归属恢复验收需要 Chromium")
    # 仅准备文件查询输入；Session、Thread、关联、选择与草稿全部由产品入口产生。
    for index, root in enumerate(network.workspace_roots):
        (root / "identity-owner.txt").write_text(f"identityowner {'A' if index == 0 else 'B'} 正文\n", encoding="utf-8")
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registered = network.clients[0].post("/api/gateway/remote-gateways", json={
        "name": "身份恢复远端 B", "host": "127.0.0.1", "port": 22, "username": "integration",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    })
    assert registered.status_code == 200, registered.text
    assert {(item["gateway_id"], item["workspace_id"]) for item in registered.json()["data"]["items"]} == {
        (gateway, network.workspace_id) for gateway in network.gateway_ids
    }
    result_path = context.artifacts_dir / "workspace-owner-result.json"
    screenshot_path = context.artifacts_dir / "workspace-owner-failure.png"
    command = ["bun", "tests/integration/clients/web/workspace_owner_recovery.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        completed = subprocess.run(
            command, cwd=context.project_root, capture_output=True, text=True, timeout=150, check=False,
            env={**os.environ, **temporary,
                 "BOXTEAM_BROWSER_BASE_URL": str(network.clients[0].base_url),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                 "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
        )
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete"
    assert result["switches"] == [*network.gateway_ids, network.gateway_ids[0], network.gateway_ids[1]]
    assert result["restored"] == [*network.gateway_ids]
    assert result["pageErrors"] == []
    for index, root in enumerate(network.workspace_roots):
        assert (root / "identity-owner.txt").read_text(encoding="utf-8") == f"identityowner {'A' if index == 0 else 'B'} 正文\n"
