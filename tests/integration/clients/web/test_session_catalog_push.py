from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — 复用正式构建。
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


@pytest.fixture
def catalog_push_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


async def test_catalog_push_idle_and_updates(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    catalog_push_port: int,
    web_workbench_assets: Path,
) -> None:
    del integration_workspace_config_path
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("目录推送验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    result_path = context.artifacts_dir / "catalog-push-result.json"
    home = context.boxteam_home_for_node(request.node.nodeid)
    database = home / "state/gateway/gateway.sqlite"
    profile = home / "state/gateway/users/retired-user/profile.jsonc"
    profile.parent.mkdir(parents=True, exist_ok=True)
    original_profile = b'{"legacy_note":"keep original"}\n'
    profile.write_bytes(original_profile)
    # 历史保护的查询前提，不替产品创建当前身份或业务执行状态。
    store = GatewayStateStore(path=database)
    try:
        with store.connection() as connection:
            connection.execute("INSERT INTO user_account VALUES(?,?,?,NULL)", ("retired-user", "旧用户", "2000-01-01T00:00:00Z"))
            connection.execute("INSERT INTO guest_tracking VALUES(?,?,?,?,?)", ("retired-guest", "{}", "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z"))
    finally:
        store.close()
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=catalog_push_port, boxteam_home=home,
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    try:
        command = ["bun", "tests/integration/clients/web/session_catalog_push.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": result.returncode,
        }) + "\n")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["idleNavigationGets"] == []
        assert receipt["onlineUpdateVisible"] is True
        assert receipt["pageErrors"] == []
        assert receipt["retiredUserRequests"] == []
        assert receipt["nativeImage"]["loadedWidth"] == 2
        assert profile.read_bytes() == original_profile
        assert {item.name for item in profile.parent.parent.iterdir()} == {"retired-user"}
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute("SELECT user_id FROM user_account").fetchall() == [("retired-user",)]
            assert connection.execute("SELECT guest_id FROM guest_tracking").fetchall() == [("retired-guest",)]
            assert connection.execute("SELECT COUNT(*) FROM user_access_lease").fetchone() == (0,)
    finally:
        close_gateway_process(gateway)
