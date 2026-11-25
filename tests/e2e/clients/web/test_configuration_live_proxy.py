"""显式启用的真实代理、PTY、Provider 与正式会话验收。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import commentjson
import httpx
import pytest
from dotenv import dotenv_values

from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import e2e_ports_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import copy_prepared_developer_tools_package
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
# autouse 装具随后会改写 BOXTEAM_HOME；在收集阶段冻结真实验收来源。
LIVE_SOURCE_HOME = Path(os.getenv("BOXTEAM_HOME", str(Path.home() / ".boxteams"))).expanduser()


@pytest.fixture
def live_web_assets(request: pytest.FixtureRequest) -> Path:
    prepared = os.getenv("BOXTEAM_CONFIGURATION_LIVE_WEB_ASSETS")
    return Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")


@pytest.mark.skipif(os.getenv("BOXTEAM_CONFIGURATION_LIVE") != "1",
    reason="真实模型请求须显式设置 BOXTEAM_CONFIGURATION_LIVE=1")
async def test_configuration_live_proxy_pty_and_formal_model_entry(
    request: pytest.FixtureRequest,
    prepared_developer_tools_package: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_web_assets: Path,
) -> None:
    await _run_configuration_live_proxy(request, prepared_developer_tools_package, monkeypatch, assets=live_web_assets, scenario="live")


@pytest.mark.skipif(os.getenv("BOXTEAM_CONFIGURATION_SHELL") != "1",
    reason="真实启动环境验收须显式设置 BOXTEAM_CONFIGURATION_SHELL=1")
async def test_configuration_jsonc_preserves_pty_environment(
    request: pytest.FixtureRequest,
    prepared_developer_tools_package: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_web_assets: Path,
) -> None:
    await _run_configuration_live_proxy(request, prepared_developer_tools_package, monkeypatch, assets=live_web_assets, scenario="shell-environment")


async def _run_configuration_live_proxy(request: pytest.FixtureRequest, prepared_developer_tools_package: Path,
    monkeypatch: pytest.MonkeyPatch, *, assets: Path, scenario: str) -> None:
    shell_only = scenario == "shell-environment"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    project = context.project_root
    assert (assets / "version.json").is_file(), "须提供本次验收的正式 Workbench 构建"
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "真实产品验收需要 Chromium"
    strace = shutil.which("strace")
    assert strace, "本验收使用 connect-only strace 核验产品后代出网目标"
    source_env = Path(os.getenv("BOXTEAM_CONFIGURATION_LIVE_ENV_SOURCE", str(LIVE_SOURCE_HOME / "config/.env"))).expanduser()
    if not source_env.is_file():
        # 当前验收明确授权根 .env；常规测试不会隐式采用这个来源。
        source_env = project / ".env"
    values = dotenv_values(source_env, interpolate=False)
    proxy = values.get("HTTPS_PROXY") or values.get("https_proxy")
    assert proxy, "验收来源必须显式提供标准 HTTPS 代理"
    secret_name = os.getenv("BOXTEAM_CONFIGURATION_LIVE_SECRET", "OPENCODE_ZEN_API_KEY")
    if not shell_only:
        assert values.get(secret_name), "真实模型所需凭据在已授权来源中缺失"
    environment_digest = hashlib.sha256(json.dumps([[name, values.get(name)] for name in PROXY_VARIABLES],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=project)
    copied_env = home / "config/.env"
    shutil.copy2(source_env, copied_env)
    copied_env.chmod(0o600)
    request.addfinalizer(lambda: copied_env.unlink(missing_ok=True))
    source_env_digest = hashlib.sha256(source_env.read_bytes()).hexdigest()
    assert hashlib.sha256(copied_env.read_bytes()).hexdigest() == source_env_digest, "基线必须完整复制原始环境配置"
    # 排除 pytest conftest 加载根 .env 的影响，产品必须从独立 Home 加载。
    for variable in (*PROXY_VARIABLES, "NO_PROXY", "no_proxy", secret_name):
        monkeypatch.delenv(variable, raising=False)
    workspace = prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=project / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    )
    source = install_test_workspace_config(workspace_root=workspace,
        config_path=project / "configs/tests/workspace/default.jsonc", schema_path=project / "configs/workspace_schema.jsonc")
    ports = e2e_ports_for_file(Path(request.node.path))
    backend_port, gateway_port = ports.port(0), ports.port(20)
    failure_port = ports.port(21)
    base_url = f"http://127.0.0.1:{gateway_port}"
    document = commentjson.loads(source.read_text(encoding="utf-8"))
    business = document["settings"]["workspace"]
    business.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = base_url
    business.setdefault("plugins", {}).setdefault("runtime", {})["entries"] = [{"package_id": "boxteam.developer-tools",
        "package_version": "0.1.1", "plugin_id": "terminal", "enabled": True}]
    for agent in business["agents"].values():
        agent["model"]["fallbacks"] = []
    document["settings"]["network"] = {"proxy": {"mode": "environment"}}
    source.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copy_prepared_developer_tools_package(prepared_developer_tools_package, home)
    gateway_source = home / "config/gateway.jsonc"
    gateway_config = commentjson.loads(gateway_source.read_text(encoding="utf-8"))
    gateway_config["settings"].setdefault("gateway", {}).setdefault("plugins", {})["workspace_defaults"] = {"entries": []}
    gateway_source.write_text(json.dumps(gateway_config) + "\n", encoding="utf-8")
    proxy_url = urlsplit(proxy)
    evidence = {"phase": "starting", "env_source": str(source_env), "isolated_home": str(home),
        "env_copied_without_changes": True, "proxy": {"scheme": proxy_url.scheme,
            "host": proxy_url.hostname, "port": proxy_url.port}, "secret_environment_name": secret_name,
        "assets": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
        "scenario": scenario,
        "model_responses": "不调用模型" if shell_only else "真实外部Provider，无重放或替身",
        "socket_trace": "strace connect-only，包含Gateway新建后代"}
    evidence_path = context.artifacts_dir / "live-environment.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    original_popen = subprocess.Popen

    def traced_popen(command, *args, **kwargs):
        # -D 保持真实 Gateway 为 helper 的直接子进程，原监听归属检查仍然有效。
        traced = [strace, "-D", "-ff", "-ttt", "-e", "trace=connect", "-o", str(context.artifacts_dir / "connections"), *command]
        evidence["gateway_command"] = traced
        return original_popen(traced, *args, **kwargs)

    # 只包住既有 Gateway helper 的启动动作；产品代码与所有网络响应保持真实。
    with patch("tests.support.processes.gateway.subprocess.Popen", traced_popen):
        gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
            default_backend_url=None, port=gateway_port, boxteam_home=home,
            artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(assets),
                "BOXTEAM_DEFAULT_BACKEND_PORT": str(backend_port), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    try:
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, trust_env=False, timeout=90) as client:
            response = await client.get("/api/gateway/workspaces")
            response.raise_for_status()
            owner = response.json()["data"]["active_workspace"]
        fixture = {"baseUrl": base_url, "gatewayId": owner["gateway_id"], "workspaceId": owner["workspace_id"],
            "scenario": scenario, "environmentDigest": environment_digest,
            "endpoint": os.getenv("BOXTEAM_CONFIGURATION_LIVE_ENDPOINT", "https://opencode.ai/zen/v1"),
            "model": os.getenv("BOXTEAM_CONFIGURATION_LIVE_MODEL", "big-pickle"), "secretName": secret_name,
            "failureProxy": f"http://127.0.0.1:{failure_port}"}
        command = ["timeout", "480s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
            "bun", "tests/e2e/clients/web/configuration_live_proxy.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=project, capture_output=True,
                text=True, check=False, timeout=495, start_new_session=True,
                env=os.environ | temporary | {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                    "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir), "BOXTEAM_LIVE_FIXTURE": json.dumps(fixture)})
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        evidence.update(phase="browser-finished", browser_command=command, browser_exit=completed.returncode)
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{context.artifacts_dir}"
        result = json.loads((context.artifacts_dir / "live-result.json").read_text(encoding="utf-8"))
        assert result["phase"] == "completed" and result["pageErrors"] == []
        assert result["cleanupErrors"] == []
        connections = []
        for trace in context.artifacts_dir.glob("connections.*"):
            for line in trace.read_text(encoding="utf-8").splitlines():
                match = re.match(r'([\d.]+) connect\(.*sa_family=AF_INET, sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]+)"\)', line)
                if match:
                    connections.append({"at": float(match[1]), "pid": int(trace.suffix[1:]),
                        "port": int(match[2]), "host": match[3]})
                assert "sa_family=AF_INET6" not in line, "当前IPv4验收出现IPv6连接，须核对原始trace后扩展证据"
        proxy_connections = [item for item in connections if (item["host"], item["port"]) == (proxy_url.hostname, proxy_url.port)]
        external = [item for item in connections if not ipaddress.ip_address(item["host"]).is_loopback]
        if shell_only:
            assert not proxy_connections, "终端控制不得连接环境代理"
            assert not external, "无外网用户命令的终端场景只能直连本地控制面"
            assert connections, "必须保留实际控制连接证据"
            assert result["shellEnvironment"]["baselineDigest"] == environment_digest
            assert result["shellEnvironment"]["oldDigest"] == environment_digest
            assert result["shellEnvironment"]["newDigest"] == environment_digest
            assert result["probes"] == [] and result["sessions"] == []
            rejected = []
        else:
            assert proxy_connections, "PTY和模型调用必须真实连接到所选环境代理"
            assert all(item in proxy_connections for item in external), external
            rejected = [item for item in connections if item["at"] >= result["proxyFailure"]["startedAt"] / 1000]
            assert any(item["port"] == failure_port for item in rejected), "故障阶段必须真实尝试指定的拒绝代理"
            assert all(ipaddress.ip_address(item["host"]).is_loopback for item in rejected), "拒绝代理后不得改走真实代理或外网直连"
        (context.artifacts_dir / "socket-targets.json").write_text(json.dumps({
            "connections": connections, "external": external, "failure_phase": rejected,
            "boundary": "connect-only系统调用，不采集CONNECT报文；真实Home环境代理未改写",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert hashlib.sha256(copied_env.read_bytes()).hexdigest() == source_env_digest, "运行不得改写独立环境副本"
        assert hashlib.sha256(source_env.read_bytes()).hexdigest() == source_env_digest, "运行不得改写真实环境来源"
        evidence["phase"] = "completed"
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
