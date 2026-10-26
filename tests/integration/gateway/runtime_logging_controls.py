"""通过真实 HTTP 与 Launcher 既有 socket 核实进程日志控制。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import commentjson
import httpx

from app.core.logging_contract import load_runtime_log_contract


def verify_runtime_logging_reload(gateway_url: str, token: str, home: Path, *, artifacts: Path) -> None:
    """只编辑用户来源，由真实 watcher、配置事务和进程 owner 完成状态转换。"""
    path = home / "config" / "gateway.jsonc"
    original = path.read_text(encoding="utf-8")
    document = commentjson.loads(original)
    observed = []
    with httpx.Client(timeout=5, trust_env=False, headers={"X-Local-Token": token}) as client:
        target = next(item for item in client.get(f"{gateway_url}/api/gateway/workspaces").json()["data"]["items"] if item["active"])
        urls = {"gateway": f"{gateway_url}/api/gateway/internal/runtime-logging"}
        for role, service in (("backend", "workspace_api"), ("terminal", "terminal_manager"), ("browser", "browser_manager")):
            suffix = "/api/v1/internal/runtime-logging" if role == "backend" else "/internal/runtime-logging"
            urls[role] = target["services"][service]["local_url"].rstrip("/") + suffix

        def states():
            result = {role: client.get(url).raise_for_status().json()["data"] for role, url in urls.items()}
            result["launcher"] = _launcher_control(home, {"action": "status", "token": token})
            return result

        before = states()
        original_level, original_pretty = before["gateway"]["level"], before["gateway"]["pretty"]

        def wait_for_settings(level: str, pretty: bool) -> None:
            deadline = time.monotonic() + 20
            last = None
            while time.monotonic() < deadline:
                last = client.get(f"{gateway_url}/api/gateway/config/reload-status").raise_for_status().json()["data"]
                if last["state"] in {"rejected", "conflict", "recovery_required"}:
                    raise AssertionError(last)
                actual = states()
                if last["state"] == "active" and all(
                    state["applied_revision"] == last["revision"] and state["level"] == level and state["pretty"] is pretty
                    for state in actual.values()
                ):
                    for role, state in actual.items():
                        assert state == before[role] | {"applied_revision": last["revision"], "level": level, "pretty": pretty}, role
                    observed.append({"revision": last["revision"], "processes": actual})
                    return
                time.sleep(0.05)
            raise AssertionError(f"产品日志热应用未结算: {last}")

        try:
            for level, pretty in (("debug", False), ("warning", True)):
                document["logging"] = {"runtime": {"level": level, "pretty": pretty}}
                path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                wait_for_settings(level, pretty)
        finally:
            path.write_text(original, encoding="utf-8")
            wait_for_settings(original_level, original_pretty)
        assert observed[-1]["revision"] == before["gateway"]["applied_revision"]
        debug_revision = observed[0]["revision"]
        warning_revision = observed[1]["revision"]
        contract = load_runtime_log_contract()
        for role, state in before.items():
            log_path = contract.path_for(contract.root_for(home), "gateway") if role == "gateway" else Path(state["log_sink"]["path"])
            text = log_path.read_text(encoding="utf-8")
            assert text.count(f"应用产品日志设置: revision={debug_revision}") == 1, role
            assert f"应用产品日志设置: revision={warning_revision}" not in text, role
    (artifacts / "runtime-logging-reload.json").write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")


def _launcher_control(home: Path, payload: dict[str, object]) -> dict[str, object]:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", """
          import { readFileSync } from 'node:fs';
          import { gatewaySupervisorControlAddress, requestGatewaySupervisorControl }
            from './packages/launcher/src/gateway/control-channel.mjs';
          const input = JSON.parse(readFileSync(0, 'utf8'));
          try {
            const result = await requestGatewaySupervisorControl({
              address: gatewaySupervisorControlAddress(input.home), payload: input.payload, timeoutMs: 5000,
            });
            if (!result.handled) throw new Error('Launcher control 未监听');
            process.stdout.write(JSON.stringify(result.data));
          } catch (error) { process.stdout.write(JSON.stringify({ error: error.message })); }
        """],
        input=json.dumps({"home": str(home), "payload": {"operation": "runtime_logging", **payload}}),
        capture_output=True, text=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def verify_runtime_logging_controls(
    gateway_url: str, token: str, home: Path, *, launcher_pid: int, artifacts: Path,
) -> dict[str, dict[str, object]]:
    states: dict[str, dict[str, object]] = {}
    contract = load_runtime_log_contract()
    log_root = contract.root_for(home)
    with httpx.Client(timeout=5, trust_env=False) as client:
        workspaces = client.get(f"{gateway_url}/api/gateway/workspaces", headers={"X-Local-Token": token})
        workspaces.raise_for_status()
        target = next(item for item in workspaces.json()["data"]["items"] if item["active"])
        urls = {"gateway": f"{gateway_url}/api/gateway/internal/runtime-logging"}
        for name, service in (("backend", "workspace_api"), ("terminal", "terminal_manager"), ("browser", "browser_manager")):
            base = target["services"][service]["local_url"]
            assert base, (name, target["services"])
            suffix = "/api/v1/internal/runtime-logging" if name == "backend" else "/internal/runtime-logging"
            urls[name] = f"{base.rstrip('/')}{suffix}"

        authority_revision = client.get(urls["gateway"], headers={"X-Local-Token": token}).json()["data"]["applied_revision"]
        assert isinstance(authority_revision, str) and authority_revision
        for service, url in urls.items():
            headers = {"X-Local-Token": token}
            for invalid in (None, "wrong-token", "local-dev-token"):
                response = client.get(url, headers={} if invalid is None else {"X-Local-Token": invalid})
                assert response.status_code == 401, (service, response.text)
            assert client.get(url, headers=headers | {"Origin": "http://localhost"}).status_code == 403
            initial = client.get(url, headers=headers)
            initial.raise_for_status()
            before = initial.json()["data"]
            assert before["service"] == service
            assert before["applied_revision"] == authority_revision
            if service in {"backend", "gateway"}:
                assert initial.headers["X-Request-ID"] == initial.json()["request_id"]
            payload = {"runtime_instance_id": before["runtime_instance_id"], "expected_revision": before["applied_revision"],
                           "revision": f"control-{service}-debug", "level": "DEBUG", "pretty": False}
            for invalid in ({"pretty": "false"}, {"pretty": 0}, {"level": "warn"},
                            {"expected_revision": 1}, {"revision": " "}, {"unknown": True}):
                assert client.post(url, headers=headers, json=payload | invalid).status_code == 422
            applied = client.post(url, headers=headers, json=payload)
            applied.raise_for_status()
            debug = applied.json()["data"]
            assert debug == before | {"level": "debug", "pretty": False, "applied_revision": payload["revision"]}
            assert client.post(url, headers=headers, json=payload).json()["data"] == debug
            for conflict in ({"runtime_instance_id": "old-instance"}, {"revision": "late"}, {"pretty": True}):
                assert client.post(url, headers=headers, json=payload | conflict).status_code == 409
            assert client.get(url, headers=headers).json()["data"] == debug
            warning = payload | {"revision": f"control-{service}-warning", "expected_revision": debug["applied_revision"], "level": "warning", "pretty": True}
            assert client.post(url, headers=headers, json=warning).json()["data"]["level"] == "warning"
            assert client.post(url, headers=headers, json=payload).status_code == 409
            restore = payload | {"revision": before["applied_revision"], "expected_revision": warning["revision"], "level": before["level"], "pretty": before["pretty"]}
            final = client.post(url, headers=headers, json=restore).json()["data"]
            assert final["pid"] == before["pid"] and final["runtime_instance_id"] == before["runtime_instance_id"]
            states[service] = final
            log_path = (contract.path_for(log_root, "gateway") if service == "gateway"
                        else contract.path_for(log_root, service, httpx.URL(url).port))
            if service != "gateway":
                sink = before["log_sink"]
                assert sink["path"] == str(log_path) and sink["source"] == "gateway"
                assert sink["role"] == service
                identity = log_path.stat()
                assert (sink["device"], sink["inode"]) == (str(identity.st_dev), str(identity.st_ino))
                ownership = json.loads(log_path.with_name(log_path.name + ".owner.json").read_text())
                assert ownership["owner_id"] == sink["owner_id"]
                assert ownership["phase"] == "process" and ownership["pid"] == before["pid"]
            text = log_path.read_text(encoding="utf-8")
            assert text.count(f"应用产品日志设置: revision=control-{service}-debug") == 1
            assert f"应用产品日志设置: revision=control-{service}-warning" not in text
            assert token not in text

    for invalid in (None, "wrong-token", "local-dev-token"):
        assert "invalid runtime control token" in str(_launcher_control(home, {"action": "status", "token": invalid})["error"])
    before = _launcher_control(home, {"action": "status", "token": token})
    assert before["service"] == "launcher" and before["pid"] == launcher_pid
    assert before["log_sink"]["path"] == str(contract.path_for(log_root, "launcher"))
    assert before["log_sink"]["role"] == "launcher"
    assert before["applied_revision"] == authority_revision
    payload = {"runtime_instance_id": before["runtime_instance_id"], "expected_revision": before["applied_revision"],
                   "revision": "control-launcher-debug", "level": "debug", "pretty": False}
    debug = _launcher_control(home, {"action": "apply", "token": token, "payload": payload})
    assert debug["level"] == "debug" and debug["applied_revision"] == payload["revision"]
    assert _launcher_control(home, {"action": "apply", "token": token, "payload": payload}) == debug
    assert "运行实例已变化" in str(_launcher_control(home, {"action": "apply", "token": token, "payload": payload | {"runtime_instance_id": "old"}})["error"])
    restore = payload | {"revision": before["applied_revision"], "expected_revision": payload["revision"], "level": before["level"], "pretty": before["pretty"]}
    states["launcher"] = _launcher_control(home, {"action": "apply", "token": token, "payload": restore})
    assert states["launcher"]["pid"] == launcher_pid
    launcher_text = contract.path_for(log_root, "launcher").read_text(encoding="utf-8")
    assert launcher_text.count("应用产品日志设置: revision=control-launcher-debug") == 1
    assert len({state["runtime_instance_id"] for state in states.values()}) == 5
    (artifacts / "runtime-logging-control.json").write_text(json.dumps(states, indent=2) + "\n", encoding="utf-8")
    return states
