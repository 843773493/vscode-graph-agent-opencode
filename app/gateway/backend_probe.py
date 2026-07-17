from __future__ import annotations

import httpx


LOCAL_TOKEN = "local-dev-token"


async def read_workspace_root(backend_url: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{backend_url}/api/v1/workspace",
            headers={"X-Local-Token": LOCAL_TOKEN},
        )
        response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("远程后端工作区接口缺少 data 对象")
    root_path = data.get("root_path")
    if not isinstance(root_path, str) or not root_path.strip():
        raise ValueError("远程后端工作区接口缺少 root_path")
    return root_path.strip()
