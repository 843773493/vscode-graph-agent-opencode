"""LangGraph `configurable` 工具。

LangGraph 1.2 在 `config["configurable"]` 中只识别以下键：
- `thread_id`（必需）：会话标识
- `checkpoint_ns`：命名空间（默认空字符串）
- `checkpoint_id`：定位到具体 checkpoint（默认 `None`，saver 选最新）

其它业务键（session_id、job_id、user_id 等）应当通过以下方式传递：
- 进程内：`contextvars`（如 `app.core.job_context.set_current_job_id`）
- 跨节点：图的 state schema（TypedDict + reducer）

本模块只暴露 `build_checkpoint_config`，所有需要 `config` 的代码统一调用它，
避免散落 4+ 处独立构造 `{"configurable": {"thread_id": ...}}` 的代码。
"""
from __future__ import annotations

from typing import Any


def build_checkpoint_config(
    thread_id: str,
    *,
    checkpoint_ns: str = "",
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """构造 LangGraph 标准 `configurable` 字典。

    注意：**不要**往里塞业务键（session_id / job_id / user_id），这些应当通过
    `contextvars` 或图 state schema 传递 —— 详见本模块顶部 docstring。
    """
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
    }
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}
