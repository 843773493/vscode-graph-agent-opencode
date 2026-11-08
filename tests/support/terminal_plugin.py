"""通过真实 Gateway 安装包并由原 Workspace 来源 CAS 启用 Terminal。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx


async def enable_terminal_plugin(
    client: httpx.AsyncClient, *, gateway_id: str, workspace_id: str, artifacts_dir: Path,
) -> dict[str, object]:
    """只装配本次隔离 Gateway；不替调用方启动 Backend 或维护另一份绑定。"""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    archive = artifacts_dir / f"developer-tools-{uuid4().hex}.zip"
    packed = await asyncio.to_thread(
        subprocess.run,
        ["bun", "scripts/plugins/pack-developer-tools.mjs", "--out", str(archive.resolve())],
        cwd=Path.cwd(), capture_output=True, text=True, check=False, timeout=60,
    )
    (archive.with_suffix(".build.log")).write_text(packed.stdout + packed.stderr, encoding="utf-8")
    packed.check_returncode()
    installed = await client.post("/api/gateway/service-plugins/install", json={
        "gateway_id": gateway_id, "path": str(archive.resolve()),
        "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }, timeout=90)
    installed.raise_for_status()
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    loaded = await client.get(source_path, headers=headers)
    loaded.raise_for_status()
    document = loaded.json()["data"]
    config = commentjson.loads(document["jsonc"])
    entries = config.setdefault("plugins", {}).setdefault("runtime", {}).setdefault("entries", [])
    entries[:] = [entry for entry in entries if (entry["package_id"], entry["plugin_id"])
                  != ("boxteam.developer-tools", "terminal")]
    entries.append({"package_id": "boxteam.developer-tools", "package_version": "0.1.0",
                    "plugin_id": "terminal", "enabled": True})
    saved = await client.put(source_path, headers=headers, json={
        "gateway_id": gateway_id, "workspace_id": workspace_id,
        "source_key": "workspace_root_mutable_override",
        "jsonc": json.dumps(config, ensure_ascii=False, indent=2),
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": uuid4().hex,
    })
    saved.raise_for_status()
    if saved.json()["data"]["edit"]["state"] != "active":
        raise RuntimeError(f"Terminal 插件配置未生效: {saved.text}")
    async with asyncio.timeout(30):
        while True:
            status = await client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})
            status.raise_for_status()
            slot = next((item for item in status.json()["data"]["plugins"]
                         if item["binding"]["package_id"] == "boxteam.developer-tools"
                         and item["binding"]["plugin_id"] == "terminal"
                         and item["binding"].get("workspace_id") == workspace_id), None)
            if slot is not None:
                if slot["state"] == "ready":
                    return slot
                if slot["state"] == "failed":
                    raise RuntimeError(f"Terminal 插件启动失败: {slot}")
            await asyncio.sleep(0.1)
