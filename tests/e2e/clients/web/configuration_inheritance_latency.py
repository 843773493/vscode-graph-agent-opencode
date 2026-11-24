"""独立配置性能验收入口；显式选择隔离目录和本批正式 Workbench 产物。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from configs.installer import install_user_configuration
from configs.runtime import read_jsonc_object
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePorts
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import prepare_test_workspace


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(root: Path, assets: Path) -> dict[str, object]:
    paths = [*root.glob("configs/inheritance/*.py"), *root.glob("app/services/infrastructure/config/*.py"),
        *root.glob("app/services/infrastructure/config_service/*.py"), root / "app/api/config.py",
        root / "app/schemas/internal_v2/config.py", root / "app/schemas/config_nodes.py",
        root / "app/runtime/litellm_runtime.py", root / "app/runtime/model_metadata.py",
        root / "app/core/trace_middleware.py", root / "app/gateway/server/workspace_proxy.py",
        root / "proto/boxteam/workspace/v2/public.proto",
        *root.glob("src/clients/workbench/src/settings/*"), *root.glob("src/clients/web/src/components/settings/*"),
        root / "src/clients/web/src/api/configSources.ts", root / "configs/workspace_inline.jsonc",
        root / "configs/workspace_schema.jsonc", Path(__file__).resolve(),
        root / "tests/e2e/clients/web/configuration_inheritance_latency.mjs"]
    source = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths if path.is_file()}
    return {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "source_sha256": source, "assets": str(assets),
        "asset_sha256": {str(path.relative_to(assets)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in assets.rglob("*") if path.is_file() and (path.suffix in {".js", ".json", ".css", ".html"})}}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--http-only", action="store_true")
    mode.add_argument("--first-settings-only", action="store_true")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    output = args.output_root.resolve()
    assert output.is_relative_to(root / "out/tests"), "性能数据必须位于out/tests"
    assets = args.assets.resolve(strict=True)
    assert (assets / "version.json").is_file() and (assets / "out/main.js").is_file()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    artifacts = output / "artifacts" / stamp
    artifacts.mkdir(parents=True)
    workspace = prepare_test_workspace(workspace_root=output / "workspace",
        template_root=root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",))
    home = artifacts / "boxteam-home"
    install_user_configuration(config_root=home / "config", profile="default", project_root=root, workspace_root=workspace)
    ports = TestFilePorts(Path(__file__).resolve(), output / "artifacts/port-leases")
    gateway_port, backend_port = ports.port("gateway"), ports.port("backend")
    base_url = f"http://127.0.0.1:{gateway_port}"
    inline = read_jsonc_object(root / "configs/workspace_inline.jsonc")
    builtin_ids = list(inline["settings"]["workspace"]["llm"]["providers"])
    provider_ids = builtin_ids + [f"latency-{index:02d}" for index in range(20 - len(builtin_ids))]
    model_ids = ["default", *(f"model-{index:02d}" for index in range(1, 10))]
    shared = workspace / ".boxteam/latency_shared.jsonc"
    write_json(shared, {"config_version": 4, "settings": {}, "profiles": {
        "provider": {"extends": "context:workspace", "settings": {
            "model_options": {"endpoint": "https://latency.invalid/v1", "api_key": "", "custom_llm_provider": "openai",
                "api_mode": {"protocol": "chat_completions", "supports_reasoning": {}}},
            "network": {"proxy": {"mode": "direct"}}}},
        "model": {"extends": "#/profiles/provider", "settings": {
            "model_options": {"request_options": {"parameters": {"temperature": 0.25}}}}},
    }})
    providers = {provider_id: {"name": f"Latency {index:02d}",
        "config_ref": "./latency_shared.jsonc#/profiles/provider", "models": {model_id: {
            "model": f"latency-upstream-{model_index:02d}", "config_ref": "./latency_shared.jsonc#/profiles/model",
        } for model_index, model_id in enumerate(model_ids)}} for index, provider_id in enumerate(provider_ids)}
    source = workspace / ".boxteam/workspace.jsonc"
    write_json(source, {"config_version": 4, "extends": "user:workspace_local", "settings": {"workspace": {
        "llm": {"providers": providers}, "agents": {agent_id: {"model": {
            "primary": {"provider_id": "primary", "model_id": "default"}, "fallbacks": []
        }} for agent_id in inline["settings"]["workspace"]["agents"]},
        "runtime": {"gateway": {"connection": {"url": base_url}}},
    }}})
    gateway_source = home / "config/gateway.jsonc"
    gateway_config = read_jsonc_object(gateway_source)
    gateway_config.setdefault("settings", {}).setdefault("gateway", {}).setdefault("plugins", {})["workspace_defaults"] = {"entries": []}
    write_json(gateway_source, gateway_config)
    for index in range(5):
        (workspace / f"latency-editor-{index}.txt").write_text(f"CONFIG LATENCY EDITOR {index}\n" + "x\n" * 40, encoding="utf-8")
    sources = [shared, source, home / "config/workspace.jsonc", home / "config/workspace_local.jsonc",
        root / "configs/workspace_inline.jsonc"]
    manifest = artifacts / "runtime-manifest.json"
    write_json(manifest, {"schema_version": 1, "distribution": "source-development",
        "version": json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]})
    probe = artifacts / "source-read-probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text('''import json, os, sys, time
_files = set(json.loads(os.environ["BOXTEAM_CONFIG_LATENCY_SOURCES"]))
_fd = os.open(os.environ["BOXTEAM_CONFIG_LATENCY_READ_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
def _audit(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.path.abspath(os.fsdecode(args[0]))
        if path in _files:
            os.write(_fd, (json.dumps({"path": path, "pid": os.getpid(), "wallMs": time.time_ns() / 1000000,
                "mode": args[1], "flags": args[2]}) + "\\n").encode())
sys.addaudithook(_audit)
''', encoding="utf-8")
    write_json(artifacts / "before-source.json", fingerprint(root, assets))
    write_json(artifacts / "workload.json", {"providers": 20, "models_per_provider": 10, "shared_ancestor": str(shared),
        "provider_ids": provider_ids, "model_ids": model_ids, "editor_count": 5 if args.http_only else 6,
        "workspace": str(workspace), "home": str(home), "gateway_port": gateway_port, "backend_port": backend_port,
        "command": sys.argv})
    gateway = None
    try:
        gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace, default_backend_url=None,
            port=gateway_port, boxteam_home=home, artifacts_dir=artifacts / "gateway", extra_env={
                "BOXTEAM_WEB_ASSETS": str(assets), "BOXTEAM_DEFAULT_BACKEND_PORT": str(backend_port),
                "BOXTEAM_RUNTIME_MANIFEST": str(manifest),
                "LITELLM_LOCAL_MODEL_COST_MAP": "true", "PYTHONPATH": os.pathsep.join([str(probe), str(root), os.getenv("PYTHONPATH", "")]),
                "BOXTEAM_CONFIG_LATENCY_SOURCES": json.dumps([str(path) for path in sources]),
                "BOXTEAM_CONFIG_LATENCY_READ_LOG": str(artifacts / "source-reads.jsonl"),
            })
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, trust_env=False, timeout=90) as client:
            response = await client.get("/api/gateway/workspaces")
            response.raise_for_status()
            owner = response.json()["data"]["active_workspace"]
        fixture = {"baseUrl": base_url, "gatewayId": owner["gateway_id"], "workspaceId": owner["workspace_id"],
            "providers": provider_ids, "models": model_ids, "httpOnly": args.http_only,
            "firstSettingsOnly": args.first_settings_only}
        chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
        assert chromium, "配置性能验收需要真实Chromium"
        command = ["timeout", "180s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
            "bun", "tests/e2e/clients/web/configuration_inheritance_latency.mjs"]
        with chromium_temporary_environment(artifacts / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=root, timeout=195, capture_output=True,
                text=True, check=False, env=os.environ | temporary | {
                    "BOXTEAM_CONFIG_LATENCY_FIXTURE": json.dumps(fixture), "BOXTEAM_BROWSER_ARTIFACTS": str(artifacts),
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium})
        (artifacts / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (artifacts / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        write_json(artifacts / "browser-process.json", {"command": command, "exit_code": completed.returncode})
        observed = json.loads((artifacts / "latency.json").read_text(encoding="utf-8"))
        reads = [json.loads(line) for line in (artifacts / "source-reads.jsonl").read_text(encoding="utf-8").splitlines()]
        warm_reads = [item for item in reads if any(sample["wallStartedMs"] <= item["wallMs"] <= sample["wallFinishedMs"]
            for sample in observed["httpSamples"] if sample["phase"] == "warm-inspection")]
        write_json(artifacts / "warm-source-reads.json", {"count": len(warm_reads), "reads": warm_reads,
            "observed_files": [str(path) for path in sources], "boundary": "Python audit open事件；仅统计20次暖inspection完整请求窗口"})
        assert not warm_reads, f"暖inspection读取父文件: {warm_reads}"
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n产物: {artifacts}"
        print(json.dumps({"artifacts": str(artifacts), "phase": observed["phase"], "warm": observed.get("warm"),
            "coldMs": observed.get("coldMs"), "settingsMs": observed.get("settingsMs"), "warmSourceReads": len(warm_reads)}, ensure_ascii=False))
    finally:
        if gateway is not None:
            await asyncio.to_thread(close_gateway_process, gateway)
        ports.close()
        write_json(artifacts / "after-source.json", fingerprint(root, assets))


if __name__ == "__main__":
    asyncio.run(main())
