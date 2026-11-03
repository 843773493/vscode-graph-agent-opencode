"""工具结果的历史身份与不可变完整输出读取，不执行或重放工具。"""

import hashlib
from collections.abc import Mapping

from app.core.resource_uri import parse_session_artifact_uri
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.mapping.itemized.history_turns.tools import history_tool_call_id


class ToolOutputChangedError(ValueError):
    """canonical 引用对应的完整工具输出已不再是原版本。"""


def tool_result_identity(item: CanonicalItemRecord) -> dict[str, str]:
    payload = item.payload if isinstance(item.payload, Mapping) else {}
    evidence = item.tool_result_evidence
    call_id = evidence.tool_call_id if evidence is not None else payload["tool_call_id"]
    return {"tool_call_id": history_tool_call_id(item.projection_identity or ItemProjectionIdentity(), call_id),
            "tool_name": str(payload.get("name") or "tool")}


def read_tool_result_output(
    item: CanonicalItemRecord, store: ToolOutputStore | None, remaining_bytes: int,
) -> tuple[str | None, int] | None:
    """只沿 canonical 的输出引用读一次；预算不足时返回所需字节而不打开文件。"""
    payload = item.payload
    artifact = payload.get("artifact") if isinstance(payload, Mapping) else None
    reference = artifact.get("tool_output") if isinstance(artifact, Mapping) else None
    if reference is None:
        return None
    if not isinstance(reference, Mapping):
        raise TypeError("canonical 工具结果的完整输出引用格式无效")
    byte_count = reference["byte_count"]
    if type(byte_count) is not int or byte_count < 0:
        raise ValueError("工具结果输出引用缺少有效字节量")
    if byte_count > remaining_bytes:
        return None, byte_count
    if store is None:
        raise RuntimeError("工具结果搜索未装配原 ToolOutputStore")
    uri = reference["read_path"]
    parsed = parse_session_artifact_uri(uri)
    result = store.read(parsed.tail_segments[0], uri, byte_count)
    # 同一次读取核对不可变版本；不增加 stat、二次读取或每键预读。
    if len(result.data) != byte_count or hashlib.sha256(result.data).hexdigest() != reference["content_sha256"]:
        raise ToolOutputChangedError("工具结果的不可变完整输出已变化，请核查原结果")
    return result.data.decode("utf-8"), byte_count
