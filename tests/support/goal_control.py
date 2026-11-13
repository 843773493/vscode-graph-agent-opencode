"""顺序业务场景显式读取控制版本，再通过原 Goal HTTP 入口提交一次。"""

from collections.abc import Mapping

import httpx

from app.core.identifier import create_uuid_hex


async def goal_control_payload(
    client: httpx.AsyncClient, session_id: str, payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    snapshot = await client.get(f"/api/v1/sessions/{session_id}/goal")
    assert snapshot.status_code == 200, snapshot.text
    return {**(payload or {}), "operation_id": f"goal-{create_uuid_hex()}",
            "expected_control_revision": snapshot.json()["data"]["control_revision"]}
