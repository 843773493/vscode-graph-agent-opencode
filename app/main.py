from __future__ import annotations
from fastapi import FastAPI

from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.jobs import router as jobs_router
from app.api.messages import router as messages_router
from app.api.runtime import router as runtime_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router

app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)

app.include_router(workspace_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(tools_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")


@app.on_event("startup")
async def startup_event():
    # 初始化工作区目录
    from app.core import path_utils
    path_utils.initialize_directories()


if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
