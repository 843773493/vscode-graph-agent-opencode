"""会话动作的回报内容；结果正文由原 canonical execution reader 提供。"""

from __future__ import annotations

from app.abstractions.internal_message import PreparedInternalMessage
from app.domain.itemized.records import InternalDisplayProvenance
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.session_navigation import SessionGenerationExecuteRequest


def generation_report_message(
    payload: SessionGenerationExecuteRequest,
    *,
    provenance: InternalDisplayProvenance,
    terminal_status: str,
    branch_result: str,
) -> PreparedInternalMessage:
    context = {
        "report_back": payload.session_strategy.report_back,
        **provenance.to_dict(),
        "terminal_status": terminal_status,
    }
    return internal_message_factory.build(
        kind="generated_session_result",
        control=(
            "这是系统直接注入当前会话的分支执行结果，不是来自其它会话的通信请求。"
            "请只在当前会话中基于结果继续处理并回复当前用户；禁止调用 "
            "send_message_to_session、reply_to_session 或其它跨会话通信工具，"
            "也不要重新访问、唤醒或启动生成会话。"
            "分支最终回复已由系统直接读取并注入如下，不要猜测文件路径，"
            "也不需要再次读取其它会话。"
        ),
        sections=(
            PromptSection("control_context", context),
            PromptSection("generated_session_result", branch_result),
        ),
        display_content="生成分支已结束，主会话正在处理返回结果。",
        display_provenance=provenance,
    )
