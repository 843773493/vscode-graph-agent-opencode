"""通过原配置入口验 Gateway 插件启动与关闭的提交顺序。"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import commentjson
import httpx
import psutil

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugin_streams import PLUGIN
from tests.support.plugin_package import install_plugin, native_plugin_target

pytest_plugins = ["tests.integration.gateway.test_service_plugins"]


def _archive(path: Path, *, gate_path: Path, starts_path: Path) -> None:
    manifest = {
        "manifest_version": 1,
        "package_id": "startup.close.probe",
        "version": "1.0.0",
        "name": "启动关闭屏障探针",
        "target": native_plugin_target("python"),
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "Probe",
            "instance_scope": "gateway",
            "runtime": "python",
            "entrypoint": ["server.py", str(starts_path), str(gate_path)],
            "management_protocol_version": 1,
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {
                "services": [{
                    "service_id": "events",
                    "service_type": "terminal",
                    "protocol": "http",
                }],
            },
        }],
    }
    source = PLUGIN.replace(
        "identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])",
        "identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as started:\n"
        "    started.write(json.dumps({'instance_id': identity['instance_id'], 'pid': os.getpid()}) + '\\n')",
    ).replace(
        "app = FastAPI()",
        "app = FastAPI()\n"
        "@app.middleware('http')\n"
        "async def hold_ready(request, call_next):\n"
        "    if request.url.path == '/internal/plugin/status':\n"
        "        while not os.path.exists(sys.argv[2]):\n"
        "            await asyncio.sleep(0.02)\n"
        "    return await call_next(request)",
    )
    if source == PLUGIN:
        raise AssertionError("测试插件未注入 ready 屏障")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("server.py", source)


def test_gateway_commits_close_after_start_and_never_leaves_late_ready_plugin(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = plugin_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    target = {"gateway_id": gateway_id}
    gate_path = context.artifacts_dir / "release-plugin-ready"
    starts_path = context.artifacts_dir / "plugin-starts.jsonl"
    archive_path = context.artifacts_dir / "startup-close-probe.zip"
    _archive(archive_path, gate_path=gate_path, starts_path=starts_path)
    install_plugin(
        lambda endpoint, payload: client.post(endpoint, json=payload),
        gateway_id=gateway_id,
        path=str(archive_path),
        expected_digest=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    )

    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    binding = {
        "package_id": "startup.close.probe",
        "package_version": "1.0.0",
        "plugin_id": "probe",
        "enabled": True,
    }
    evidence: list[dict[str, object]] = []

    def save(*, enabled: bool, request_id: str, idempotency_key: str,
             before_submit: Event | None = None) -> dict[str, object]:
        document = client.get(source_path).raise_for_status().json()["data"]
        config = commentjson.loads(document["jsonc"])
        config.setdefault("plugins", {})["runtime"] = {
            "entries": [{**binding, "enabled": enabled}],
        }
        payload = {
            "gateway_id": gateway_id,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(config, ensure_ascii=False),
            "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"],
            "idempotency_key": idempotency_key,
        }
        if before_submit is not None:
            before_submit.set()
        response = client.put(source_path, json=payload, headers={"X-Request-ID": request_id})
        value = response.json()
        evidence.append({"request_id": request_id, "status": response.status_code, "body": value})
        assert response.status_code == 200, value
        assert response.headers["X-Request-ID"] == value["request_id"] == request_id
        assert value["data"]["edit"]["state"] == "active", value
        return value["data"]

    def status() -> dict[str, object]:
        return client.post("/api/gateway/service-plugins/status", json=target).raise_for_status().json()["data"]

    close_started = Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            enable = workers.submit(save, enabled=True, request_id="plugin-start-enable",
                                   idempotency_key="plugin-start-enable")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                slots = status()["plugins"]
                slot = next((item for item in slots
                             if item["binding"]["package_id"] == binding["package_id"]), None)
                if slot is not None and slot["state"] == "starting":
                    break
                time.sleep(0.02)
            else:
                raise AssertionError(f"Gateway 未进入 starting: {status()}")
            assert not enable.done(), "未放开 ready 屏障前，开启响应不应先完成"

            def close() -> dict[str, object]:
                return save(enabled=False, request_id="plugin-start-close",
                            idempotency_key="plugin-start-close", before_submit=close_started)

            disable = workers.submit(close)
            assert close_started.wait(5)
            assert not disable.done(), "启动仍被屏障阻塞时，关闭提交应排在原配置 owner 后"
            assert status()["plugins"][0]["state"] == "starting"

            gate_path.write_text("ready\n", encoding="utf-8")
            enabled_result = enable.result(timeout=30)
            disabled_result = disable.result(timeout=30)
            assert enabled_result["edit"]["state"] == disabled_result["edit"]["state"] == "active"

        final = status()
        stopped, = [item for item in final["plugins"]
                    if item["binding"]["package_id"] == binding["package_id"]]
        assert stopped["binding"]["enabled"] is False
        assert stopped["state"] == "stopped"
        assert stopped["instance"] is None
        starts = [json.loads(line) for line in starts_path.read_text(encoding="utf-8").splitlines()]
        assert len(starts) == 1, starts
        assert not psutil.pid_exists(starts[0]["pid"]), starts
        assert [item["request_id"] for item in evidence] == [
            "plugin-start-enable", "plugin-start-close",
        ], evidence
    finally:
        gate_path.touch(exist_ok=True)
        (context.artifacts_dir / "startup-close.json").write_text(
            json.dumps({"evidence": evidence}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
