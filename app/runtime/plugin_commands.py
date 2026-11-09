"""Python 插件在自身 HTTP 服务内注册具名命令；不管理进程或持久执行。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue, TypeAdapter

from app.api.runtime_control import (
    verify_local_control_request,
    verify_runtime_control_token,
)
from app.api.runtime_logging import runtime_logging_router
from app.core.logging_config import runtime_logging_state
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.trace_middleware import get_request_id
from app.runtime.plugin_observation import plugin_observation_router
from app.schemas.gateway.plugin_commands import (
    PluginCommandCallRequest,
    PluginCommandCancelDTO,
    PluginCommandCancelRequest,
    PluginCommandEventDTO,
    PluginCommandRegistration,
    PluginCommandRegistrationsDTO,
)
from app.schemas.gateway.service_plugin import (
    PluginCommandContribution,
    PluginInstanceStatus,
)

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
    """handler 集合在启动边界冻结，必须逐项匹配 Gateway 交付的清单。"""
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

    @router.get("/internal/plugin/status")
    async def status() -> PluginInstanceStatus:
        return identity.model_copy(update={"log_sink": asdict(runtime_logging_state())["log_sink"]})

    @router.get("/internal/plugin/commands")
    async def registered() -> PluginCommandRegistrationsDTO:
        return PluginCommandRegistrationsDTO(instance_id=identity.instance_id, commands=list(registrations.values()))

    @router.post("/internal/plugin/shutdown")
    async def stop(background: BackgroundTasks) -> dict[str, object]:
        background.add_task(shutdown)
        return {"accepted": True, "process_id": os.getpid(), "instance_id": identity.instance_id}

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
            raise HTTPException(422, error.message) from error
        if payload.call_id in active:
            raise HTTPException(409, "原命令调用仍在进行，不重复执行")
        request_id = get_request_id(request)
        deadline = asyncio.get_running_loop().time() + payload.remaining_ms / 1000

        async def stream() -> AsyncIterator[bytes]:
            if payload.call_id in active:
                raise RuntimeError("原命令调用已开始，不重复执行")
            # 背压与实际 handler 共用当前连接；不留后台续跑或结果账本。
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
                except Exception as error:
                    logger.exception("插件命令失败: command=%s call=%s", payload.command_id, payload.call_id)
                    await emit("error", error_code="deadline-exceeded" if isinstance(error, TimeoutError) else "handler-failed",
                               message=str(error))

            task = asyncio.create_task(execute())
            active[payload.call_id] = payload, task
            try:
                while True:
                    pending = asyncio.create_task(queue.get())
                    try:
                        await asyncio.wait((pending, task), return_when=asyncio.FIRST_COMPLETED)
                        if not pending.done() and task.done() and queue.empty():
                            if task.cancelled():
                                # 首次调度前取消也有配对终态，不运行 handler。
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
                        # 断连时不再等待进度队列空间，取消只结清原 handler。
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
