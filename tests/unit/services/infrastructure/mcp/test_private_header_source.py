"""引用经过真实来源预检、CAS 保存、权威快照与冷恢复；不建立 MCP 连接。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import SecretStr

from app.schemas.internal_v2.config import ConfigSourceEditRequest
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp.private_inputs import McpPrivateInputStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

GATEWAY = "gateway_" + "a" * 32
SOURCE = "workspace_root_mutable_override"
SECRET = "Bearer source-unit-only"


class SourceConfigPaths(TypedDict):
    config_dir: Path
    inline_config_path: Path
    config_path: Path
    workspace_root: Path


@pytest.fixture
def private_source(empty_workspace_scope: tuple[str, Path], tmp_path: Path, monkeypatch):
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
    kwargs = SourceConfigPaths(config_dir=directory, inline_config_path=inline, config_path=directory / "workspace.jsonc", workspace_root=root)
    with WorkspaceStateStore(workspace_root=root) as store:
        service = ConfigService(**kwargs, workspace_state_store=store)
        service.get_snapshot()
        service.validate_workspace_config()
        reference = McpPrivateInputStore(workspace_root=root).write(
            server_id="secure", header_name="Authorization", endpoint="https://service.test/mcp", value=SecretStr(SECRET),
        )
        yield workspace_id, service, store, reference, path, kwargs


async def test_reference_survives_source_candidate_cas_save_and_cold_snapshot(private_source):
    workspace_id, service, store, reference, path, kwargs = private_source
    assert (reference.workspace_id, reference.server_id, reference.header_name, reference.endpoint) == (
        workspace_id, "secure", "Authorization", "https://service.test/mcp",
    )
    original = service.get_source_document(source_key=SOURCE, gateway_id=GATEWAY)
    payload = {"mcp": {"servers": {"secure": {"transport": "streamable_http", "url": reference.endpoint,
               "headers": {"Authorization": reference.model_dump(mode="json")}}}}}
    text = "// 用户保留的格式\n" + json.dumps(payload, indent=2) + "\n"
    request = ConfigSourceEditRequest(
        gateway_id=GATEWAY, workspace_id=workspace_id, source_key=SOURCE, jsonc=text,
        base_file_digest=original.file_digest, base_layer_revision=original.layer_revision,
        base_layer_digest=original.layer_digest, expected_active_revision=original.active_revision,
        expected_active_digest=original.active_digest, idempotency_key="private-header-source",
    )
    preview = await service.preflight_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=request)
    assert preview.candidate["mcp"]["servers"]["secure"]["headers"]["Authorization"] == reference.model_dump(mode="json")
    assert path.read_text() == "{}\n" and SECRET not in preview.model_dump_json()
    stale = request.model_copy(update={"expected_active_digest": "stale"})
    with pytest.raises(ConfigConflictError):
        await service.save_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=stale)
    assert path.read_text() == "{}\n"
    saved = await service.save_source_edit(source_key=SOURCE, gateway_id=GATEWAY, request=request)
    assert saved.jsonc == text and saved.edit is not None and saved.edit.source_saved and saved.edit.layer_imported
    assert saved.edit.state == "active" and path.read_text() == text
    assert SECRET not in saved.model_dump_json() and "boxteam-redacted:" not in saved.jsonc
    restarted = ConfigService(**kwargs, workspace_state_store=store)
    assert restarted.get_mcp_config()["servers"]["secure"]["headers"]["Authorization"] == reference.model_dump(mode="json")
    cold_private_store = McpPrivateInputStore(workspace_root=kwargs["workspace_root"])
    assert cold_private_store.resolve(
        reference, server_id="secure", header_name="Authorization", endpoint="https://service.test/mcp",
    ) == SECRET
    assert SECRET not in path.read_text(encoding="utf-8")
    with sqlite3.connect(store.path) as connection:
        assert SECRET not in "\n".join(connection.iterdump())
