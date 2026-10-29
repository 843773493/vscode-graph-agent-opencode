"""真实 Gateway 验证用户 profile 故障、账户副作用与原入口恢复。"""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import ExitStack
from pathlib import Path
from traceback import format_exception

import httpx
import pytest
from fastapi import FastAPI

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


def create_profile_diagnostic_failure_app() -> FastAPI:
    """只装配可开关的真实日志写入故障；仍使用原Gateway应用与全部业务owner。"""
    from app.gateway.main import app

    control = Path(os.environ["BOXTEAM_TEST_PROFILE_SINK_CONTROL"])
    destination = Path(os.environ["BOXTEAM_TEST_PROFILE_SINK_DESTINATION"])
    audit = Path(os.environ["BOXTEAM_TEST_PROFILE_SINK_AUDIT"])

    class FailingLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not control.is_file():
                return
            assert record.exc_info is not None and record.exc_info[1] is not None
            primary = record.exc_info[1]
            with audit.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "logger": record.name, "error_type": type(primary).__name__,
                    "diagnostic": "".join(format_exception(primary)),
                }, ensure_ascii=False) + "\n")
            # 普通文件被当成父目录，open由本机OS真实抛出含日志位置的错误。
            with destination.open("a", encoding="utf-8"):
                pass

    handler = FailingLogHandler(level=logging.ERROR)
    for name in ("app.gateway.control.user_profile", "app.core.failures"):
        logging.getLogger(name).addHandler(handler)
    return app


@pytest.mark.asyncio
async def test_profile_failures_preserve_account_contract_and_recover(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(0),
            artifacts_dir=context.artifacts_dir,
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30,
        ) as client:
            created = await client.post("/api/gateway/users", json={"user_id": "profile-user", "display_name": "配置用户"})
            assert created.status_code == 200, created.text
            profile_root = context.boxteam_home / "state/gateway/users/profile-user"
            profile_path = profile_root / "profile.jsonc"
            original = profile_path.read_bytes()
            acquired = await client.post("/api/gateway/users/profile-user/access", json={"client_label": "profile-integration"})
            assert acquired.status_code == 200, acquired.text

            # 账户、profile 和租约由产品生成；磁盘操作只用于注入可复查的外部故障。
            blocked = profile_root.with_name("blocked-user")
            blocked.write_text("/private/profile-secret")
            processes.callback(blocked.unlink, missing_ok=True)
            rejected = await client.post("/api/gateway/users", json={"user_id": "blocked-user", "display_name": "创建失败"})
            assert rejected.status_code == 500, rejected.text
            assert "operation=initialize" in rejected.json()["detail"]
            assert str(blocked) not in rejected.text and "profile-secret" not in rejected.text
            accounts = await client.get("/api/gateway/users")
            assert [item["user_id"] for item in accounts.json()["data"]["items"]] == ["profile-user"]
            assert blocked.read_text() == "/private/profile-secret"
            evidence["initialize"] = rejected.json()

            for name, content in [
                ("missing", None), ("not-object", b"[]"), ("version", b'{"config_version":2}'),
                ("json", b'{"private":"/private/profile-secret",}'), ("utf8", b'\xff/private/profile-secret'),
            ]:
                if content is None:
                    profile_path.unlink()
                else:
                    profile_path.write_bytes(content)
                for method, endpoint, payload in [
                    ("GET", "/api/gateway/ui-settings", None),
                    ("PUT", "/api/gateway/ui-settings", {"layout": {"workbench_view": "gateway"}}),
                    ("GET", "/api/gateway/themes", None),
                    ("GET", "/api/gateway/ui-assets", None),
                ]:
                    failed = await client.request(method, endpoint, json=payload)
                    assert failed.status_code == 409, failed.text
                    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
                    assert str(profile_root) not in failed.text and "profile-secret" not in failed.text
                    assert profile_path.exists() == (content is not None)
                    if content is not None:
                        assert profile_path.read_bytes() == content
                    evidence[f"{name}-{method}-{endpoint}"] = failed.json()
                profile_path.write_bytes(original)
                recovered = await client.get("/api/gateway/ui-settings")
                assert recovered.status_code == 200, recovered.text

            assets = profile_root / "themes/assets"
            shutil.rmtree(assets)
            assets.write_text("/private/profile-secret")
            failed_assets = await client.get("/api/gateway/themes")
            assert failed_assets.status_code == 409, failed_assets.text
            assert "operation=theme-assets" in failed_assets.json()["detail"]
            assert str(assets) not in failed_assets.text and "profile-secret" not in failed_assets.text
            assets.unlink()
            updated = await client.put("/api/gateway/ui-settings", json={"layout": {"workbench_view": "gateway"}})
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["layout"]["workbench_view"] == "gateway"
            persisted = profile_path.read_bytes()
            assert json.loads(persisted)["display_name"] == "配置用户"
            assert not list(profile_root.glob(".profile.jsonc.*"))
            evidence["theme-assets"] = failed_assets.json()
            evidence["recovered"] = updated.json()

            occupied = await client.delete("/api/gateway/users/profile-user")
            assert occupied.status_code == 409 and profile_path.read_bytes() == persisted
            assert (await client.delete("/api/gateway/users/current")).status_code == 200
            shutil.rmtree(profile_root)
            profile_root.write_text("/private/profile-secret")
            failed_delete = await client.delete("/api/gateway/users/profile-user")
            assert failed_delete.status_code == 500, failed_delete.text
            assert "operation=delete" in failed_delete.json()["detail"]
            assert str(profile_root) not in failed_delete.text and "profile-secret" not in failed_delete.text
            accounts = await client.get("/api/gateway/users")
            assert accounts.status_code == 200 and accounts.json()["data"]["items"] == []
            assert profile_root.read_text() == "/private/profile-secret"
            evidence["delete"] = failed_delete.json()
            profile_root.unlink()
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(profile_path) in logs and str(blocked) in logs and str(assets) in logs
        assert "Traceback" in logs
        (context.artifacts_dir / "profile-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.asyncio
async def test_native_profile_and_log_failures_preserve_http_contract_and_recover(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    blocked_log = runtime / "blocked-log-directory"
    blocked_log.write_text("保留原日志位置阻塞文件", encoding="utf-8")
    control = runtime / "sink-enabled"
    audit = context.artifacts_dir / "profile-sink-diagnostics.jsonl"
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(0),
            artifacts_dir=context.artifacts_dir,
            asgi_app=f"{__name__}:create_profile_diagnostic_failure_app",
            extra_env={"BOXTEAM_TEST_PROFILE_SINK_CONTROL": str(control),
                       "BOXTEAM_TEST_PROFILE_SINK_DESTINATION": str(blocked_log / "service.log"),
                       "BOXTEAM_TEST_PROFILE_SINK_AUDIT": str(audit)},
        )
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30,
        ) as client:
            created = await client.post("/api/gateway/users", json={"user_id": "profile-user", "display_name": "配置用户"})
            assert created.status_code == 200, created.text
            assert (await client.post("/api/gateway/users/profile-user/access", json={"client_label": "sink-test"})).status_code == 200
            profile_root = context.boxteam_home / "state/gateway/users/profile-user"
            path = profile_root / "profile.jsonc"
            original = path.read_bytes()

            async def fail(method: str, endpoint: str, *, status: int, marker: str, payload=None) -> None:
                control.touch()
                try:
                    response = await client.request(method, endpoint, json=payload)
                finally:
                    control.unlink()
                assert response.status_code == status, response.text
                assert marker in response.json()["detail"], response.text
                assert "diagnostic_sink_error=NotADirectoryError" in response.json()["detail"]
                assert response.json()["request_id"] == response.headers["X-Request-ID"]
                assert str(profile_root) not in response.text and str(blocked_log) not in response.text
                assert blocked_log.read_text(encoding="utf-8") == "保留原日志位置阻塞文件"
                evidence[marker] = response.json()

            blocked_user = profile_root.with_name("blocked-user")
            blocked_user.write_text("保留原用户阻塞文件", encoding="utf-8")
            await fail("POST", "/api/gateway/users", status=500, marker="operation=initialize",
                       payload={"user_id": "blocked-user", "display_name": "创建失败"})
            accounts = await client.get("/api/gateway/users")
            assert [item["user_id"] for item in accounts.json()["data"]["items"]] == ["profile-user"]
            assert blocked_user.read_text(encoding="utf-8") == "保留原用户阻塞文件"
            blocked_user.unlink()

            for name, content, marker in (
                ("missing", None, "error_type=FileNotFoundError"),
                ("json", b'{"private":"/private/profile-secret",}', "error_type=JSONDecodeError"),
                ("utf8", b'\xff/private/profile-secret', "error_type=UnicodeDecodeError"),
            ):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                await fail("GET", "/api/gateway/ui-settings", status=409, marker=marker)
                assert path.exists() == (content is not None), name
                if content is not None:
                    assert path.read_bytes() == content
                path.write_bytes(original)
                recovered = await client.get("/api/gateway/ui-settings")
                assert recovered.status_code == 200, recovered.text
                assert path.read_bytes() == original

            assets = profile_root / "themes/assets"
            shutil.rmtree(assets)
            assets.write_text("保留原资源阻塞文件", encoding="utf-8")
            await fail("GET", "/api/gateway/themes", status=409, marker="operation=theme-assets")
            assert assets.read_text(encoding="utf-8") == "保留原资源阻塞文件"
            assets.unlink()
            assert (await client.get("/api/gateway/themes")).status_code == 200
            assert (await client.delete("/api/gateway/users/current")).status_code == 200
            shutil.rmtree(profile_root)
            profile_root.write_text("保留原删除阻塞文件", encoding="utf-8")
            await fail("DELETE", "/api/gateway/users/profile-user", status=500, marker="operation=delete")
            assert profile_root.read_text(encoding="utf-8") == "保留原删除阻塞文件"
            accounts = await client.get("/api/gateway/users")
            assert accounts.json()["data"]["items"] == []
            profile_root.unlink()
            recreated = await client.post("/api/gateway/users", json={"user_id": "profile-user", "display_name": "恢复用户"})
            assert recreated.status_code == 200, recreated.text
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [record["error_type"] for record in records] == [
        "FileExistsError", "FileNotFoundError", "JSONDecodeError", "UnicodeDecodeError", "FileExistsError", "NotADirectoryError",
    ]
    assert all("Traceback" in record["diagnostic"] for record in records)
    assert str(path) in records[1]["diagnostic"]
    (context.artifacts_dir / "profile-sink-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
