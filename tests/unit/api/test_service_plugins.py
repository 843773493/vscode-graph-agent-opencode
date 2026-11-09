"""创建默认值使用原配置候选重试，失败状态由真实 writer 产生。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from app.api.service_plugins import initialize_workspace_plugins
from app.container import AppContainer
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.gateway.service_plugin_workspace import WorkspacePluginCreation
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

GATEWAY = "gateway_" + "a" * 32


@pytest.fixture
async def creation_config(empty_workspace_scope: tuple[str, Path], tmp_path: Path, monkeypatch):
    import app.services.infrastructure.config_service.config_reload as reload_module
    import app.services.infrastructure.config_service.config_source_layers as source_layers

    workspace_id, root = empty_workspace_scope
    directory = tmp_path / "configuration"
    directory.mkdir()
    inline = directory / "workspace_inline.jsonc"
    inline.write_bytes(Path("configs/workspace_inline.jsonc").read_bytes())
    (directory / "workspace_schema.jsonc").write_bytes(Path("configs/workspace_schema.jsonc").read_bytes())
    monkeypatch.setattr(source_layers, "get_user_workspace_schema_path", lambda: directory / "absent.jsonc")
    path = root / ".boxteam/workspace.jsonc"
    path.write_text("{}\n")
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"detail": "controlled validation unavailable"})
        return httpx.Response(200, json={"code": 0, "data": True, "request_id": request.headers["X-Request-ID"]})

    client = httpx.AsyncClient
    monkeypatch.setattr(reload_module.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with WorkspaceStateStore(workspace_root=root) as store:
        service = ConfigService(config_dir=directory, inline_config_path=inline, config_path=directory / "workspace.jsonc",
            workspace_root=root, workspace_state_store=store)
        service.get_snapshot()
        service.validate_workspace_config()
        container = Mock(spec=AppContainer)
        container.config_service, container.workspace_id = service, workspace_id
        token = set_current_gateway_id(GATEWAY)
        try:
            yield container, service, store, path, calls
        finally:
            reset_current_gateway_id(token)
            await service.close()


@pytest.fixture
def creation() -> WorkspacePluginCreation:
    return WorkspacePluginCreation(intent_id="original-create", revision="frozen-template-v1", bindings=[
        PluginBinding(package_id="probe.defaults", plugin_id="probe", package_version="1.0.0", enabled=True),
    ])


async def test_rejected_initialization_retries_same_candidate_then_preserves_later_edits(creation_config, creation):
    container, service, store, path, calls = creation_config
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        await initialize_workspace_plugins(creation, "first-create", container)
    rejected = store.get_pending_config_candidate(config_domain="workspace", idempotency_key=creation.intent_id)
    assert rejected is not None and rejected.state == "rejected"
    original_text = path.read_text()
    first_revision = service.get_revision()
    result = await initialize_workspace_plugins(creation, "retry-create", container)
    assert result.data is True and result.request_id == "retry-create"
    active = store.get_pending_config_candidate(config_domain="workspace", idempotency_key=creation.intent_id)
    assert active is not None and active.state == "active"
    assert active.candidate_id == rejected.candidate_id and active.last_apply_id != rejected.last_apply_id
    assert path.read_text() == original_text and service.get_revision() != first_revision
    assert len(calls) == 3  # 首次验证失败、重试验证、成功提交通知。
    path.write_text('{"plugins":{"runtime":{"entries":[]}}}\n')
    assert (await initialize_workspace_plugins(creation, "already-created", container)).data is True
    assert path.read_text() == '{"plugins":{"runtime":{"entries":[]}}}\n'
    assert len(calls) == 3


async def test_rejected_initialization_does_not_overwrite_changed_source(creation_config, creation):
    container, service, store, path, calls = creation_config
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        await initialize_workspace_plugins(creation, "first-create", container)
    text = '{"plugins":{"runtime":{"entries":[]}}}\n'
    path.write_text(text)
    with pytest.raises(ConfigConflictError, match="基线已改变"):
        await initialize_workspace_plugins(creation, "retry-create", container)
    assert path.read_text() == text and len(calls) == 1
    pending = store.get_pending_config_candidate(config_domain="workspace", idempotency_key=creation.intent_id)
    assert pending is not None and pending.state == "rejected"
    assert service.get_snapshot().to_dict()["plugins"]["runtime"]["entries"] == []
