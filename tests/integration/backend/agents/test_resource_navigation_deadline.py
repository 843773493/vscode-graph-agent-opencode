"""两跳真实 Gateway 的原期限到达目标后不能重新获得完整查询预算。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.core.resource_uri import resource_display_uri
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
)
from tests.support.communication_federation import open_communication_triad


@pytest.fixture
async def deadline_triad(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    async with open_communication_triad(context, _prepare_workspace,
        backend_asgi_app="tests.integration.backend.agents.resource_navigation_deadline_backend:app",
        backend_environment={"BOXTEAM_TEST_NAVIGATION_ARTIFACTS": str(artifacts)}) as nodes:
        yield nodes, artifacts


async def test_real_federation_deadline_survives_target_parse_block(deadline_triad):
    nodes, artifacts = deadline_triad
    source, target = nodes["b"], nodes["c"]
    relative = "src/deadline-probe.txt"
    (source.workspace / relative).write_text("SOURCE DECOY", encoding="utf-8")
    (target.workspace / relative).write_text("TARGET BODY", encoding="utf-8")
    uri = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", "src", "deadline-probe.txt"), gateway_authority=target.gateway_id)
    # 先走相同 read grant 和两跳 channel，排除建连前失败冒充目标执行期限验收。
    warm = await source.client.post("/api/v1/resources/navigation", json={
        "uri": uri.rsplit("/", 1)[0], "operation": "describe"})
    assert warm.status_code == 200, warm.text
    started = time.monotonic()
    response = await source.client.post("/api/v1/resources/navigation", json={
        "uri": uri, "operation": "read", "timeout_ms": 1234},
        headers={NAVIGATION_DEADLINE_HEADER: str(time.time() + 0.4)})
    elapsed = time.monotonic() - started
    assert response.status_code in {400, 409}, response.text
    error = response.json()["detail"]
    assert error.get("reason") == "deadline" or error.get("code") == "federation-deadline-exceeded"
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert elapsed < 0.9, f"source 等待 {elapsed} 秒，超过原预算且等待了目标一秒解析"
    async with asyncio.timeout(5):
        finished = artifacts / f"{target.workspace_id}-finished.json"
        while not finished.is_file():
            await asyncio.sleep(0.02)
    observed = json.loads((artifacts / f"{target.workspace_id}-entered.json").read_text(encoding="utf-8"))
    assert observed["timeout_ms"] == 1234
    assert 0 < observed["remaining"] <= 0.4
    assert not (artifacts / f"{target.workspace_id}-read-attempted.json").exists()
    assert (source.workspace / relative).read_text(encoding="utf-8") == "SOURCE DECOY"
    assert (target.workspace / relative).read_text(encoding="utf-8") == "TARGET BODY"
    (artifacts / "source-response.json").write_text(json.dumps({
        "elapsed": elapsed, "status": response.status_code, "body": response.json(), "target": observed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
