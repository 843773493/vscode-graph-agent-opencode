from __future__ import annotations
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.trace_middleware import TraceMiddleware
from app.container import build_app_container
from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.jobs import router as jobs_router
from app.api.messages import router as messages_router
from app.api.runtime import router as runtime_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router
from app.core.env import load_project_env

load_project_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.core import path_utils

    path_utils.initialize_directories()
    workspace_root = os.environ.get("WORKSPACE_ROOT", "") or None
    container = build_app_container(
        project_root=Path.cwd(),
        workspace_root=workspace_root,
    )
    _.state.container = container

    container.config_service.validate_boxteam_config()

    await container.trace_event_recorder.start()

    yield

    await container.trace_event_recorder.stop()
    _.state.container = None

app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    lifespan=lifespan,
)

# Add trace middleware
app.add_middleware(TraceMiddleware)

# 允许本地前端开发服务器通过浏览器跨域访问后端接口。
# TODO: 这会放宽为允许所有来源，仅适合本地开发；若后续引入生产部署，请改成可配置项。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health", summary="健康检查")
async def health():
    return {"status": "ok"}

app.include_router(workspace_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(tools_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010)
