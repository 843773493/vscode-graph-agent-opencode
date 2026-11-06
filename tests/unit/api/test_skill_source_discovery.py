"""Skill 发现只投影原 owner 的 accepted metadata，认证和诊断沿原 HTTP 边界。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import resources
from app.api.deps import get_configured_skill_sources
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.resource_platform.observation.resource_observation_channel import (
    ResourceObservationChannel,
)
from app.services.infrastructure.resource_platform.sources import skill_source_config
from app.services.infrastructure.resource_platform.sources.configured_skill_sources import (
    ConfiguredSkillSources,
)
from app.services.infrastructure.resource_platform.sources.http_skill_fetch import (
    HttpSkillFetch,
)
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillFetchCapability,
)
from app.services.infrastructure.resource_platform.sources.skill_source_config import (
    SkillSourceConfig,
)


@dataclass
class DiscoveryCase:
    owner: ConfiguredSkillSources
    client: httpx.AsyncClient
    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 200


@pytest.fixture
async def discovery_case(
    empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[DiscoveryCase]:
    from app.main import app as workspace_app

    workspace_id, _ = empty_workspace_scope
    owner = ConfiguredSkillSources(scope="workspace", scope_id=workspace_id, channel=ResourceObservationChannel())
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(resources.router, prefix="/api/v1")
    application.add_exception_handler(HTTPException, workspace_app.exception_handlers[HTTPException])
    application.dependency_overrides[get_configured_skill_sources] = lambda: owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://test", headers={
        "X-Local-Token": "local-dev-token", "X-BoxTeam-Gateway-Id": "gateway_" + "a" * 32,
        "X-Request-ID": "skill-source-discovery",
    }) as client:
        case = DiscoveryCase(owner, client)

        async def respond(request: httpx.Request) -> httpx.Response:
            case.requests.append(request)
            return httpx.Response(case.status, content="# 私有 accepted 正文 Ω\n".encode(), headers={"ETag": '"private-validator"'})

        capability = skill_source_config.skill_fetch_capability("http")
        monkeypatch.setattr(skill_source_config, "SKILL_FETCH_CAPABILITIES", {
            **skill_source_config.SKILL_FETCH_CAPABILITIES,
            "http": SkillFetchCapability(config_type=capability.config_type,
                create=lambda: HttpSkillFetch(httpx.AsyncClient(transport=httpx.MockTransport(respond)))),
        })
        configs = tuple(SkillSourceConfig.model_validate({
            "id": source_id, "name": "same-skill", "priority": priority,
            "fetch": {"protocol": "http", "url": f"https://private-user:private-secret@skills.example.test/{source_id}?key=private-key"},
            "updates": {"mode": "manual"},
        }) for source_id, priority in (("first", 0), ("higher", 2**40)))
        owner.activate(await owner.prepare(configs))
        try:
            yield case
        finally:
            await owner.close()


async def test_discovery_preserves_each_accepted_source_without_fetch_or_private_payload(discovery_case: DiscoveryCase) -> None:
    case = discovery_case
    accepted, count = case.owner.entries(), len(case.requests)
    response = await case.client.get("/api/v1/resources/skill-sources")
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "skill-source-discovery"
    assert response.json()["data"] == {"workspace_id": case.owner.owner_id, "entries": [
        {"name": entry.name, "display_uri": entry.display_uri, "available": True, "error_code": None, "priority": entry.priority}
        for entry in accepted
    ]}
    assert accepted[0].display_uri != accepted[1].display_uri
    assert accepted[1].priority == 2**40
    assert case.owner.entries() == accepted and len(case.requests) == count == 2
    for private in ("content", "fetch", "url", "private-user", "private-secret", "private-key", "private-validator", "私有 accepted 正文"):
        assert private not in response.text


async def test_unavailable_entry_keeps_original_reason_without_old_body_or_retry(discovery_case: DiscoveryCase) -> None:
    case = discovery_case
    case.status = 503
    await case.owner.refresh()
    failed, count = case.owner.entries(), len(case.requests)
    assert all(entry.content and not entry.available and entry.error_code == "source-unavailable" for entry in failed)
    response = await case.client.get("/api/v1/resources/skill-sources")
    assert response.status_code == 200, response.text
    assert [(entry["display_uri"], entry["available"], entry["error_code"]) for entry in response.json()["data"]["entries"]] == [
        (entry.display_uri, False, "source-unavailable") for entry in failed
    ]
    assert "私有 accepted 正文" not in response.text and "content" not in response.text
    assert case.owner.entries() == failed and len(case.requests) == count == 4


@pytest.mark.parametrize("token", ["", "wrong-token"])
async def test_discovery_requires_original_local_authentication(discovery_case: DiscoveryCase, token: str) -> None:
    response = await discovery_case.client.get("/api/v1/resources/skill-sources", headers={"X-Local-Token": token})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid local token"
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "skill-source-discovery"
    assert len(discovery_case.requests) == 2


async def test_empty_accepted_configuration_is_explicit(discovery_case: DiscoveryCase) -> None:
    owner = discovery_case.owner
    owner.activate(await owner.prepare(()))
    response = await discovery_case.client.get("/api/v1/resources/skill-sources")
    assert response.json()["data"] == {"workspace_id": owner.owner_id, "entries": []}
    assert len(discovery_case.requests) == 2


async def test_unready_owner_is_not_reported_as_empty_configuration(discovery_case: DiscoveryCase, caplog: pytest.LogCaptureFixture) -> None:
    await discovery_case.owner.close()
    response = await discovery_case.client.get("/api/v1/resources/skill-sources")
    assert response.status_code == 500 and response.json()["data"] is None
    assert response.json()["message"] == "请求处理失败: stage=http_request, error=RuntimeError"
    assert "配置 Skill source owner 尚未就绪" in caplog.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "skill-source-discovery"
