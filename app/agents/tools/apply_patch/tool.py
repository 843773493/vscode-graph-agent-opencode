from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.agents.tools.apply_patch.executor import apply_patch_text
from app.agents.tools.apply_patch.models import DiffError
from app.core.resource_uri import ResourceUriGrammarError
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.text_publication import (
    WorkspaceFileConflictError,
)

APPLY_PATCH_TOOL_NAME = "apply_patch"


class ApplyPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(description="要应用的 V4A 文件补丁。")
    explanation: str = Field(description="本次工具调用要实现的目标的简短说明。")
    expected_versions: dict[str, Annotated[str, Field(pattern=r"^(absent|sha256:[0-9a-f]{64})$")]] = Field(
        description="全部补丁相对文件名的预期原字节版本；已读文件使用 read_file 的 sha256:<hex>，新文件显式填 absent。移动须覆盖源和目的。")
    target: str = Field(default=".", description="固定目录根：工作区相对路径、目标原生绝对路径或 files 资源 URI。")


APPLY_PATCH_DESCRIPTION = """编辑文本文件。不要使用此工具编辑 Jupyter notebook。
apply_patch 使用专用的 V4A diff 格式修改文件。input 必须使用以下结构：

*** Begin Patch
[一个或多个文件操作]
*** End Patch

target 固定一个目录根，默认当前工作区。补丁内部文件名必须相对该根，禁止绝对路径、URI 和越界。
expected_versions 必须精确覆盖全部文件，值为先前 read_file 公布的 sha256:<hex>，新文件为 absent；移动目的也必须给出版本或 absent。
每个文件操作必须使用以下一种路径头：
*** Add File: src/new_file.py - 创建文件，后续每一行都必须以 + 开头。
*** Delete File: src/old_file.py - 删除文件，后面不能跟内容。
*** Update File: src/main.py - 更新文件，可紧跟 *** Move to: src/new_main.py。

Update File 由一个或多个 @@ hunk 组成。上下文行以空格开头，删除行以 - 开头，新增行以 + 开头。
默认在修改前后提供三行上下文；上下文不唯一时，在 @@ 后写类、函数或其它定位行；仍不唯一时可以连续使用多个 @@。
不要使用行号。必须保持原文件的缩进风格。一次调用可以包含多个文件操作，全部预检后逐文件原子发布。
结果 files 公布每个 URI 的 published/not_published/not_attempted/unknown 与核实到的版本。多文件及移动不保证跨路径事务，失败不自动回滚；部分或未知结果必须重新读取原目标核实，禁止自动重放。"""


def create_apply_patch_tool(*, workspace_root: Path | None = None) -> BaseTool:
    def apply_patch(input: str, explanation: str, expected_versions: dict[str, str], target: str = ".") -> str:
        """使用 VS Code 的 V4A apply_patch 格式修改工作区文本文件。"""
        try:
            result = apply_patch_text(
                input,
                explanation=explanation,
                workspace_root=workspace_root,
                expected_versions=expected_versions,
                target=target,
            )
        except (ResourceUriGrammarError, ResourceUriResolveError) as exc:
            return json.dumps({"status": "error", "effect": "not_executed", "code": exc.reason_code,
                "owner": "resource_uri_grammar" if isinstance(exc, ResourceUriGrammarError) else "resource_uri_resolver",
                "error": str(exc), "retryable": False}, ensure_ascii=False)
        except (OSError, RuntimeError, ValueError) as exc:
            code = ("revision_conflict" if isinstance(exc, WorkspaceFileConflictError) else
                    "invalid_encoding" if isinstance(exc, UnicodeDecodeError) else
                    "invalid_patch" if isinstance(exc, (DiffError, ValueError)) else "execution_failed")
            detail = (f"{type(exc).__name__}: {exc.strerror}, errno={exc.errno}"
                      if isinstance(exc, OSError) and (exc.filename is not None or exc.filename2 is not None) else str(exc))
            return json.dumps(
                {
                    "status": "error",
                    "effect": "not_executed",
                    "code": code,
                    "explanation": explanation,
                    "error_type": type(exc).__name__,
                    "error": detail,
                    "retryable": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    async def async_apply_patch(input: str, explanation: str, expected_versions: dict[str, str], target: str = ".") -> str:
        # 生命周期回调留在所属事件循环；仅阻塞文件发布交工作线程。
        return await asyncio.to_thread(apply_patch, input, explanation, expected_versions, target)

    return StructuredTool.from_function(
        func=apply_patch,
        coroutine=async_apply_patch,
        name=APPLY_PATCH_TOOL_NAME,
        description=APPLY_PATCH_DESCRIPTION,
        args_schema=ApplyPatchInput,
    )
