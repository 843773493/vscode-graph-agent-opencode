"""模型可见来源块的唯一确定性编码；不携带 locator，不解释正文标签。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.validation import _non_empty_string

SourceProtocolVersion = Literal[1, 2]
SOURCE_PROTOCOL = "boxteam-context-sources:v2"
SOURCE_PROTOCOLS: dict[SourceProtocolVersion, str] = {
    1: "boxteam-context-sources:v1", 2: SOURCE_PROTOCOL,
}


@dataclass(frozen=True, slots=True)
class SourceVersionRef:
    """具名替代一个已出现的来源版本；不指向用户消息或工具事实。"""

    source_identity: str
    revision: str

    def __post_init__(self) -> None:
        _non_empty_string(self.source_identity, "SourceVersionRef.source_identity")
        _non_empty_string(self.revision, "SourceVersionRef.revision")


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
    replaces: tuple[SourceVersionRef, ...] = ()

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
        if not isinstance(self.replaces, tuple) or not all(isinstance(ref, SourceVersionRef) for ref in self.replaces):
            raise ItemSchemaError("SourceModelBlock.replaces 必须是 typed tuple")
        if len(set(self.replaces)) != len(self.replaces):
            raise ItemSchemaError("SourceModelBlock.replaces 不得重复")
        if SourceVersionRef(self.source_identity, self.revision) in self.replaces:
            raise ItemSchemaError("来源版本不能替代自身")
        canonical_json_bytes(self.body)


class SourceReplacementIndex:
    """只从有序封存来源块派生替代目标，不读取当前目录或可变控制状态。"""

    def __init__(self) -> None:
        self._versions: dict[SourceVersionRef, tuple[str, str]] = {}
        self._skills: dict[str, SourceVersionRef] = {}

    def replacements_for(self, block: SourceModelBlock) -> tuple[SourceVersionRef, ...]:
        refs = []
        if block.base_revision is not None and block.base_revision != block.revision:
            refs.append(SourceVersionRef(block.source_identity, block.base_revision))
        if block.source_kind == "skill" and block.operation != "update":
            current = self._skills.get(block.name.strip())
            if current is not None and current != SourceVersionRef(block.source_identity, block.revision) and current not in refs:
                refs.append(current)
        return tuple(refs)

    def record(self, block: SourceModelBlock) -> None:
        """替代范围只允许同一来源版本，或显式切换的同名 Skill 来源。"""
        for ref in block.replaces:
            if ref.source_identity == block.source_identity:
                if block.base_revision is not None and ref.revision != block.base_revision:
                    raise ItemSchemaError("source-mismatch: 替代版本与来源基准不一致")
                continue
            if block.source_kind != "skill" or block.operation != "replace":
                raise ItemSchemaError("source-mismatch: 跨来源替代只允许显式 Skill 切换")
            previous = self._versions.get(ref)
            if previous is not None and previous != ("skill", block.name.strip()):
                raise ItemSchemaError("source-mismatch: 不得替代其它名称或类型的来源")
            # 新 epoch 可以携带旧封存帧；压缩已移除的版本仍是历史替代范围，
            # 不要求把其正文重新加入 active context。
        self._versions[SourceVersionRef(block.source_identity, block.revision)] = (
            block.source_kind, block.name.strip(),
        )
        if block.source_kind == "skill":
            self._skills[block.name.strip()] = SourceVersionRef(block.source_identity, block.revision)


def render_source_blocks(
    blocks: Sequence[SourceModelBlock], *, version: SourceProtocolVersion = 2,
) -> str:
    """JSON 字符串转义隔离原正文，固定 JCS 编码供 root、tail 和重放共用。"""
    if not blocks or not all(isinstance(block, SourceModelBlock) for block in blocks):
        raise ItemSchemaError("来源封装需要非空 typed blocks")
    if type(version) is not int or version not in SOURCE_PROTOCOLS:
        raise ItemSchemaError("来源协议版本非法")
    sources = []
    for block in blocks:
        if version == 2 and block.base_revision is not None and block.base_revision != block.revision and (
            SourceVersionRef(block.source_identity, block.base_revision) not in block.replaces
        ):
            raise ItemSchemaError("来源替代引用缺少具名基准版本")
        source = asdict(block)
        if version == 1:
            if block.replaces:
                raise ItemSchemaError("v1 来源协议不能编码替代引用")
            source.pop("replaces")
        sources.append(source)
    return canonical_json_bytes({
        "protocol": SOURCE_PROTOCOLS[version], "sources": sources,
    }).decode("utf-8")


def parse_source_blocks(
    content: object, *, expected_version: SourceProtocolVersion | None = None,
) -> tuple[SourceModelBlock, ...]:
    """只解析已显式登记版本的封存帧，不从原正文猜测协议。"""
    if not isinstance(content, str):
        raise ItemSchemaError("source-mismatch: 来源封装必须是文本")
    raw = json.loads(content)
    if not isinstance(raw, dict) or set(raw) != {"protocol", "sources"}:
        raise ItemSchemaError("source-mismatch: 来源协议版本或字段非法")
    version = cast(SourceProtocolVersion | None, next(
        (key for key, name in SOURCE_PROTOCOLS.items() if name == raw["protocol"]), None,
    ))
    if version is None or expected_version is not None and version != expected_version:
        raise ItemSchemaError("source-mismatch: 来源协议版本或字段非法")
    if not isinstance(raw["sources"], list) or not raw["sources"]:
        raise ItemSchemaError("source-mismatch: 来源封装缺少 blocks")
    fields = set(SourceModelBlock.__dataclass_fields__)
    if version == 1:
        fields.remove("replaces")
    blocks = []
    for source in raw["sources"]:
        if not isinstance(source, dict) or set(source) != fields:
            raise ItemSchemaError("source-mismatch: 来源块字段非法")
        if version == 2:
            refs = source["replaces"]
            if not isinstance(refs, list) or any(
                not isinstance(ref, dict) or set(ref) != {"source_identity", "revision"} for ref in refs
            ):
                raise ItemSchemaError("source-mismatch: 替代引用字段非法")
            source = {**source, "replaces": tuple(SourceVersionRef(**ref) for ref in refs)}
        blocks.append(SourceModelBlock(**source))
    result = tuple(blocks)
    if render_source_blocks(result, version=version) != content:
        raise ItemSchemaError("source-mismatch: 来源封装不是规范编码")
    return result
