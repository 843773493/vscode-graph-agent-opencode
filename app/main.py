from __future__ import annotations
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
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

load_project_env(__file__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.core import path_utils
    from app.runtime import clear_app_services, init_app_services

    path_utils.initialize_directories()
    init_app_services()

    from app.runtime import get_config_service

    workspace_root = os.environ.get("WORKSPACE_ROOT", "") or None
    config_service = get_config_service()

    if workspace_root:
        config_service._workspace_root = workspace_root
        config_service._apply_workspace_override(workspace_root)

    try:
        config_service.validate_boxteam_config()
    except Exception as e:
        import logging

        logging.warning(f"boxteam.json 配置验证失败: {e}")

    yield

    clear_app_services()

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
    uvicorn.run(app, host="127.0.0.1", port=8000)
