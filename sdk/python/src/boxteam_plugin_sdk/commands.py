"""插件运行实例使用的具名命令 HTTP 合同与执行入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from boxteam_plugin_sdk.failures import (
    DIAGNOSTIC_BYTES,
    bounded_failure_text,
    failure_message,
    format_failure_diagnostic,
)
from boxteam_plugin_sdk.http import (
    get_request_id,
    verify_local_control_request,
    verify_runtime_control_token,
)
from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from boxteam_plugin_sdk.logging import runtime_logging_router, runtime_logging_state
from boxteam_plugin_sdk.observation import plugin_observation_router


class PluginCommandContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=1)
    category: str | None = None
    handler: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    arguments_schema: dict[str, JsonValue]
    result_schema: dict[str, JsonValue]
    required_capabilities: list[str] = Field(default_factory=list)
    enablement: str | None = None


class PluginServiceContribution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    service_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    service_type: Literal["mcp", "terminal", "browser", "debug"]
    protocol: Literal["streamable-http", "http", "websocket", "dap"]

    @model_validator(mode="after")
    def protocol_matches_service(self) -> PluginServiceContribution:
        allowed = {
            "mcp": {"streamable-http"},
            "terminal": {"http", "websocket"},
            "browser": {"http", "websocket"},
            "debug": {"dap"},
        }
        if self.protocol not in allowed[self.service_type]:
            raise ValueError("服务协议与 service_type 不匹配")
        return self


class PluginCommandRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)


class PluginCommandRegistrationsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    commands: list[PluginCommandRegistration]


class PluginInstanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    gateway_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    plugin_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    instance_scope: Literal["gateway", "workspace"]
    workspace_id: str | None = None
    instance_id: str = Field(min_length=1)
    management_protocol_version: Literal[1]
    extension_contract_version: Literal[1]
    status: Literal["stopped", "starting", "ready", "stopping", "failed"]
    process_id: int = Field(gt=0)
    capabilities: list[str] = Field(default_factory=list)
    services: list[PluginServiceContribution] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_scope(self) -> PluginInstanceView:
        if (self.instance_scope == "workspace") != (self.workspace_id is not None):
            raise ValueError("workspace 插件回执必须且只能绑定 workspace_id")
        return self


class PluginCommandCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    gateway_id: str = Field(min_length=1)
    gateway_connection_id: str | None = None
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    plugin_id: str = Field(min_length=1)
    workspace_id: str | None = None
    instance_id: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class PluginCommandCallRequest(PluginCommandCancelRequest):
    arguments: dict[str, JsonValue]
    remaining_ms: int = Field(strict=True, gt=0, le=2_147_483_647)
    session_id: str | None = Field(default=None, min_length=1)
    thread_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def context_belongs_to_workspace(self) -> PluginCommandCallRequest:
        if (self.session_id is None) != (self.thread_id is None):
            raise ValueError("命令会话上下文必须同时提供 session_id 和 thread_id")
        if self.session_id is not None and self.workspace_id is None:
            raise ValueError("命令会话上下文必须具有明确工作区")
        return self


class PluginCommandCancelDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    call_id: str
    accepted: bool


class PluginCommandEventDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: Literal["started", "progress", "result", "error", "cancelled"]
    completed: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, ge=0)
    message: str | None = None
    result: JsonValue = None
    error_code: str | None = None

    @model_validator(mode="after")
    def event_fields_match_kind(self) -> PluginCommandEventDTO:
        if self.kind != "result" and self.result is not None:
            raise ValueError("只有 result 事件可包含命令结果")
        if (self.error_code is not None) != (self.kind == "error"):
            raise ValueError("error 事件必须且只能携带 error_code")
        if self.kind != "progress" and (self.completed is not None or self.total is not None):
            raise ValueError("只有 progress 事件可包含进度数值")
        return self


class PluginShutdownDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    process_id: int = Field(gt=0)
    accepted: bool


class PluginInstanceStatus(PluginInstanceView):
    log_sink: dict[str, JsonValue] | None = None


Progress = Callable[[float, float | None, str | None], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PluginCommandContext:
    call: PluginCommandCallRequest
    request_id: str
    progress: Progress
    parameters: dict[str, JsonValue]


CommandHandler = Callable[[dict[str, JsonValue], PluginCommandContext], Awaitable[JsonValue]]
logger = logging.getLogger(__name__)


def plugin_command_router(
    handlers: Mapping[str, CommandHandler], *, shutdown: Callable[[], Awaitable[None]],
) -> APIRouter:
    handlers = dict(handlers)
    parameters = TypeAdapter(dict[str, JsonValue]).validate_json(os.environ.get("BOXTEAM_PLUGIN_PARAMETERS", "{}"))
    identity = PluginInstanceStatus.model_validate({
        **json.loads(os.environ["BOXTEAM_PLUGIN_INSTANCE"]),
        "status": "ready", "process_id": os.getpid(), "capabilities": ["status", "shutdown", "commands", "observation"],
    })
    declarations = [PluginCommandContribution.model_validate(item)
                    for item in json.loads(os.environ["BOXTEAM_PLUGIN_COMMANDS"])]
    if set(handlers) != {command.handler for command in declarations}:
        raise ValueError("实际命令 handler 与插件清单不一致")
    commands = {command.id: command for command in declarations}
    arguments_validators = {command.id: Draft202012Validator(command.arguments_schema) for command in declarations}
    result_validators = {command.id: Draft202012Validator(command.result_schema) for command in declarations}
    registrations = {command.id: PluginCommandRegistration(
        command_id=command.id, handler=command.handler, registration_id=uuid4().hex,
    ) for command in declarations}
    active: dict[str, tuple[PluginCommandCallRequest, asyncio.Task[None]]] = {}
    router = APIRouter(include_in_schema=False,
        dependencies=[Depends(verify_local_control_request), Depends(verify_runtime_control_token)])
    router.include_router(runtime_logging_router(prefix="/internal/runtime-logging", verify_token=verify_runtime_control_token))
    router.include_router(plugin_observation_router(instance_id=identity.instance_id, service=identity.plugin_id,
        verify_local=verify_local_control_request, verify_token=verify_runtime_control_token))

    @router.get("/internal/plugin/status")
    async def status() -> PluginInstanceStatus:
        return identity.model_copy(update={"log_sink": asdict(runtime_logging_state())["log_sink"]})

    @router.get("/internal/plugin/commands")
    async def registered() -> PluginCommandRegistrationsDTO:
        return PluginCommandRegistrationsDTO(instance_id=identity.instance_id, commands=list(registrations.values()))

    @router.post("/internal/plugin/shutdown")
    async def stop(background: BackgroundTasks) -> PluginShutdownDTO:
        background.add_task(shutdown)
        return PluginShutdownDTO(accepted=True, process_id=os.getpid(), instance_id=identity.instance_id)

    def check(payload: PluginCommandCallRequest | PluginCommandCancelRequest) -> None:
        if (payload.gateway_id, payload.package_id, payload.package_version, payload.plugin_id,
            payload.workspace_id, payload.instance_id) != (
            identity.gateway_id, identity.package_id, identity.package_version, identity.plugin_id,
            identity.workspace_id, identity.instance_id,
        ):
            raise HTTPException(409, "命令不属于原插件实例")
        registration = registrations.get(payload.command_id)
        if registration is None or registration.registration_id != payload.registration_id:
            raise HTTPException(409, "具名命令注册已经失效")

    @router.post("/internal/plugin/commands/cancel")
    async def cancel(payload: PluginCommandCancelRequest) -> PluginCommandCancelDTO:
        check(payload)
        current = active.get(payload.call_id)
        if current is not None:
            call, task = current
            if call.command_id != payload.command_id or call.registration_id != payload.registration_id:
                raise HTTPException(409, "取消目标与原调用不符")
            if not task.cancelling():
                task.cancel()
        return PluginCommandCancelDTO(instance_id=identity.instance_id, call_id=payload.call_id,
                                      accepted=current is not None)

    @router.post("/internal/plugin/commands/call")
    async def call(payload: PluginCommandCallRequest, request: Request) -> StreamingResponse:
        check(payload)
        try:
            arguments_validators[payload.command_id].validate(payload.arguments)
        except ValidationError as error:
            raise HTTPException(422, failure_message(error)) from error
        if payload.call_id in active:
            raise HTTPException(409, "原命令调用仍在进行，不重复执行")
        request_id = get_request_id(request)
        deadline = asyncio.get_running_loop().time() + payload.remaining_ms / 1000

        async def stream() -> AsyncIterator[bytes]:
            if payload.call_id in active:
                raise RuntimeError("原命令调用已开始，不重复执行")
            queue: asyncio.Queue[PluginCommandEventDTO] = asyncio.Queue(maxsize=16)
            sequence = 0
            emission = asyncio.Lock()

            async def emit(kind: str, **fields: object) -> None:
                nonlocal sequence
                async with emission:
                    await queue.put(PluginCommandEventDTO.model_validate({
                        "instance_id": identity.instance_id, "registration_id": payload.registration_id,
                        "command_id": payload.command_id, "call_id": payload.call_id,
                        "request_id": request_id, "sequence": sequence + 1, "kind": kind, **fields,
                    }))
                    sequence += 1

            async def progress(completed: float, total: float | None = None, message: str | None = None) -> None:
                await emit("progress", completed=completed, total=total, message=message)

            async def execute() -> None:
                try:
                    async with asyncio.timeout_at(deadline):
                        await emit("started")
                        result = await handlers[commands[payload.command_id].handler](payload.arguments,
                            PluginCommandContext(payload, request_id, progress, parameters))
                        result_validators[payload.command_id].validate(result)
                        await emit("result", result=result)
                except asyncio.CancelledError:
                    await emit("cancelled")
                except Exception as error:  # noqa: BLE001 — 原 owner 边界结算任意失败，并报告安全诊断。
                    detail = bounded_failure_text(f"插件命令失败: command={payload.command_id} call={payload.call_id}", max_bytes=1024)
                    logger.error("%s\n%s", detail, format_failure_diagnostic(
                        error, max_bytes=DIAGNOSTIC_BYTES - len(detail.encode("utf-8")) - 1,
                    ))
                    await emit("error", error_code="deadline-exceeded" if isinstance(error, TimeoutError) else "handler-failed",
                               message=failure_message(error))

            task = asyncio.create_task(execute())
            active[payload.call_id] = payload, task
            try:
                while True:
                    pending = asyncio.create_task(queue.get())
                    try:
                        await asyncio.wait((pending, task), return_when=asyncio.FIRST_COMPLETED)
                        if not pending.done() and task.done() and queue.empty():
                            if task.cancelled():
                                if sequence == 0:
                                    await emit("started")
                                await emit("cancelled")
                            else:
                                await task
                                raise RuntimeError("插件命令退出但没有返回终态")
                        event = await pending
                    finally:
                        if not pending.done():
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                    yield (event.model_dump_json() + "\n").encode()
                    if event.kind in {"result", "error", "cancelled"}:
                        break
            finally:
                async def settle() -> None:
                    if not task.done():
                        task.cancel()
                        while not queue.empty():
                            queue.get_nowait()
                    try:
                        if not task.cancelled():
                            await task
                    finally:
                        active.pop(payload.call_id, None)

                await run_cleanup_shielded(settle)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return router


__all__ = [
    "PluginCommandCallRequest", "PluginCommandCancelDTO", "PluginCommandCancelRequest",
    "PluginCommandContext", "PluginCommandContribution", "PluginCommandEventDTO",
    "PluginCommandRegistration", "PluginCommandRegistrationsDTO",
    "PluginInstanceStatus", "PluginInstanceView", "PluginServiceContribution", "PluginShutdownDTO",
    "plugin_command_router",
]
