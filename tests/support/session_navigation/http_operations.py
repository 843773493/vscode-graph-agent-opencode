from __future__ import annotations

import asyncio
import uuid
from typing import Literal

import httpx

NAVIGATION_OPERATION_WAIT_SECONDS = 30

_TERMINAL_STATES = {
    "committed",
    "rejected",
    "cancelled",
    "dependency_failed",
}
_PARENT_UNSET = object()


def _response_data(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise TypeError(f"API 响应缺少 data 对象: {response.text}")
    return payload["data"]


async def wait_for_navigation_operation(
    client: httpx.AsyncClient,
    operation_id: str,
    *,
    wait_for_settlement: bool = False,
) -> dict[str, object]:
    """等待终态；同一个期限覆盖请求、轮询间隔及可选的物理 settlement。"""
    timeout = asyncio.timeout(NAVIGATION_OPERATION_WAIT_SECONDS)
    try:
        async with timeout:
            while True:
                response = await client.get(
                    "/api/v1/session-catalog/operations",
                    params={"operation_id": operation_id},
                )
                assert response.status_code == 200, response.text
                data = _response_data(response)
                items = data.get("items")
                if not isinstance(items, list):
                    raise TypeError(f"导航 operation 状态缺少 items: {response.text}")
                receipt = next(
                    (
                        item
                        for item in items
                        if isinstance(item, dict)
                        and item.get("operation_id") == operation_id
                    ),
                    None,
                )
                if receipt is not None:
                    state = receipt.get("state")
                    pending_delete_settlement = (
                        wait_for_settlement
                        and state == "committed"
                        and receipt.get("kind") in {"delete_folder", "delete_session"}
                        and receipt.get("settlement") == "pending"
                    )
                    if state in _TERMINAL_STATES and not pending_delete_settlement:
                        return receipt
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        if not timeout.expired():
            raise
        detail = "物理 settlement 未到终态" if wait_for_settlement else "未到终态"
        raise AssertionError(f"导航 operation {detail}: {operation_id}") from error


async def enqueue_navigation_operation(
    client: httpx.AsyncClient,
    kind: Literal[
        "create_folder",
        "rename_node",
        "move_node",
        "delete_folder",
        "delete_session",
    ],
    *,
    target_node_id: str | None = None,
    name: str | None = None,
    parent_node_id: str | None | object = _PARENT_UNSET,
    recursive: bool = False,
) -> str:
    """提交导航 intent，并返回 durable acceptance 回显的 operation ID。"""
    snapshot_response = await client.get("/api/v1/session-catalog/snapshot")
    assert snapshot_response.status_code == 200, snapshot_response.text
    snapshot = _response_data(snapshot_response)
    catalog_revision = snapshot.get("catalog_revision")
    if not isinstance(catalog_revision, int):
        raise TypeError(f"导航快照缺少 catalog_revision: {snapshot_response.text}")

    operation_id = f"op_{uuid.uuid4().hex}"
    intent: dict[str, object] = {
        "client_operation_id": operation_id,
        "client_sequence": 1,
        "kind": kind,
        "base_catalog_revision": catalog_revision,
    }
    if kind == "create_folder":
        if name is None or target_node_id is not None:
            raise ValueError("create_folder 需要 name 且不接受 target_node_id")
        intent["name"] = name
        if parent_node_id is not _PARENT_UNSET:
            intent["parent_node_id"] = parent_node_id
    else:
        if target_node_id is None:
            raise ValueError(f"{kind} 缺少 target_node_id")
        if kind in ("rename_node", "move_node"):
            export_response = await client.get("/api/v1/session-catalog/export")
            assert export_response.status_code == 200, export_response.text
            export = _response_data(export_response)
            nodes = export.get("items")
            if not isinstance(nodes, list):
                raise TypeError(f"目录导出缺少 items: {export_response.text}")
            node = next(
                (
                    item
                    for item in nodes
                    if isinstance(item, dict)
                    and item.get("node_id") == target_node_id
                ),
                None,
            )
            if node is None:
                raise AssertionError(f"目录中找不到目标节点: {target_node_id}")
            node_revision = node.get("node_revision")
            if not isinstance(node_revision, int):
                raise TypeError(f"目录节点缺少权威 revision: {target_node_id}")
            intent["expected_revision"] = node_revision
        intent["target_node_id"] = target_node_id
        if kind == "rename_node":
            if name is None:
                raise ValueError("rename_node 缺少 name")
            intent["name"] = name
        elif kind == "move_node":
            if parent_node_id is _PARENT_UNSET:
                raise ValueError("move_node 必须显式给出 parent_node_id")
            intent["parent_node_id"] = parent_node_id
        elif kind == "delete_folder":
            intent["recursive"] = recursive
        elif recursive:
            raise ValueError("delete_session 不接受 recursive")

    enqueue_response = await client.post(
        "/api/v1/session-catalog/operations:enqueue",
        json={"intents": [intent]},
    )
    assert enqueue_response.status_code == 202, enqueue_response.text
    accepted = _response_data(enqueue_response)
    receipts = accepted.get("receipts")
    if not isinstance(receipts, list) or not any(
        isinstance(receipt, dict)
        and receipt.get("operation_id") == operation_id
        for receipt in receipts
    ):
        raise AssertionError(f"durable acceptance 未回显 operation: {enqueue_response.text}")
    return operation_id


async def submit_navigation_operation(
    client: httpx.AsyncClient,
    kind: Literal[
        "create_folder",
        "rename_node",
        "move_node",
        "delete_folder",
        "delete_session",
    ],
    *,
    target_node_id: str | None = None,
    name: str | None = None,
    parent_node_id: str | None | object = _PARENT_UNSET,
    recursive: bool = False,
    wait_for_settlement: bool = False,
) -> dict[str, object]:
    operation_id = await enqueue_navigation_operation(
        client,
        kind,
        target_node_id=target_node_id,
        name=name,
        parent_node_id=parent_node_id,
        recursive=recursive,
    )
    return await wait_for_navigation_operation(
        client,
        operation_id,
        wait_for_settlement=wait_for_settlement,
    )


__all__ = [
    "enqueue_navigation_operation",
    "submit_navigation_operation",
    "wait_for_navigation_operation",
]
