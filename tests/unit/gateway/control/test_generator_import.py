"""只验证显式退休入口；生成任务的运行由工作区产品测试验证。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from app.gateway.control.generator_import import import_session_generators
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


@pytest.fixture
def migration_case(tmp_path, monkeypatch):
    target = WorkspaceTarget(workspace_id="route", name="目标", root_path="/not-used", backend_url="http://workspace.test",
                             connection_kind="local", backend_workspace_id="00000000-0000-4000-8000-000000000001")
    registry = Mock(spec=GatewayWorkspaceRegistry)
    registry.resolve.side_effect = lambda value: target if value == "route" else (_ for _ in ()).throw(KeyError(value))
    monkeypatch.setattr("app.gateway.control.resource_catalog.load_proxy_gateway_id", lambda: "gateway_local")
    created = datetime(2026, 10, 27, tzinfo=UTC).isoformat()
    definition = {"generator_id": "gen_old", "name": "原生成器", "execution_workspace_id": "route",
                  "placement": {"kind": "workspace", "workspace_id": "route"}, "created_at": created, "updated_at": created,
                  "config": {"prompt": "原提示"}, "trigger": {"type": "interval", "interval_seconds": 60}, "revision": 7,
                  "created_from": {"workspace_id": "route", "session_id": "ses_origin"}}
    path = tmp_path / "generators/gen_old.json"
    path.parent.mkdir()
    path.write_text(json.dumps(definition))
    schedule = tmp_path / "generator-schedules/gen_old.json"
    schedule.parent.mkdir()
    schedule.write_text(json.dumps({"definition_revision": 7, "next_run_at": "2026-10-27T00:01:00Z"}))
    sent = []
    def receive(request):
        sent.append(request)
        payload = json.loads(request.content)
        return httpx.Response(200, json={"data": {"task_id": payload["task_id"]}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    api = GatewayResourceCatalogService(registry=registry, http_client=client)
    return tmp_path, registry, client, api, sent


async def migrate(case, *, preview=False):
    root, registry, client, api, _sent = case
    return await import_session_generators(root=root, registry=registry, http_client=client, api=api,
                                           gateway_id="gateway_local", request_id="req_import", preview=preview)


@pytest.mark.asyncio
async def test_preview_then_install_and_repeat_retains_original_snapshot(migration_case):
    root, _registry, client, _api, sent = migration_case
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    try:
        planned, = await migrate(migration_case, preview=True)
        assert planned["status"] == "planned" and sent == []
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        installed, = await migrate(migration_case)
        assert installed == {**planned, "status": "imported"}
        request, = sent
        assert request.url.path == "/api/v1/persistent-tasks/imports/session-generators"
        assert request.headers["X-Request-ID"] == "req_import"
        assert request.headers["X-BoxTeam-Workspace-Id"] == "00000000-0000-4000-8000-000000000001"
        payload = json.loads(request.content)
        assert payload["task_id"] == "gen_old" and payload["revision"] == 7
        assert payload["configuration"]["action"]["generation"]["created_from_uri"].startswith("boxteam://gateway_local/")
        assert payload["next_run_at"] == "2026-10-27T00:01:00Z"
        assert not (root / "generators/gen_old.json").exists()
        assert (await migrate(migration_case)) == [installed]
        assert len(sent) == 1
        backup = root / "migration-backups/session-generators" / planned["source_hash"]
        for relative, raw in before.items():
            assert (backup / relative).read_bytes() == raw
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retirement_crash_retries_deleted_prefix_without_second_publish(migration_case, monkeypatch):
    root, _registry, client, _api, sent = migration_case
    original = Path.unlink
    def interrupt(path, *args, **kwargs):
        if path == root / "generator-schedules/gen_old.json":
            raise OSError("simulated retirement crash /private/native/path")
        return original(path, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", interrupt)
            failed, = await migrate(migration_case)
        assert failed["status"] == "failed" and "private/native/path" not in failed["error"]
        assert not (root / "generators/gen_old.json").exists()
        assert len(sent) == 1
        recovered, = await migrate(migration_case)
        assert recovered["status"] == "imported" and len(sent) == 1
        assert not (root / "generator-schedules/gen_old.json").exists()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_offline_or_changed_owner_never_falls_back(migration_case):
    root, registry, client, _api, sent = migration_case
    try:
        registry.resolve.side_effect = KeyError("offline")
        failed, = await migrate(migration_case)
        assert failed["status"] == "failed" and sent == []
        assert (root / "generators/gen_old.json").exists()
    finally:
        await client.aclose()
