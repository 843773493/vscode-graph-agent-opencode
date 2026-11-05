"""直接调用原 session 工厂的测试显式持有生命周期身份。"""

import pytest

from app.core.child_process import OwnedChildProcess
from app.services.infrastructure.mcp.session_runtime import McpSessionLifecycle


@pytest.fixture
def mcp_session_lifecycle() -> McpSessionLifecycle:
    async def observed() -> None:
        return None

    return McpSessionLifecycle(OwnedChildProcess("mcp-connection:unit-test"), observed)
