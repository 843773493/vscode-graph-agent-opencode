"""Chromium 局部组件集成：真实文件 owner，Gateway 认证/身份入口为显式受控替身。"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from app.api import workspace as workspace_api
from app.api import workspace_observation
from app.api.deps import (
    get_config_service,
    get_session_service,
    get_workspace_activity_service,
    get_workspace_file_watch_service,
    get_workspace_service,
    verify_local_token,
)
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.support.workspaces import prepare_default_test_workspace


def make_app(output_root: Path) -> FastAPI:
    services = {}
    watchers = {}
    gates = {}
    requests = []
    for name in ("a", "b"):
        route = "gw-files-" + name
        root = prepare_default_test_workspace(
            workspace_root=output_root / "workspace" / name,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        for directory in ("target", "other", "cold"):
            (root / directory).mkdir()
        for name in ("source-copy.txt", "source-paste.txt"):
            (root / name).write_text("真实集合写入口样本\n")
        config = Mock(spec=ConfigService)
        config.get_workspace_file_default_limit.return_value = 500
        config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
        config.get_workspace_preview_binary_sample_bytes.return_value = 8192
        service = WorkspaceService(config_service=config, workspace_root=root)
        gate = Event()
        gate.set()
        original = service.file_operations._execute

        def execute(record, *, execute_original=original, execute_gate=gate):
            if not execute_gate.wait(60):
                raise RuntimeError("浏览器集成的 worker gate 未在有界时间释放")
            execute_original(record)

        service.file_operations._execute = execute
        services[route] = service
        gates[route] = gate
        watchers[route] = WorkspaceFileWatchService(workspace_root=root)

    @asynccontextmanager
    async def lifespan(_app):
        for service in services.values():
            await service.start()
        try:
            yield
        finally:
            for gate in gates.values():
                gate.set()
            for watcher in watchers.values():
                await watcher.shutdown()
            for service in services.values():
                await service.stop()
                service.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.include_router(workspace_observation.router, prefix="/api/v1")

    def route_for(request: Request) -> str:
        route = request.headers.get("X-BoxTeam-Workspace-Id")
        if route not in services:
            raise HTTPException(409, "测试必须显式指定固定 Gateway workspace ID")
        return route

    def workspace_service(request: Request):
        return services[route_for(request)]

    def watch_service(request: Request):
        return watchers[route_for(request)]

    app.dependency_overrides[get_workspace_service] = workspace_service
    app.dependency_overrides[get_workspace_file_watch_service] = watch_service
    app.dependency_overrides[verify_local_token] = lambda: "controlled-browser-token"
    app.dependency_overrides[get_config_service] = lambda: config
    app.dependency_overrides[get_session_service] = lambda: Mock()
    app.dependency_overrides[get_workspace_activity_service] = lambda: Mock()

    @app.middleware("http")
    async def record_request(request: Request, call_next):
        if request.url.path.startswith("/api/v1/"):
            requests.append({"path": request.url.path, "query": request.url.query,
                             "route": request.headers.get("X-BoxTeam-Workspace-Id"),
                             "method": request.method})
        return await call_next(request)

    def envelope(request: Request, data):
        return {"request_id": request.state.request_id, "data": data}

    @app.get("/api/gateway/auth/local-credential")
    async def credential(request: Request):
        return envelope(request, {"token": "controlled-browser-token"})

    @app.get("/api/gateway/users/current")
    async def user(request: Request):
        return envelope(request, {"kind": "guest", "user_id": None})

    @app.get("/api/gateway/health")
    async def health(request: Request):
        return envelope(request, {"gateway_id": "controlled-file-browser-gateway"})

    @app.get("/control/info")
    async def info():
        return {route: {"workspace_id": service.workspace_id, "root_path": service.root_path, "uri": service.file_paths.workspace_uri}
                for route, service in services.items()}

    @app.post("/control/{route}/gate/{state}")
    async def gate_control(route: str, state: str):
        if state == "pause":
            gates[route].clear()
        elif state == "release":
            gates[route].set()
        else:
            raise HTTPException(400, "未知 gate 状态")
        return {"state": state}

    @app.get("/control/{route}/inspect")
    async def inspect(route: str):
        owner = services[route].file_operations
        with owner.store._lock:
            ids = [row[0] for row in owner.store._db.execute("SELECT operation_id FROM operations ORDER BY queue_seq")]
        return {"operations": [owner.store.get(operation_id, "local").receipt.model_dump()
                               for operation_id in ids],
                "requests": list(requests), "worker_state": owner.worker_state,
                "worker_error": owner.worker_error}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(make_app(args.output_root), host="127.0.0.1", port=args.port, log_level="warning")
