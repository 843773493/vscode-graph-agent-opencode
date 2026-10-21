"""只观察真实 canonical 工具结果提交；导入时不装配应用。"""

from app.abstractions.turn_terminal_status import ToolResultCompletion
from app.core.phase_observer import observe_phase
from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.execution_step.stream_bindings import CanonicalItemSink


class ObservedCanonicalItemSink(CanonicalItemSink):
    async def complete_tool_result(self, completion: ToolResultCompletion) -> CanonicalItemRecord:
        item = await super().complete_tool_result(completion)
        # 只观察真实owner已提交的结果，不构造返回值或补写业务事实。
        await observe_phase(completion.tool_call_id, "test.tool_result.committed", details={
            "session_id": self._session_id, "thread_id": self._thread_id, "item": item.to_dict(),
        })
        return item
