"""真实认证路由与文件 owner 配合 ASGI transport，验证发布通知及唯一快照消费链路。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import app.services.infrastructure.resource_platform.sources.gateway_skill_sources as gateway_sources
from app.core.trace_middleware import (
    TraceMiddleware,
    reset_current_request_id,
    set_current_request_id,
)
from app.gateway.auth import get_gateway_local_token
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes.skill_sources import router
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.skill_sources import GatewaySkillPublicationRequest
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
)

GATEWAY = "gateway_0123456789abcdef0123456789abcdef"
WORKSPACE = "11111111-1111-4111-8111-111111111111"
BASE = f"/api/gateway/skill-source-connections/{GATEWAY}/workspaces/{WORKSPACE}"


@dataclass
class PublicationCase:
    owner: GatewaySkillSourceOwner
    source: GatewaySkillSources
    skill: Path
    client: httpx.AsyncClient
    requests: list[httpx.Request] = field(default_factory=list)
    notification_failures: int = 0
    change_after_initial_fetch: bool = False
    corrupt_notification: str | None = None

    @property
    def snapshot_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "GET"]


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def publication_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "gateway-home"))
    skill = tmp_path / "gateway-home/skills/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("已发布的初版\n")
    owner = GatewaySkillSourceOwner(gateway_id=GATEWAY, skills_root=skill.parent.parent)
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    registry.upsert(WorkspaceTarget(
        workspace_id="local-workspace", name="test", root_path=str(tmp_path / "workspace"),
        backend_workspace_id=WORKSPACE, backend_url="http://unused.test", connection_kind="local", managed=False,
    ))
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router)
    app.state.skill_source_owner, app.state.registry = owner, registry
    await owner.start()
    token = get_gateway_local_token()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://gateway.test", headers={"X-Local-Token": token})
    source = GatewaySkillSources(
        workspace_id=WORKSPACE, connection=GatewayTransportConnection(gateway_url="http://gateway.test", timeout_seconds=1),
        local_token_supplier=lambda: token, observation_channel=ResourceObservationChannel(),
    )
    case = PublicationCase(owner, source, skill, client)

    async def send(request: httpx.Request) -> httpx.Response:
        case.requests.append(request)
        if request.method == "POST" and case.notification_failures:
            case.notification_failures -= 1
            raise httpx.ConnectError("测试通知断线", request=request)
        response = await transport.handle_async_request(request)
        await response.aread()
        if request.method == "POST" and case.corrupt_notification is not None:
            payload = response.json()
            headers = dict(response.headers)
            if case.corrupt_notification == "header":
                headers.pop("x-request-id")
            elif case.corrupt_notification == "envelope":
                payload["request_id"] = "wrong-response-identity"
            else:
                values = {"gateway_id": "another-owner", "workspace_id": "another-workspace", "cursor": "invalid",
                          "changed": False, "gap": True}
                payload["data"][case.corrupt_notification] = values[case.corrupt_notification]
                if case.corrupt_notification == "gap":
                    payload["data"]["changed"] = False
            # 只替换传输回执；真实 owner 发布和认证步骤已执行。
            return httpx.Response(200, json=payload, headers={key: value for key, value in headers.items() if key != "content-length"})
        if request.method == "GET" and case.change_after_initial_fetch:
            case.change_after_initial_fetch = False
            old_revision = owner.snapshot(workspace_id=WORKSPACE).revision
            skill.write_text("获取与订阅之间发布的新版\n")
            await _until(lambda: owner.snapshot(workspace_id=WORKSPACE).revision != old_revision)
        return response

    source._state(GATEWAY).client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    identity = set_current_request_id("publication-binding")
    try:
        yield case
    finally:
        await source.close()
        await client.aclose()
        await owner.close()
        registry.close()
        reset_current_request_id(identity)


async def test_notification_is_authenticated_lightweight_and_follows_publication(publication_case):
    case = publication_case
    for headers in ({}, {"X-Local-Token": "wrong"}):
        request = httpx.Request("POST", "http://gateway.test" + BASE + "/publications/wait", headers=headers, json={})
        assert (await case.client.send(request)).status_code == 401
    outside = BASE.replace(WORKSPACE, "outside-workspace")
    assert (await case.client.post(outside + "/publications/wait", json={})).status_code == 403
    first = await case.client.post(BASE + "/publications/wait", json={})
    assert first.status_code == 200
    assert first.headers["X-Request-ID"] == first.json()["request_id"]
    notice = first.json()["data"]
    assert set(notice) == {"gateway_id", "workspace_id", "cursor", "revision", "available", "changed", "gap"}
    assert notice["changed"] and notice["gap"] and notice["available"]
    assert notice["revision"] == (await case.client.get(BASE)).json()["data"]["revision"]
    case.owner._publish()  # 同一已接受版本重复发布不能制造新位置。
    heartbeat = await case.client.post(BASE + "/publications/wait", json={"after_cursor": notice["cursor"], "wait_ms": 1})
    assert heartbeat.json()["data"] == {**notice, "changed": False, "gap": False}
    case.skill.write_text("真实文件观察触发的新版\n")
    changed = await case.client.post(BASE + "/publications/wait", json={"after_cursor": notice["cursor"]})
    assert changed.status_code == 200
    latest = changed.json()["data"]
    assert latest["changed"] and not latest["gap"] and latest["revision"] != notice["revision"]
    snapshot = (await case.client.get(BASE)).json()["data"]
    assert snapshot["revision"] == latest["revision"]
    assert snapshot["entries"][0]["content"] == case.skill.read_text()
    assert str(case.skill) not in changed.text and "正文" not in changed.text


async def test_event_observation_closes_fetch_subscribe_window_without_periodic_snapshot(publication_case):
    case = publication_case
    case.change_after_initial_fetch = True
    await case.source.ensure_bound(GATEWAY)
    assert case.source.snapshot(gateway_id=GATEWAY).entries[0].content == "已发布的初版\n"
    await _until(lambda: case.source.snapshot(gateway_id=GATEWAY).entries[0].content == "获取与订阅之间发布的新版\n")
    assert len(case.snapshot_requests) == 2
    # 跨过多个真实通知保活，正文接口不应被每轮保活重新获取。
    await _until(lambda: len([r for r in case.requests if r.method == "POST"]) >= 4)
    assert len(case.snapshot_requests) == 2
    await case.source.close()
    assert case.owner._publications.subscriber_ids == ()


async def test_notification_disconnect_blocks_dispatch_and_reconciles_on_reconnect(publication_case):
    case = publication_case
    case.notification_failures = 1
    await case.source.ensure_bound(GATEWAY)
    retained = case.source.snapshot(gateway_id=GATEWAY)
    await _until(lambda: case.source._state(GATEWAY).error == "ConnectError")
    with pytest.raises(RuntimeError, match="ConnectError"):
        case.source.snapshot(gateway_id=GATEWAY)
    assert case.source._state(GATEWAY).snapshot is retained
    case.skill.write_text("断线期间的新正文\n")
    await _until(lambda: case.source._state(GATEWAY).error is None)
    assert case.source.snapshot(gateway_id=GATEWAY).entries[0].content == "断线期间的新正文\n"
    assert len(case.snapshot_requests) == 2


@pytest.mark.parametrize("failure", ["header", "envelope", "gateway_id", "workspace_id", "cursor", "changed", "gap"])
async def test_invalid_notification_never_accepts_another_owner_or_silently_switches_to_poll(publication_case, failure):
    case = publication_case
    case.corrupt_notification = failure
    await case.source.ensure_bound(GATEWAY)
    retained = case.source.snapshot(gateway_id=GATEWAY)
    await _until(lambda: case.source._state(GATEWAY).error is not None)
    assert case.source._state(GATEWAY).snapshot is retained
    assert len(case.snapshot_requests) == 1
    with pytest.raises(RuntimeError, match="ValueError|ValidationError"):
        case.source.snapshot(gateway_id=GATEWAY)


async def test_old_cursor_gap_and_restarted_owner_reconcile_current_publication(publication_case):
    case = publication_case
    first = await case.owner.wait_for_publication(GatewaySkillPublicationRequest(), workspace_id=WORKSPACE)
    for text in ("中间版\n", "最终版\n"):
        previous = case.owner.snapshot(workspace_id=WORKSPACE).revision
        case.skill.write_text(text)
        await _until(lambda previous=previous: case.owner.snapshot(workspace_id=WORKSPACE).revision != previous)
    latest = await case.owner.wait_for_publication(GatewaySkillPublicationRequest(after_cursor=first.cursor), workspace_id=WORKSPACE)
    assert latest.gap and latest.changed
    retained = await case.client.get(BASE)
    assert retained.json()["data"]["revision"] == latest.revision
    await case.owner.close()
    restarted = GatewaySkillSourceOwner(gateway_id=GATEWAY, skills_root=case.skill.parent.parent)
    try:
        await restarted.start()
        after_restart = await restarted.wait_for_publication(GatewaySkillPublicationRequest(after_cursor=latest.cursor), workspace_id=WORKSPACE)
        assert after_restart.changed and after_restart.gap
        assert after_restart.revision == latest.revision and after_restart.cursor != latest.cursor
    finally:
        await restarted.close()


async def test_manual_mode_has_one_initial_fetch_and_only_explicit_reconciliation(publication_case):
    case = publication_case
    case.source._update_policy = SourceUpdatePolicy(mode="manual")
    await case.source.ensure_bound(GATEWAY)
    await case.source.ensure_bound(GATEWAY)
    old = case.source.snapshot(gateway_id=GATEWAY)
    case.skill.write_text("只在显式对账后接受\n")
    await _until(lambda: case.owner.snapshot(workspace_id=WORKSPACE).revision != old.revision)
    assert case.source.snapshot(gateway_id=GATEWAY) is old
    assert len(case.requests) == 1 and case.source._state(GATEWAY).task is None
    await case.source.reconcile(gateway_id=GATEWAY)
    assert case.source.snapshot(gateway_id=GATEWAY).entries[0].content == case.skill.read_text()
    assert len(case.requests) == 2 and all(r.method == "GET" for r in case.requests)


async def test_poll_mode_uses_configured_interval_and_same_snapshot_endpoint(publication_case, monkeypatch):
    case = publication_case
    case.source._update_policy = SourceUpdatePolicy(mode="poll", poll_interval="1h")
    original_sleep = asyncio.sleep
    release = asyncio.Event()
    intervals: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        if asyncio.current_task() is case.source._state(GATEWAY).task:
            intervals.append(delay)
            if len(intervals) == 1:
                await release.wait()
            else:
                await asyncio.Event().wait()
        else:
            await original_sleep(delay)

    monkeypatch.setattr(gateway_sources.asyncio, "sleep", controlled_sleep)
    await case.source.ensure_bound(GATEWAY)
    old = case.source.snapshot(gateway_id=GATEWAY)
    await _until(lambda: bool(intervals))
    case.skill.write_text("轮询接受的新正文\n")
    await _until(lambda: case.owner.snapshot(workspace_id=WORKSPACE).revision != old.revision)
    assert case.source.snapshot(gateway_id=GATEWAY) is old and len(case.requests) == 1
    release.set()
    await _until(lambda: len(intervals) == 2)
    assert intervals == [3600, 3600]
    assert case.source.snapshot(gateway_id=GATEWAY).entries[0].content == case.skill.read_text()
    assert len(case.requests) == 2 and all(r.url.path == BASE for r in case.requests)


async def test_explicit_event_poll_reconciles_on_deadline_without_source_event(publication_case, monkeypatch):
    case = publication_case
    case.source._update_policy = SourceUpdatePolicy(mode="event_poll", poll_interval="5m")
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(gateway_sources, "time", SimpleNamespace(monotonic=lambda: clock.value))
    await case.source.ensure_bound(GATEWAY)
    await _until(lambda: len(case.snapshot_requests) == 2)
    await _until(lambda: len([r for r in case.requests if r.method == "POST"]) >= 3)
    assert len(case.snapshot_requests) == 2
    clock.value += 301
    await _until(lambda: len(case.snapshot_requests) == 3)
    assert case.snapshot_requests[-1].headers["If-None-Match"] == '"' + case.source.snapshot(gateway_id=GATEWAY).revision + '"'
