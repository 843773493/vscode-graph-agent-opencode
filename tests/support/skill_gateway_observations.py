"""只记录真实 Skill HTTP 与 owner 回执；不替换生产响应或持久化事实。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from app.core.trace_middleware import get_current_request_id
from app.gateway.federation.skill_sources import GatewaySkillSourcePort
from app.services.infrastructure.resource_platform.sources.gateway_skill_sources import (
    GatewaySkillSources,
    _GatewaySkillState,
)


def _record(event: str, values: dict[str, object]) -> None:
    directory = Path(os.environ["BOXTEAM_TEST_SKILL_OBSERVATION_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    role = os.environ["BOXTEAM_TEST_SKILL_OBSERVATION_ROLE"]
    with (directory / f"skill-observations-{role}-{os.getpid()}.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, "pid": os.getpid(), **values}, ensure_ascii=False) + "\n")


def _request_values(request: httpx.Request) -> dict[str, object]:
    task = asyncio.current_task()
    return {
        "method": request.method, "path": request.url.path, "url": str(request.url),
        "sent_request_id": request.headers.get("X-Request-ID"),
        "if_none_match": request.headers.get("If-None-Match"),
        "context_request_id": get_current_request_id(),
        "task": task.get_name() if task is not None else None,
    }


async def _request(request: httpx.Request) -> None:
    _record("workspace_request", _request_values(request))


async def _response(response: httpx.Response) -> None:
    await response.aread()
    envelope_id = None
    if response.content and "application/json" in response.headers.get("Content-Type", ""):
        envelope_id = response.json().get("request_id")
    _record("workspace_response", {
        **_request_values(response.request), "status": response.status_code,
        "response_request_id": response.headers.get("X-Request-ID"),
        "envelope_request_id": envelope_id, "has_body": bool(response.content),
        "etag": response.headers.get("ETag"),
    })


class ObservedGatewaySkillSources(GatewaySkillSources):
    """在生产 client 上安装只读 HTTPX 事件观察，不改变请求与响应。"""

    def _client(self, state: _GatewaySkillState) -> httpx.AsyncClient:
        client = super()._client(state)
        if _request not in client.event_hooks["request"]:
            client.event_hooks["request"].append(_request)
            client.event_hooks["response"].append(_response)
        return client


class ObservedGatewaySkillSourcePort(GatewaySkillSourcePort):
    """先执行完整生产 owner，再记录返回给原调用方的身份与状态。"""

    async def execute(self, request: dict[str, object]) -> dict[str, object]:
        result = await super().execute(request)
        _record("owner_receipt", {
            "owner_gateway_id": self.owner.gateway_id,
            "request_gateway_id": request.get("gateway_id"),
            "request_workspace_id": request.get("workspace_id"),
            "request_id": request.get("request_id"), "kind": request.get("kind"),
            "if_none_match": request.get("if_none_match"),
            "result_gateway_id": result.get("gateway_id"),
            "result_workspace_id": result.get("workspace_id"),
            "result_request_id": result.get("request_id"), "status": result.get("status"),
            "etag": result.get("etag"),
        })
        return result


role = os.environ["BOXTEAM_TEST_SKILL_OBSERVATION_ROLE"]
if role == "gateway":
    from app.gateway import lifespan
    from app.gateway.main import app
    from app.gateway.routes import skill_sources

    lifespan.GatewaySkillSourcePort = ObservedGatewaySkillSourcePort
    skill_sources.GatewaySkillSourcePort = ObservedGatewaySkillSourcePort
elif role == "backend":
    from app.services.infrastructure.resource_platform import bootstrap

    bootstrap.GatewaySkillSources = ObservedGatewaySkillSources
    from tests.support.communication_fault_backend import app
else:
    raise ValueError(f"未知 Skill 观测进程角色: {role}")

__all__ = ["app"]
