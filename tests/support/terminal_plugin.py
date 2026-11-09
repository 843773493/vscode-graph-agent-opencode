"""通过真实 Gateway 安装包并由原 Workspace 来源 CAS 启用 Terminal。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx


def copy_prepared_developer_tools_package(package_root: Path, boxteam_home: Path) -> Path:
    """将已准备的不可变包复制到隔离 Home，保留真实 Gateway runtime 启动。"""
    from app.gateway.plugins.packages import PluginPackageStore

    source = package_root.resolve(strict=True)
    source_store = PluginPackageStore(source.parent.parent)
    installed = source_store.get("boxteam.developer-tools", "0.1.0")
    if installed.root != source:
        raise ValueError("预备开发工具包路径与安装记录不一致")
    target_store = PluginPackageStore(boxteam_home / "plugins" / "packages")
    target = target_store.root / "boxteam.developer-tools" / "0.1.0"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target_store.get("boxteam.developer-tools", "0.1.0").root


async def enable_developer_tools_plugins(
    client: httpx.AsyncClient, *, gateway_id: str, workspace_id: str,
    plugin_ids: tuple[str, ...], artifacts_dir: Path,
    prepared_package: bool = False,
) -> dict[str, dict[str, object]]:
    """安装并启用指定开发工具插件，使用 Gateway 的正式配置与生命周期入口。"""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if not plugin_ids or len(set(plugin_ids)) != len(plugin_ids) or set(plugin_ids) - {"terminal", "browser", "debugging"}:
        raise ValueError(f"开发工具插件列表无效: {plugin_ids}")
    if not prepared_package:
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
        }, timeout=600)
        installed.raise_for_status()
    headers = {"X-BoxTeam-Workspace-Id": workspace_id}
    source_path = f"/api/v1/workspaces/{workspace_id}/config/sources/workspace_root_mutable_override"
    loaded = await client.get(source_path, headers=headers)
    loaded.raise_for_status()
    document = loaded.json()["data"]
    config = commentjson.loads(document["jsonc"])
    entries = config.setdefault("plugins", {}).setdefault("runtime", {}).setdefault("entries", [])
    selected = set(plugin_ids)
    entries[:] = [entry for entry in entries if entry["package_id"] != "boxteam.developer-tools"
                  or entry["plugin_id"] not in selected]
    entries.extend({"package_id": "boxteam.developer-tools", "package_version": "0.1.0",
                    "plugin_id": plugin_id, "enabled": True} for plugin_id in plugin_ids)
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
            slots = [item for item in status.json()["data"]["plugins"]
                     if item["binding"]["package_id"] == "boxteam.developer-tools"
                     and item["binding"]["plugin_id"] in selected
                     and item["binding"].get("workspace_id") == workspace_id]
            if len(slots) == len(selected) and all(slot["state"] == "ready" for slot in slots):
                return {str(slot["binding"]["plugin_id"]): slot for slot in slots}
            failed = [slot for slot in slots if slot["state"] == "failed"]
            if failed:
                raise RuntimeError(f"开发工具插件启动失败: {failed}")
            await asyncio.sleep(0.1)
