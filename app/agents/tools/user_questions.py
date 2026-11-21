"""非阻塞提问工具，不涉及审批、答案提交或执行等待。"""

from langchain_core.tools import BaseTool, tool

from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.execution_admission_context import require_current_execution_admission
from app.services.business.communication.user_questions import UserQuestionService


def create_ask_user_async_tool(service: UserQuestionService, context: ToolInvocationContext) -> BaseTool:
    @tool
    async def ask_user_async(markdown: str) -> dict[str, str]:
        """留下希望用户稍后阅读的问题与说明，用 Markdown 加粗重点；一次调用保存一条留言。

        成功只表示已保存，不表示用户已回答或授权。不要等待、轮询或重复提问；
        继续能独立完成的工作，没有工作时正常结束。用户会通过普通消息回复。
        留言不会暂停持续任务；确实必须等用户时，通过既有任务工具显式暂停。
        仅主 Agent 使用，子 Agent 应向父 Agent 汇报问题。
        """
        binding = context.thread_binding
        if binding is None:
            raise RuntimeError("异步留言缺少执行 Thread")
        admission = require_current_execution_admission(langgraph_thread_id=binding.thread_id)
        if (admission.session_id, admission.thread_id) != (binding.session_id, binding.thread_id):
            raise RuntimeError("异步留言调用与执行归属不一致")
        question_id = await service.create(binding.session_id, binding.thread_id,
            question_id=context.require_operation_id("ask_user_async"), markdown=markdown,
            model_call_id=context.require_model_call_id(), turn_id=admission.turn_id)
        return {"question_id": question_id, "status": "saved"}

    return ask_user_async
