"""真实认证、模型工厂与 Agent HTTP 响应共用凭据错误合同。"""

from __future__ import annotations

import base64
import errno
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.agents.agent_factory import build_runtime_for_agent
from app.api.agents import router
from app.api.deps import get_agent_service, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.services.business.agent_service import AgentService
from app.services.infrastructure.config_service import ConfigService
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def auth_service(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    home = tmp_path / "private-home"
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
    token_dir = tmp_path / "private-token-directory"
    token_dir.mkdir()
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    target = token_dir / "auth.json"
    source = home / ".codex/auth.json"
    source.parent.mkdir(parents=True)
    claims = base64.urlsafe_b64encode(b'{"exp":2000000000}').decode().rstrip("=")
    tokens = {"access_token": f"header.{claims}.signature", "refresh_token": "test-refresh",
              "id_token": "test-id", "account_id": "test-account"}
    source.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
    record = {**tokens, "expires_at": 2_000_000_000}
    target.write_text(json.dumps(record), encoding="utf-8")
    config = tmp_path / "workspace.jsonc"
    config_text = Path("configs/workspace_inline.jsonc").read_text(encoding="utf-8")
    for name in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", config_text):
        monkeypatch.setenv(name, "test-only-not-sent")
    config.write_text(config_text, encoding="utf-8")
    config_service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config)
    service = AgentService(config_service=config_service)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_agent_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "local"
    return app, config_service, target, source, record


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "access_token", "tokens", "read_target", "read_migration", "read_refresh",
    "publish_migration", "publish_refresh",
])
async def test_auth_failure_keeps_agent_visible_and_recovers_same_entry(
    auth_service, monkeypatch, caplog, request, failure,
):
    app, config_service, target, source, record = auth_service
    artifact = "codex_auth" if failure in {"tokens", "read_migration", "read_refresh"} else "chatgpt_auth"
    path = target if artifact == "chatgpt_auth" else source
    io_failure = PermissionError(errno.EACCES, "读取失败 private-auth-marker", str(path))
    original_read = Path.read_text

    def read_text(current, *args, **kwargs):
        if current == path:
            raise io_failure
        return original_read(current, *args, **kwargs)

    def publish(*args, **kwargs):
        raise io_failure

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        initial = await client.get("/api/v1/agents/default")
        assert initial.status_code == 200, initial.text
        initial_provider = next(p for p in initial.json()["data"]["providers"] if p["provider_id"] == "backup_4")
        assert initial_provider["available"] is True, initial_provider
        if failure == "access_token":
            target.write_bytes(b"{}\n")
        elif failure == "tokens":
            source.write_bytes(b"{}\n")
            target.unlink()
        elif failure in {"read_migration", "publish_migration"}:
            target.unlink()
        elif failure in {"read_refresh", "publish_refresh"}:
            target.write_text(json.dumps({**record, "expires_at": 0}), encoding="utf-8")
        before = {file: file.read_bytes() if file.exists() else None for file in (source, target)}
        responses = []
        with monkeypatch.context() as patch:
            if failure.startswith("read_"):
                patch.setattr(Path, "read_text", read_text)
            elif failure.startswith("publish_"):
                patch.setattr(os, "link" if failure == "publish_migration" else "replace", publish)
            for endpoint in ("/api/v1/agents", "/api/v1/agents/default"):
                response = await client.get(endpoint, headers={"X-Request-ID": "auth-diagnostics-contract"})
                assert response.status_code == 200, response.text
                assert response.headers["X-Request-ID"] == response.json()["request_id"] == "auth-diagnostics-contract"
                data = response.json()["data"]
                default = next(a for a in data if a["agent_id"] == "default") if isinstance(data, list) else data
                provider = next(p for p in default["providers"] if p["provider_id"] == "backup_4")
                assert provider["available"] is False
                assert f"artifact={artifact}, stage=" in provider["configuration_error"]
                assert default["agent_id"] == "default" and len(default["providers"]) > 1
                assert str(source) not in response.text and str(target) not in response.text
                assert "private-auth-marker" not in response.text
                responses.append(response.json())
            with pytest.raises(RuntimeError) as caught:
                build_runtime_for_agent("default", config_service, preferred_provider_id="backup_4")
            assert "provider_id=backup_4" in str(caught.value)
            assert f"artifact={artifact}, stage=" in str(caught.value)
            assert str(path) not in str(caught.value)
            if failure.startswith(("read_", "publish_")):
                assert type(caught.value.__cause__) is PermissionError
                assert caught.value.__cause__.__cause__ is io_failure
        assert {file: file.read_bytes() if file.exists() else None for file in (source, target)} == before
        assert str(path) in caplog.text
        if failure.startswith(("read_", "publish_")):
            assert any(r.exc_info and r.exc_info[1] is io_failure for r in caplog.records)
        assert list(target.parent.glob(".auth.*.tmp")) == []
        if not failure.startswith("publish_"):
            target.write_text(json.dumps(record), encoding="utf-8")
        if failure == "tokens":
            source.write_text(json.dumps({"tokens": {key: value for key, value in record.items() if key != "expires_at"}}), encoding="utf-8")
        recovered = await client.get("/api/v1/agents/default")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["data"] == initial.json()["data"]
        assert json.loads(target.read_bytes()) == record
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        (context.artifacts_dir / f"auth-errors-{failure}.json").write_text(json.dumps({
            "responses": responses, "recovered": recovered.json(),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
