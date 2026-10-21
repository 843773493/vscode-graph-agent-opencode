"""先绑定回放控制，再安装既有通信故障观测；业务仍由真实应用执行。"""

# isort: off
from tests.support.e2e_control.backend_app import app
from tests.support import communication_fault_backend  # noqa: F401
# isort: on

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.phase_observer import observe_phase
from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.execution_step import runner
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink


class ObservedCanonicalItemSink(CanonicalItemSink):
    async def complete_tool_result(self, completion: ToolResultCompletion) -> CanonicalItemRecord:
        item = await super().complete_tool_result(completion)
        # 只观察真实owner已提交的结果，不构造返回值或补写业务事实。
        await observe_phase(completion.tool_call_id, "test.tool_result.committed", details={
            "session_id": self._session_id, "thread_id": self._thread_id, "item": item.to_dict(),
        })
        return item


runner.CanonicalItemSink = ObservedCanonicalItemSink

__all__ = ["app"]
