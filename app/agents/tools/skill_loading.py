"""模型可见的 name-only Skill 激活工具。

结果 schema 固定为:status/name/mode/display_uri/revision/content_hash/
append_status/tracked/queued/error。display_uri 只能是 boxteam:// 虚拟
资源 URI;结果永远不含物理路径、provider locator、credential 或正文。

snapshot/tracked 从发起调用时已冻结的 Turn/ModelCall SkillCatalog
activation snapshot 解析 exact binding;工具路径零 I/O,不读当前目录,
也不读当前 catalog。untrack 按当前 thread 与受校验逻辑 name 定位唯一
active tracked registration,不解析当前 effective entry,不读 source。
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
    ContextSourceTrackingStateConflict,
    SkillCatalogSnapshotConflict,
    SkillLoadMode,
)


class SkillLoadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="要激活的 Skill 名称，不是文件路径。")
    mode: SkillLoadMode = Field(
        default="snapshot",
        description="snapshot 只加载一次；tracked 接收后续已观察 revision；untrack 停止跟踪但保留已注入内容。",
    )


_RESULT_KEYS = (
    "status",
    "name",
    "mode",
    "display_uri",
    "revision",
    "content_hash",
    "append_status",
    "tracked",
    "queued",
    "error",
)


def _error_result(name: str, mode: str, code: str, message: str) -> str:
    """固定的显式错误结果;不伪造成功,也不泄露 locator/正文。"""
    return json.dumps(
        {
            "status": "error",
            "name": name,
            "mode": mode,
            "display_uri": None,
            "revision": None,
            "content_hash": None,
            "append_status": "none",
            "tracked": False,
            "queued": False,
            "error": {"code": code, "message": message},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def create_skill_load_tool(manager: ContextSourceManager) -> BaseTool:
    @tool("skill_load", args_schema=SkillLoadInput)
    def skill_load(name: str, mode: SkillLoadMode = "snapshot") -> str:
        """按冻结 activation snapshot 解析 Skill binding；正文不经文件工具读取。"""
        try:
            receipt = manager.load_skill(name, mode)
        except KeyError as error:
            return _error_result(name, mode, "skill-not-found", str(error))
        except SkillCatalogSnapshotConflict as error:
            return _error_result(name, mode, error.code, str(error))
        except ContextSourceTrackingStateConflict as error:
            return _error_result(name, mode, error.code, str(error))

        # not_tracked/already_active 是确定性结果状态；error 只在 status=="error" 时非空。
        return json.dumps(
            {
                "status": receipt.status,
                "name": receipt.name,
                "mode": receipt.mode,
                "display_uri": receipt.display_uri,
                "revision": receipt.revision,
                "content_hash": receipt.content_hash,
                "append_status": receipt.append_status,
                "tracked": receipt.tracked,
                "queued": receipt.queued,
                "error": None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return skill_load


__all__ = ["SkillLoadInput", "create_skill_load_tool"]

