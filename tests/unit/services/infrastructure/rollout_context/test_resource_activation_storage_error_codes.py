"""activation storage 错误码的单点化防漂移合同。

同一 code 只允许在 storage/resource_activation_common.py 定义一次；store/reads/
migration/lineage 必须引用常量，不得重新裸写同一字面量。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.infrastructure.rollout_context.storage import (
    resource_activation_common,
)

_STORAGE_DIR = Path("app/services/infrastructure/rollout_context/storage")
# 只扫描 storage 侧的 code 归属模块；checkpoint 目录里的同名字符串是异常消息
# 文本（无 .code 语义），不属于本闭合集。
_SCANNED_MODULES = (
    "resource_activation_common.py",
    "resource_activation_store.py",
    "resource_activation_reads.py",
    "resource_activation_migration.py",
    "resource_activation_lineage.py",
)
_CODE_LITERAL = re.compile(
    r'"(resource-activation-(?:schema-invalid|schema-conflict|snapshot-conflict|'
    r'hash-mismatch|lineage-invalid|lineage-unavailable|schema-unavailable))"'
)

_EXPECTED_CODES = {
    "SCHEMA_INVALID_CODE": "resource-activation-schema-invalid",
    "SCHEMA_CONFLICT_CODE": "resource-activation-schema-conflict",
    "SNAPSHOT_CONFLICT_CODE": "resource-activation-snapshot-conflict",
    "HASH_MISMATCH_CODE": "resource-activation-hash-mismatch",
    "LINEAGE_INVALID_CODE": "resource-activation-lineage-invalid",
    "LINEAGE_BODY_UNAVAILABLE_CODE": "resource-activation-lineage-unavailable",
    "SCHEMA_UNAVAILABLE_CODE": "resource-activation-schema-unavailable",
}


def test_declared_codes_are_stable():
    actual = {
        name: getattr(resource_activation_common, name) for name in _EXPECTED_CODES
    }
    assert actual == _EXPECTED_CODES


def test_each_code_has_exactly_one_definition_point():
    counts: dict[str, int] = {}
    for module in _SCANNED_MODULES:
        text = (_STORAGE_DIR / module).read_text(encoding="utf-8")
        for code in _CODE_LITERAL.findall(text):
            counts[code] = counts.get(code, 0) + 1
    # 每个 code 的唯一字面量必须是 common.py 里的常量定义行。
    assert counts == {code: 1 for code in _EXPECTED_CODES.values()}


def test_no_inline_code_literals_outside_common():
    inline: list[tuple[str, str]] = []
    for module in _SCANNED_MODULES:
        if module == "resource_activation_common.py":
            continue
        text = (_STORAGE_DIR / module).read_text(encoding="utf-8")
        inline.extend((module, code) for code in _CODE_LITERAL.findall(text))
    assert inline == []
