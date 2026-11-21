"""会话停止策略；动作仍由各资源 owner 执行。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

ResourceStopAction = Literal["keep", "stop", "delete", "release"]


class SessionStopPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["thread", "session"]
    jobs: Literal["interrupt", "cancel", "delete"]
    pending_messages: Literal["continue", "keep", "discard"]
    debug: Literal["keep", "stop"]
    background_task: Literal["keep", "stop", "delete"]
    terminal: ResourceStopAction
    browser: ResourceStopAction
    collaboration: Literal["keep", "stop"]
    goal: Literal["keep", "pause"]
    thread_runtime: Literal["keep", "close"]

    @model_validator(mode="after")
    def validate_scope(self) -> SessionStopPolicy:
        if self.scope == "thread" and self.collaboration != "keep":
            raise ValueError("协作排空属于整个 Session，须使用 session scope")
        if self.jobs == "delete" and self.pending_messages != "discard":
            raise ValueError("删除 Job 记录必须同时丢弃对应待处理消息")
        if self.pending_messages == "continue" and self.jobs != "interrupt":
            raise ValueError("只有普通 interrupt 可以自动继续待处理消息")
        if self.thread_runtime == "close" and any(action in {"keep", "stop"} for action in (self.terminal, self.browser)):
            raise ValueError("关闭 Thread runtime 会释放 Thread 保留资源，terminal/browser 须选择 release 或 delete")
        return self


# 删除数据前的必要排空不能被配置关闭；仅普通 interrupt/stop 的动作可调整。
DELETE_SESSION_POLICY = SessionStopPolicy(
    scope="session", jobs="delete", pending_messages="discard", debug="stop",
    background_task="delete", terminal="release", browser="release",
    collaboration="stop", goal="keep", thread_runtime="close",
)
