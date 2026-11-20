"""独立插件资产：真实维护写盘、具名命令和原 SDK 管理入口。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import boxteam_plugin_sdk
import uvicorn
from boxteam_plugin_sdk.commands import plugin_command_router
from boxteam_plugin_sdk.lifecycle import LifetimeScope
from boxteam_plugin_sdk.logging import configure_application_logging
from boxteam_plugin_sdk.maintenance import MaintenanceTask, start_maintenance
from boxteam_plugin_sdk.request_context import (
    reset_current_request_id,
    set_current_request_id,
)
from fastapi import FastAPI, Request

configure_application_logging(service="offline-sdk-probe")
logger = logging.getLogger("offline-sdk-probe")
scope = LifetimeScope("offline-plugin")
receipt = Path(os.environ["MAINTENANCE_RECEIPT"])
count = 0


async def maintain() -> None:
    global count
    count += 1
    pending = receipt.with_suffix(".pending")
    pending.write_text(json.dumps({"count": count, "cwd": str(Path.cwd())}), encoding="utf-8")
    pending.replace(receipt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_maintenance([MaintenanceTask("write-receipt", 20, True, maintain)], scope=scope,
        identity="offline-plugin", report_error=lambda task, identity, elapsed, error: logger.error(
            "维护失败: task=%s identity=%s elapsed=%s", task, identity, elapsed, exc_info=error))
    try:
        yield
    finally:
        await scope.close()
        logger.warning("MAINTENANCE_DRAINED count=%s", count)


async def inspect(arguments, context):
    logger.debug("SDK_DEBUG_MARKER request=%s", context.request_id)
    logger.warning("SDK_WARNING_MARKER request=%s", context.request_id)
    return {"count": count, "request_id": context.request_id, "sdk": boxteam_plugin_sdk.__file__,
        "contract": json.loads(files("boxteam_plugin_sdk").joinpath("resources/runtime-contract.json").read_text()),
        "cwd": str(Path.cwd())}


async def shutdown() -> None:
    server.should_exit = True


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def request_identity(request: Request, call_next):
    # 此独立插件的 HTTP owner 接受调用方身份；不导入产品中间件或补造身份。
    request.state.request_id = request.headers["X-Request-ID"]
    token = set_current_request_id(request.state.request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response
    finally:
        reset_current_request_id(token)


app.include_router(plugin_command_router({"inspect": inspect}, shutdown=shutdown))
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
    port=int(os.environ["BOXTEAM_PLUGIN_PORT"]), log_config=None))
server.run()
if not server.started or server.lifespan.shutdown_failed:
    raise SystemExit(3)
