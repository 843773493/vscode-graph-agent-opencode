from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ToolDTO(BaseModel):
    tool_id: str
    name: str
    description: str | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    category: str | None = None
    group_id: str = "default"
    group_name: str = "默认工具"
    kind: str = "default"
    enabled: bool = True
    test_supported: bool = False


class ToolSelectionChange(BaseModel):
    tool_id: str = Field(min_length=1)
    enabled: bool


class ToolSelectionPatchRequest(BaseModel):
    agent_id: str = "default"
    changes: list[ToolSelectionChange] = Field(min_length=1)

    @field_validator("changes")
    @classmethod
    def reject_duplicate_tools(
        cls,
        changes: list[ToolSelectionChange],
    ) -> list[ToolSelectionChange]:
        tool_ids = [change.tool_id for change in changes]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("工具开关变更不能包含重复 tool_id")
        return changes
