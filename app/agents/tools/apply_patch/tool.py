from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.tools.apply_patch.executor import apply_patch_text


APPLY_PATCH_TOOL_NAME = "apply_patch"


class ApplyPatchInput(BaseModel):
    input: str = Field(description="要应用的 V4A 文件补丁。")
    explanation: str = Field(description="本次工具调用要实现的目标的简短说明。")


APPLY_PATCH_DESCRIPTION = """编辑文本文件。不要使用此工具编辑 Jupyter notebook。
apply_patch 使用专用的 V4A diff 格式修改文件。input 必须使用以下结构：

*** Begin Patch
[一个或多个文件操作]
*** End Patch

文件路径必须是相对于工作区根目录的路径，不能以 / 开头，也不能使用宿主机绝对路径。
每个文件操作必须使用以下一种路径头：
*** Add File: src/new_file.py - 创建文件，后续每一行都必须以 + 开头。
*** Delete File: src/old_file.py - 删除文件，后面不能跟内容。
*** Update File: src/main.py - 更新文件，可紧跟 *** Move to: src/new_main.py。

Update File 由一个或多个 @@ hunk 组成。上下文行以空格开头，删除行以 - 开头，新增行以 + 开头。
默认在修改前后提供三行上下文；上下文不唯一时，在 @@ 后写类、函数或其它定位行；仍不唯一时可以连续使用多个 @@。
不要使用行号。必须保持原文件的缩进风格。一次调用可以包含多个文件操作。"""


def create_apply_patch_tool(*, workspace_root: Path | None = None) -> BaseTool:
    def apply_patch(input: str, explanation: str) -> str:
        """使用 VS Code 的 V4A apply_patch 格式修改工作区文本文件。"""
        result = apply_patch_text(
            input,
            explanation=explanation,
            workspace_root=workspace_root,
        )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool.from_function(
        func=apply_patch,
        name=APPLY_PATCH_TOOL_NAME,
        description=APPLY_PATCH_DESCRIPTION,
        args_schema=ApplyPatchInput,
    )
