"""从冻结 schema3 registry 提取来源证据；绝不以 selection 声明反向造来源。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.request_plan import (
    ContextContribution,
    resolve_contribution_for_ref,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    SchemaV4UpgradeError,
)


def _session_sources(connection: sqlite3.Connection) -> tuple[ContextContribution, ...]:
    # 此旧表没有正文；显式列序不读取 detail 文件，也不带入旧 assembly binding。
    cursor = connection.execute(
        "SELECT contribution_id,source_kind,source_revision,content_hash,content_length,"
        "redacted_stable_digest,request_only,contribution_kind,visibility,protection,"
        "metadata_json FROM context_contributions ORDER BY contribution_id"
    )
    columns = tuple(column[0] for column in cursor.description)
    result = []
    for row in cursor:
        values = dict(zip(columns, row, strict=True))
        if type(values["request_only"]) is not int or values["request_only"] != 1:
            raise SchemaV4UpgradeError("source-mismatch: schema3 contribution request_only 非法")
        raw = values.pop("metadata_json")
        # 不可信 metadata/parser 异常可能携带凭据；不得附带原值或异常链。
        try:
            metadata = json.loads(raw)
            if not isinstance(metadata, dict) or canonical_json_bytes(metadata).decode() != raw:
                raise ValueError("metadata 不是规范 JCS object")
            contribution = ContextContribution(**{**values, "request_only": True, "metadata": metadata})
        except (ValueError, TypeError):
            invalid = True
        else:
            invalid = False
        if invalid:
            raise SchemaV4UpgradeError("source-mismatch: schema3 contribution manifest 非法")
        result.append(contribution)
    return tuple(result)


def source_contributions(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
) -> tuple[ContextContribution, ...]:
    """调用前必须已逐字段校验 snapshot 与冻结 assembly manifest。

    included 的历史版本由 assembly registry 保留，不能被后续 session source
    更新覆盖；omitted 的现有 mapping 只能从旧 session registry 独立查证。
    """
    candidates = {item.contribution_id: item for item in _session_sources(connection)}
    for item in snapshot.contributions:
        if item.body is not None:
            raise SchemaV4UpgradeError("source-mismatch: schema3 snapshot 含 inline body")
        candidates[item.contribution_id] = replace(item, assembly_id=None, contribution_ordinal=None)
    resolved = {}
    sources = {}
    for ref in snapshot.refs:
        if ref.ref_type != "request_only":
            continue
        contribution = resolve_contribution_for_ref(ref, tuple(candidates.values()))
        resolved[ref.ref_id] = contribution
        if contribution is not None:
            sources[contribution.contribution_id] = contribution
    for entry in snapshot.selection:
        if entry.contribution_id is None:
            continue
        contribution = resolved.get(entry.ref.ref_id) if entry.ref.ref_type == "request_only" else None
        if contribution is None or entry.contribution_id != contribution.contribution_id:
            raise SchemaV4UpgradeError("source-mismatch: selection contribution 缺少真实 schema3 registry 来源")
        for field in ("source_revision", "content_length", "content_hash", "redacted_stable_digest",
                      "visibility", "protection"):
            value = getattr(entry, field)
            if value is not None and value != getattr(contribution, field):
                raise SchemaV4UpgradeError("source-mismatch: selection 与 schema3 contribution 来源不一致")
    return tuple(sources[key] for key in sorted(sources))
