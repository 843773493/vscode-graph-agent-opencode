"""真实插件持续发送 shutdown 正文，也不能延长关闭总期限。"""

from __future__ import annotations

import hashlib
import json
import time
from zipfile import ZipFile

import commentjson
import httpx
import psutil

from app.schemas.gateway.service_plugin import PluginSlotStatusDTO
from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugins import PYTHON_PLUGIN
from tests.support.plugin_package import install_plugin, native_plugin_target

pytest_plugins = ["tests.integration.gateway.test_service_plugins"]


def test_slow_shutdown_body_cannot_extend_stop_deadline(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = plugin_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    target = {"gateway_id": gateway_id}
    binding = {"package_id": "shutdown.deadline", "plugin_id": "probe", "package_version": "1.0.0", "enabled": True}
    archive_path = context.artifacts_dir / "shutdown-deadline.zip"
    source = PYTHON_PLUGIN.replace(
        "    return await call_next(request)",
        "    if request.url.path == '/internal/plugin/shutdown':\n"
        "        from fastapi.responses import StreamingResponse\n"
        "        async def drip():\n"
        "            for _ in range(120):\n"
        "                yield b' '\n"
        "                await asyncio.sleep(0.1)\n"
        "        return StreamingResponse(drip(), media_type='application/json')\n"
        "    return await call_next(request)",
    )
    assert source != PYTHON_PLUGIN
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps({
            "package_id": binding["package_id"], "version": binding["package_version"], "name": "关闭期限探针",
            "target": native_plugin_target("python"),
            "plugins": [{"id": "probe", "name": "探针", "instance_scope": "gateway", "runtime": "python",
                "entrypoint": ["entry.py"], "management_protocol_version": 1,
                "requires": [{"extension_point": "services", "version": 1}],
                "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}],
        }))
        archive.writestr("entry.py", source)
        archive.writestr("local_module.py", "VALUE = 1\n")
    install_plugin(lambda path, payload: client.post(path, json=payload), gateway_id=gateway_id,
        path=str(archive_path), expected_digest=hashlib.sha256(archive_path.read_bytes()).hexdigest())
    evidence: list[dict[str, object]] = []

    def save(entries: list[dict[str, object]]) -> None:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        before = client.get(path).raise_for_status().json()["data"]
        config = commentjson.loads(before["jsonc"])
        config.setdefault("plugins", {})["runtime"] = {"entries": entries}
        request_id = f"shutdown-deadline-{len(evidence)}"
        response = client.put(path, json={**target, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
            "base_file_digest": before["file_digest"], "base_layer_revision": before["layer_revision"],
            "base_layer_digest": before["layer_digest"], "expected_active_revision": before["active_revision"],
            "expected_active_digest": before["active_digest"], "idempotency_key": request_id},
            headers={"X-Request-ID": request_id})
        payload = response.raise_for_status().json()
        assert payload["request_id"] == response.headers["X-Request-ID"] == request_id
        assert payload["data"]["edit"]["state"] == "active"
        evidence.append({"request_id": request_id, "response": payload})

    def status() -> list[PluginSlotStatusDTO]:
        data = client.post("/api/gateway/service-plugins/status", json=target).raise_for_status().json()["data"]
        return [PluginSlotStatusDTO.model_validate(item) for item in data["plugins"]]

    try:
        save([binding])
        original, = status()
        assert original.state == "ready" and original.instance is not None
        started = time.monotonic()
        save([{**binding, "enabled": False}])
        elapsed = time.monotonic() - started
        stopped, = status()
        evidence.append({"elapsed_seconds": elapsed, "slot": stopped.model_dump(mode="json")})
        # 包含来源 CAS 和序列化的余量；12秒持续响应在旧实现中超过8秒总期限。
        assert elapsed < 10, evidence[-1]
        assert stopped.binding.enabled is False and stopped.state == "failed"
        assert stopped.instance == original.instance
        assert "shutdown" in (stopped.error or "") and "已核实退出" in (stopped.error or "")
        assert not psutil.pid_exists(original.instance.process_id)
        old_url = (f"/api/gateway/service-plugins/shutdown.deadline/probe/instances/"
            f"{original.instance.instance_id}/services/echo/value?package_version=1.0.0")
        assert client.get(old_url).status_code == 409
        save([])
        assert status() == []
        client.post("/api/gateway/service-plugins/uninstall", json={**target,
            "package_id": "shutdown.deadline", "package_version": "1.0.0"}).raise_for_status()
    finally:
        (context.artifacts_dir / "shutdown-deadline.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
