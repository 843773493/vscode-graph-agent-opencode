"""具名命令复用原 Gateway 路由和流生命周期，只调用已就绪的精确实例。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from app.core.async_channel import run_channel
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.federation.policy import Capability, require_federation_capability
from app.gateway.proxy_upstream import UpstreamResponseBody, send_upstream_request
from app.gateway.runtime.service_plugins.runtime import PluginSlot
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.gateway.server.service_plugin_proxy import (
    _ServiceResponse,
    _ServiceRoute,
    _watch_access,
)
from app.schemas.gateway.plugin_commands import (
    PluginCommandCallRequest,
    PluginCommandCancelDTO,
    PluginCommandCancelRequest,
    PluginCommandEventDTO,
    PluginCommandInstanceRequest,
    PluginCommandRegistrationsDTO,
)
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()
Auth = Annotated[GatewayAuthContext, Depends(verify_gateway_access)]


@contextmanager
def command_route(request: Request, payload: PluginCommandInstanceRequest, auth: GatewayAuthContext,
                  operation: str) -> Iterator[tuple[_ServiceRoute, PluginSlot | None]]:
    with management_gateway_route(request, gateway_id=payload.gateway_id,
        gateway_connection_id=payload.gateway_connection_id, auth=auth) as remote:
        if remote is not None:
            stopped = asyncio.Event()

            def check() -> None:
                try:
                    check_management_route(request, remote)
                except LookupError:
                    stopped.set()

            registry = request.app.state.registry
            registry.add_commit_observer(check)
            try:
                check()
                yield _ServiceRoute(httpx.URL(remote.base_url + request.url.path), remote.headers,
                    stopped, payload.instance_id, payload.gateway_id, True), None
            finally:
                registry.remove_commit_observer(check)
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


def capabilities(request: Request, auth: GatewayAuthContext, slot: PluginSlot | None,
                 command_id: str | None = None) -> tuple[Capability, ...]:
    required: tuple[Capability, ...] = ("send", "read")
    if slot is not None and command_id is not None:
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


@router.post("/api/gateway/service-plugins/commands/call", response_model=None)
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
