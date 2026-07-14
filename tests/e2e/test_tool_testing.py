from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest


async def _wait_for_tool_test(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    attempts: int = 180,
) -> dict:
    for _ in range(attempts):
        response = await client.get(f"/api/v1/tools/tests/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()["data"]
        if run["status"] in {"completed", "failed"}:
            return run
        await asyncio.sleep(1)
    raise TimeoutError(f"模型工具测试没有结束: run_id={run_id}")


@pytest.mark.asyncio
async def test_tool_catalog_selection_and_real_apply_patch_model_test(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    catalog_response = await client.get("/api/v1/tools", params={"agent_id": "default"})
    assert catalog_response.status_code == 200, catalog_response.text
    tools = catalog_response.json()["data"]
    by_name = {item["tool_id"]: item for item in tools}
    assert by_name["apply_patch"]["group_id"] == "default"
    assert by_name["apply_patch"]["test_supported"] is True
    assert by_name["edit_file"]["test_supported"] is True

    disable_response = await client.patch(
        "/api/v1/tools/selection",
        json={
            "agent_id": "default",
            "changes": [{"tool_id": "apply_patch", "enabled": False}],
        },
    )
    assert disable_response.status_code == 200, disable_response.text
    assert disable_response.json()["data"] == [
        {**by_name["apply_patch"], "enabled": False}
    ]

    restore_response = await client.patch(
        "/api/v1/tools/selection",
        json={
            "agent_id": "default",
            "changes": [
                {"tool_id": "apply_patch", "enabled": True},
                {"tool_id": "edit_file", "enabled": False},
            ],
        },
    )
    assert restore_response.status_code == 200, restore_response.text
    restored_by_name = {
        item["tool_id"]: item for item in restore_response.json()["data"]
    }
    assert restored_by_name["apply_patch"]["enabled"] is True
    assert restored_by_name["edit_file"]["enabled"] is False

    enable_edit_response = await client.patch(
        "/api/v1/tools/selection",
        json={
            "agent_id": "default",
            "changes": [{"tool_id": "edit_file", "enabled": True}],
        },
    )
    assert enable_edit_response.status_code == 200, enable_edit_response.text

    start_response = await client.post(
        "/api/v1/tools/apply_patch/tests",
        json={
            "agent_id": "default",
            "provider_ids": ["backup_3"],
            "repetitions": 1,
        },
    )
    assert start_response.status_code == 200, start_response.text
    started = start_response.json()["data"]
    run = await _wait_for_tool_test(client, started["run_id"])

    assert run["status"] == "completed", run
    assert run["progress"] == 100
    assert len(run["attempts"]) == 10
    assert len({attempt["case_id"] for attempt in run["attempts"]}) == 10
    assert all(attempt["tool_called"] is True for attempt in run["attempts"]), run
    assert all(
        attempt["execution_succeeded"] is True for attempt in run["attempts"]
    ), run
    assert all(attempt["passed"] is True for attempt in run["attempts"]), run
    assert run["providers"][0]["success_rate"] == 100

    result_file = (
        Path(e2e_workspace_root_path)
        / ".boxteam"
        / "tool_tests"
        / "apply_patch"
        / "run.json"
    )
    assert result_file.is_file()
    response_file = (
        Path(e2e_workspace_root_path)
        / ".boxteam"
        / "tool_tests"
        / "apply_patch"
        / "backup_3"
        / "apply_patch_update_single_file"
        / "response.json"
    )
    assert response_file.is_file()
