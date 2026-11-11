"""具名命令复用原 Gateway 路由和流生命周期，只调用已就绪的精确实例。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    stream_sse_with_heartbeat,
)
from app.core.async_channel import run_channel
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import (
    Capability,
    federation_capability_allowed,
    require_federation_capability,
)
from app.gateway.proxy_upstream import (
    UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS,
    UpstreamResponseBody,
    send_upstream_request,
)
from app.gateway.runtime.service_plugins.runtime import PluginSlot
from app.gateway.server.service_plugin_proxy import (
    _PluginSnapshotResponse,
    _ServiceResponse,
    _ServiceRoute,
    _watch_access,
    plugin_gateway_route,
)
from app.schemas.gateway.plugin_commands import (
    PluginCommandCallRequest,
    PluginCommandCancelDTO,
    PluginCommandCancelRequest,
    PluginCommandEventDTO,
    PluginCommandInstanceRequest,
    PluginCommandRegistrationsDTO,
    PluginCommandSnapshotDTO,
    PluginContributionTarget,
)
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import sse_responses

router = APIRouter()
Auth = Annotated[GatewayAuthContext, Depends(verify_gateway_access)]


@contextmanager
def command_route(request: Request, payload: PluginCommandInstanceRequest, auth: GatewayAuthContext,
                  operation: str) -> Iterator[tuple[_ServiceRoute, PluginSlot | None]]:
    with plugin_gateway_route(request, payload, auth) as remote:
        if remote is not None:
            yield replace(remote, instance_id=payload.instance_id), None
            return
        if payload.workspace_id is not None:
            target = request.app.state.registry.resolve((payload.gateway_id, payload.workspace_id))
            if target.connection_kind != "local":
                raise LookupError("插件命令不属于原生工作区")
        owner = request.app.state.service_plugins
        binding = PluginBinding(package_id=payload.package_id, package_version=payload.package_version,
            plugin_id=payload.plugin_id, workspace_id=payload.workspace_id, enabled=True)
        slot = owner.slots.get(owner.key(binding))
        if slot is None or slot.binding != binding:
            raise LookupError("插件绑定不可用或版本已切换")
        if (slot.describe()["state"] != "ready" or slot.receipt is None or slot.stopped.is_set()
            or slot.receipt.instance_id != payload.instance_id):
            raise LookupError("原插件实例尚未就绪或已失效；命令不会启动或改投实例")
        if isinstance(payload, PluginCommandCancelRequest):
            registered = slot.commands.get(payload.command_id)
            if registered is None or registered.registration_id != payload.registration_id:
                raise LookupError("原具名命令注册已失效")
        assert slot.base_url is not None and slot.token is not None
        yield _ServiceRoute(httpx.URL(slot.base_url + "/internal/plugin/commands" + operation),
            {"X-Local-Token": slot.token, "X-Request-ID": get_request_id(request)},
            slot.stopped, payload.instance_id, payload.gateway_id), slot


def _contribution_access(request: Request, payload: PluginContributionTarget, auth: GatewayAuthContext) -> None:
    require_federation_capability(request, auth, "read")
    if payload.workspace_id is not None:
        target = request.app.state.registry.resolve((payload.gateway_id, payload.workspace_id))
        if target.connection_kind != "local":
            raise LookupError("贡献快照必须由目标 Gateway 的原生工作区解释")


def _authorized_snapshot(request: Request, payload: PluginContributionTarget,
                         auth: GatewayAuthContext) -> PluginCommandSnapshotDTO:
    snapshot = request.app.state.service_plugins.command_snapshot(payload.workspace_id)
    publications = []
    for publication in snapshot.publications:
        commands = [command for command in publication.commands if all(
            federation_capability_allowed(request, auth, capability)
            for capability in cast(list[Capability], ["send", *command.required_capabilities]))]
        ids = {command.id for command in commands}
        publications.append(publication.model_copy(update={"commands": commands,
            "registrations": [entry for entry in publication.registrations if entry.command_id in ids],
            "menus": [entry for entry in publication.menus if entry.command_id in ids],
            "keybindings": [entry for entry in publication.keybindings if entry.command_id in ids]}))
    return snapshot.model_copy(update={"publications": publications})


@router.post("/api/gateway/service-plugins/commands/publications")
async def publications(payload: PluginContributionTarget, request: Request,
                       auth: Auth) -> APIResponse[PluginCommandSnapshotDTO]:
    try:
        with plugin_gateway_route(request, payload, auth) as remote:
            require_federation_capability(request, auth, "read")
            if remote is None:
                _contribution_access(request, payload, auth)
                snapshot = _authorized_snapshot(request, payload, auth)
            else:
                response = await request.app.state.service_plugins.client.post(remote.url, headers=remote.headers,
                    json=payload.model_dump(mode="json", exclude={"gateway_connection_id"}))
                response.raise_for_status()
                envelope = APIResponse[PluginCommandSnapshotDTO].model_validate(response.json())
                snapshot = envelope.data
                if envelope.request_id != get_request_id(request) or remote.stopped.is_set():
                    raise LookupError("原贡献快照路由已失效或回执不属于原请求")
            if snapshot is None or (snapshot.gateway_id, snapshot.workspace_id) != (payload.gateway_id, payload.workspace_id):
                raise ValueError("贡献快照不属于原 Gateway／工作区")
            require_federation_capability(request, auth, "read")
            return APIResponse(data=snapshot, request_id=get_request_id(request))
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, error.response.json().get("detail")) from error


@router.post("/api/gateway/service-plugins/commands/events", response_class=StreamingResponse,
    responses=sse_responses("原绑定的完整命令贡献快照", {"snapshot": PluginCommandSnapshotDTO}))
async def publication_events(payload: PluginContributionTarget, request: Request, auth: Auth) -> StreamingResponse:
    references = ExitStack()
    body: UpstreamResponseBody | None = None
    handed_off = False
    try:
        remote = references.enter_context(plugin_gateway_route(request, payload, auth))
        require_federation_capability(request, auth, "read")
        if remote is None:
            _contribution_access(request, payload, auth)
            owner = request.app.state.service_plugins

            async def frames() -> AsyncIterator[str]:
                while True:
                    # 先捕获唤醒对象再读取快照，不在 yield 之间丢掉配置提交。
                    changed = owner.changed
                    policy_changed = request.app.state.federation_policy_store.changed
                    if stopped.is_set() or not federation_capability_allowed(request, auth, "read"):
                        return
                    snapshot = _authorized_snapshot(request, payload, auth)
                    yield f"event: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
                    await run_channel(changed.wait(), policy_changed.wait())

            source = stream_sse_with_heartbeat(frames(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
                                               close_source_on_exit=True)
            stopped = asyncio.Event()
            registry = request.app.state.registry

            def check() -> None:
                try:
                    _contribution_access(request, payload, auth)
                except (HTTPException, LookupError):
                    stopped.set()

            registry.add_commit_observer(check)
            references.callback(registry.remove_commit_observer, check)
        else:
            client = request.app.state.service_plugins.client
            upstream = client.build_request("POST", remote.url, headers=remote.headers,
                json=payload.model_dump(mode="json", exclude={"gateway_connection_id"}), timeout=None)
            response = await send_upstream_request(client, upstream,
                response_headers_timeout_seconds=UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS)
            body = UpstreamResponseBody(response)
            if response.is_error:
                await response.aread()
                raise HTTPException(response.status_code, response.json().get("detail"))
            if (response.headers.get("X-Request-ID") != get_request_id(request)
                or response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream"):
                raise ValueError("贡献事件流回执的请求身份或协议不符")
            source, stopped = body, remote.stopped
        result = _PluginSnapshotResponse(source, request=request, auth=auth, stopped=stopped,
                                            close=references.close, body=body)
        handed_off = True
        return result
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    finally:
        if not handed_off:
            try:
                if body is not None:
                    await body.aclose()
            finally:
                references.close()


def capabilities(request: Request, auth: GatewayAuthContext, slot: PluginSlot | None,
                 command_id: str | None = None) -> tuple[Capability, ...]:
    required: tuple[Capability, ...] = ("send", "read")
    if slot is not None and command_id is not None:
        assert slot.declaration is not None
        command = next(item for item in slot.declaration.contributes.commands if item.id == command_id)
        # 包发布已拒绝未知权限；每次执行及策略变更只核原业务能力。
        required = tuple(dict.fromkeys((*required, *cast(list[Capability], command.required_capabilities))))
    for capability in required:
        require_federation_capability(request, auth, capability)
    return required


class _CommandBody(UpstreamResponseBody):
    async def events(self, payload: PluginCommandCallRequest, request_id: str, deadline: float,
                     validator: Draft202012Validator | None) -> AsyncIterator[bytes]:
        sequence, terminal = 0, False
        try:
            async with asyncio.timeout_at(deadline):
                async for line in self._response.aiter_lines():
                    event = PluginCommandEventDTO.model_validate_json(line)
                    if (terminal or (event.instance_id, event.registration_id, event.command_id,
                        event.call_id, event.request_id, event.sequence) != (
                        payload.instance_id, payload.registration_id, payload.command_id,
                        payload.call_id, request_id, sequence + 1)):
                        raise ValueError("命令事件不属于原调用或顺序不符；结果不得重放")
                    if (sequence == 0) != (event.kind == "started"):
                        raise ValueError("命令必须以唯一 started 事件开始")
                    sequence = event.sequence
                    if event.kind == "result" and validator is not None:
                        validator.validate(event.result)
                    terminal = event.kind in {"result", "error", "cancelled"}
                    yield (event.model_dump_json() + "\n").encode()
                if not terminal:
                    raise ValueError("命令通道中断，结果未知；不得自动重放")
        finally:
            await self.aclose()


@router.post("/api/gateway/service-plugins/commands/registrations")
async def registrations(payload: PluginCommandInstanceRequest, request: Request,
                        auth: Auth) -> APIResponse[PluginCommandRegistrationsDTO]:
    try:
        with command_route(request, payload, auth, "") as (route, slot):
            capabilities(request, auth, slot)
            if slot is not None:
                data = PluginCommandRegistrationsDTO(instance_id=payload.instance_id, commands=list(slot.commands.values()))
            else:
                body = payload.model_dump(mode="json", exclude={"gateway_connection_id"})
                response = await request.app.state.service_plugins.client.post(route.url, json=body, headers=route.headers)
                response.raise_for_status()
                if route.stopped.is_set():
                    raise LookupError("原命令路由已退役")
                envelope = APIResponse[PluginCommandRegistrationsDTO].model_validate(response.json())
                if envelope.request_id != get_request_id(request):
                    raise ValueError("远端命令注册回执不属于原请求")
                data = envelope.data
                if data is None:
                    raise ValueError("远端没有返回命令注册")
                if data.instance_id != payload.instance_id:
                    raise ValueError("远端命令注册不属于原实例")
            capabilities(request, auth, slot)
            return APIResponse(data=data, request_id=get_request_id(request))
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, error.response.json().get("detail")) from error


@router.post("/api/gateway/service-plugins/commands/cancel")
async def cancel(payload: PluginCommandCancelRequest, request: Request,
                 auth: Auth) -> APIResponse[PluginCommandCancelDTO]:
    try:
        with command_route(request, payload, auth, "/cancel") as (route, slot):
            capabilities(request, auth, slot, payload.command_id)
            body = payload.model_dump(mode="json", exclude={"gateway_connection_id"})
            response = await request.app.state.service_plugins.client.post(route.url, json=body, headers=route.headers)
            response.raise_for_status()
            if route.stopped.is_set():
                raise LookupError("原命令路由已退役")
            if route.remote:
                envelope = APIResponse[PluginCommandCancelDTO].model_validate(response.json())
                if envelope.request_id != get_request_id(request):
                    raise ValueError("远端命令取消回执不属于原请求")
                data = envelope.data
            else:
                data = PluginCommandCancelDTO.model_validate(response.json())
            if data is None:
                raise ValueError("远端没有返回命令取消回执")
            if (data.instance_id, data.call_id) != (payload.instance_id, payload.call_id):
                raise ValueError("取消回执不属于原调用")
            capabilities(request, auth, slot, payload.command_id)
            return APIResponse(data=data, request_id=get_request_id(request))
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, error.response.json().get("detail")) from error


@router.post("/api/gateway/service-plugins/commands/call", response_model=None,
    response_class=StreamingResponse,
    responses={200: {"description": "具名插件命令的有序 JSON Lines 执行事件",
        "content": {"application/x-ndjson": {"schema": {"type": "string"},
            "x-ndjson-item": {"$ref": "#/components/schemas/PluginCommandEventDTO"}}}}})
async def call(payload: PluginCommandCallRequest, request: Request, auth: Auth) -> _ServiceResponse:
    deadline = asyncio.get_running_loop().time() + payload.remaining_ms / 1000
    routes = command_route(request, payload, auth, "/call")
    body: _CommandBody | None = None
    handed_off = False
    try:
        route, slot = routes.__enter__()
        required = capabilities(request, auth, slot, payload.command_id)
        validator = None
        if slot is not None:
            arguments_validator, validator = slot.command_validators[payload.command_id]
            arguments_validator.validate(payload.arguments)
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            raise TimeoutError("命令在准入期间已到期限")
        forwarded = payload.model_copy(update={"remaining_ms": remaining_ms})
        client = request.app.state.service_plugins.client
        upstream = client.build_request("POST", route.url,
            json=forwarded.model_dump(mode="json", exclude={"gateway_connection_id"}), headers=route.headers, timeout=None)
        response: httpx.Response | None = None

        def release_route() -> None:
            routes.__exit__(None, None, None)

        async def open_response() -> None:
            nonlocal response, body
            response = await send_upstream_request(client, upstream, response_headers_timeout_seconds=remaining_ms / 1000)
            body = _CommandBody(response, release_route)

        await run_channel(open_response(), _watch_access(request, auth, route.stopped, required))
        assert response is not None and body is not None
        if response.is_error:
            await response.aread()
            raise HTTPException(response.status_code, response.json().get("detail"))
        capabilities(request, auth, slot, payload.command_id)
        result = _ServiceResponse(body, request=request, auth=auth, route=route, response=response, capabilities=required)
        result.body_iterator = body.events(payload, get_request_id(request), deadline, validator)
        handed_off = True
        return result
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, ValidationError) as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except TimeoutError as error:
        raise HTTPException(504, str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(502, f"插件命令通道失败，结果未知，不得重放: {type(error).__name__}") from error
    finally:
        if not handed_off:
            if body is not None:
                await body.aclose()
            else:
                routes.__exit__(None, None, None)
