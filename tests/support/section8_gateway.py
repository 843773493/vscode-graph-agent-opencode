"""真实 Gateway 的测试进程装配：managed Backend 使用同一显式测试 ASGI。"""

from functools import partial

from app.gateway.runtime import local_workspace
from app.gateway.runtime.workspace_process import start_local_backend_process

local_workspace.start_local_backend_process = partial(start_local_backend_process, asgi_app="tests.support.section8_backend:app")

from app.gateway.main import app  # noqa: F401
