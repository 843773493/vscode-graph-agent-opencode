"""给用户的异步 Markdown 留言；不承载答案或审批状态。"""

from pydantic import BaseModel, ConfigDict, Field


class UserQuestionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    sequence: int
    markdown: str
    turn_id: str | None
    created_at: str


class UserQuestionSummaryDTO(BaseModel):
    session_id: str
    thread_id: str
    count: int = Field(ge=0)
    revision: int = Field(ge=0)


class UserQuestionPageDTO(UserQuestionSummaryDTO):
    items: list[UserQuestionDTO]
    next_before: int | None
