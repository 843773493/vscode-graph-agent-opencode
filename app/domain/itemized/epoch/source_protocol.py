"""模型可见来源块的唯一确定性编码；不携带 locator，不解释正文标签。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.validation import _non_empty_string

SOURCE_PROTOCOL = "boxteam-context-sources:v1"


@dataclass(frozen=True, slots=True)
class SourceModelBlock:
    """封存请求的来源表达；身份和信任资格必须来自 owner，正文永不作包头。"""

    source_identity: str
    name: str
    source_kind: str
    operation: Literal["activate", "update", "replace", "restore"]
    revision: str
    base_revision: str | None
    representation: Literal["full", "unified_diff"]
    source_tracking_at_seal: Literal["tracked", "untracked"]
    root_placement: Literal["root_eligible", "tail_only"]
    body: object

    def __post_init__(self) -> None:
        for name in ("source_identity", "name", "source_kind", "revision"):
            _non_empty_string(getattr(self, name), f"SourceModelBlock.{name}")
        if self.base_revision is not None:
            _non_empty_string(self.base_revision, "SourceModelBlock.base_revision")
        if self.operation not in {"activate", "update", "replace", "restore"}:
            raise ItemSchemaError("SourceModelBlock.operation 非法")
        if self.representation not in {"full", "unified_diff"}:
            raise ItemSchemaError("SourceModelBlock.representation 非法")
        if self.source_tracking_at_seal not in {"tracked", "untracked"}:
            raise ItemSchemaError("SourceModelBlock.source_tracking_at_seal 非法")
        if self.root_placement not in {"root_eligible", "tail_only"}:
            raise ItemSchemaError("SourceModelBlock.root_placement 非法")
        if self.representation == "unified_diff" and (
            self.operation != "update" or self.base_revision is None or not isinstance(self.body, str)
        ):
            raise ItemSchemaError("unified_diff 必须绑定 update、基准 revision 和文本正文")
        canonical_json_bytes(self.body)


def render_source_blocks(blocks: Sequence[SourceModelBlock]) -> str:
    """JSON 字符串转义隔离原正文，固定 JCS 编码供 root、tail 和重放共用。"""
    if not blocks or not all(isinstance(block, SourceModelBlock) for block in blocks):
        raise ItemSchemaError("来源封装需要非空 typed blocks")
    return canonical_json_bytes({
        "protocol": SOURCE_PROTOCOL, "sources": [asdict(block) for block in blocks],
    }).decode("utf-8")


def parse_source_blocks(content: object) -> tuple[SourceModelBlock, ...]:
    """只解析已显式登记版本的封存帧，不从原正文猜测协议。"""
    if not isinstance(content, str):
        raise ItemSchemaError("source-mismatch: 来源封装必须是文本")
    raw = json.loads(content)
    if not isinstance(raw, dict) or set(raw) != {"protocol", "sources"} or raw["protocol"] != SOURCE_PROTOCOL:
        raise ItemSchemaError("source-mismatch: 来源协议版本或字段非法")
    if not isinstance(raw["sources"], list) or not raw["sources"]:
        raise ItemSchemaError("source-mismatch: 来源封装缺少 blocks")
    fields = set(SourceModelBlock.__dataclass_fields__)
    blocks = []
    for source in raw["sources"]:
        if not isinstance(source, dict) or set(source) != fields:
            raise ItemSchemaError("source-mismatch: 来源块字段非法")
        blocks.append(SourceModelBlock(**source))
    result = tuple(blocks)
    if render_source_blocks(result) != content:
        raise ItemSchemaError("source-mismatch: 来源封装不是规范编码")
    return result
