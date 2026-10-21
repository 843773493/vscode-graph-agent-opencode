"""先绑定回放控制，再安装既有通信故障观测；业务仍由真实应用执行。"""

# isort: off
from tests.support.e2e_control.backend_app import app
from tests.support import communication_fault_backend  # noqa: F401
# isort: on

from app.services.orchestration.execution_step import runner
from tests.support.e2e_control.tool_observations import ObservedCanonicalItemSink

runner.CanonicalItemSink = ObservedCanonicalItemSink

__all__ = ["app"]
