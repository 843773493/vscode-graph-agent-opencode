"""真实启动、合并挂载与重启保持目标安装域身份。"""

from __future__ import annotations

from app.core.backend_identity import read_backend_id
from tests.integration.test_shared_managed_backend_lifecycle import (
    SharedBackendCase,
    _join_group,
)
from tests.integration.test_shared_managed_backend_lifecycle import (
    shared_backend_case as shared_backend_case,  # noqa: PLC0414
)


async def test_installation_identity_survives_group_changes_and_gateway_restart(
    shared_backend_case: SharedBackendCase,
) -> None:
    case = shared_backend_case
    # 先让产品完成首次启动；测试只读取其发布结果，不替产品准备身份。
    backend_id = read_backend_id(case.boxteam_home)

    async def health(workspace_id: str | None = None) -> dict[str, object]:
        request_id = "installation-identity-" + (workspace_id or "initial")
        response = await case.client.get("/api/v1/health", headers={
            "X-Request-ID": request_id,
            **({"X-BoxTeam-Workspace-Id": workspace_id} if workspace_id else {}),
        })
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == request_id
        payload = response.json()
        assert payload["backend_id"] == backend_id
        assert payload["status"] == "ok"
        return payload

    initial = await health()
    primary, secondary, joined = await _join_group(case)
    assert initial["workspace_id"] == primary
    joined_health = [await health(identity) for identity in (primary, secondary)]
    assert joined_health[0]["process_id"] == joined_health[1]["process_id"]
    assert joined_health[0]["process_id"] != initial["process_id"]
    assert joined_health[0]["backend_generation"] == joined["backend_generation"]
    assert joined_health[0]["backend_generation"] != initial["backend_generation"]
    await case.data("POST", f"/api/gateway/workspaces/{secondary}/activate")
    assert (await health())["workspace_id"] == secondary
    await case.restart_gateway()
    for identity in (primary, secondary):
        assert (await health(identity))["workspace_id"] == identity
    assert read_backend_id(case.boxteam_home) == backend_id
