"""resource_activation 错误码单一权威的防漂移合同。

`RESOURCE_ACTIVATION_ERROR_CODES` 是 domain 域内 snapshot/provenance 合同的唯一
闭集；snapshot.py/provenance.py 的 raise 点必须引用同一批符号，不得各自裸写。
本文件从源码现场派生实际字面量，避免手抄清单成为第二权威。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.domain.itemized.resource_activation import (
    RESOURCE_ACTIVATION_ERROR_CODES,
    ResourceActivationContractError,
)

# 固定的观测应然集合（独立于源码，供变异自检使用，非闭集的第二权威）。
# 一旦有人把闭集缩回旧版 11 个码或漏掉任一枚举，下面的用例会立刻变红。
_EXPECTED_RESOURCE_ACTIVATION_ERROR_CODES = frozenset(
    {
        "resource-activation-schema-invalid",
        "resource-activation-boundary-singularity-rejected",
        "resource-activation-provider-locator-rejected",
        "resource-activation-credential-rejected",
        "resource-activation-absolute-path-rejected",
        "resource-activation-legacy-field-rejected",
        "resource-activation-field-alias-rejected",
        "resource-activation-lineage-invalid",
        "resource-activation-parent-invalid",
        "resource-activation-ordinal-conflict",
        "resource-activation-hash-mismatch",
    }
)

_RESOURCE_ACTIVATION_PACKAGE_DIR = Path("app/domain/itemized/resource_activation")
# 只匹配完整的 code 字面量；schema token 形如 "resource-activation-provenance:v1"
# 引号不紧跟词尾，因此不会被本正则捕获。
_CODE_LITERAL = re.compile(r'"(resource-activation-[a-z0-9-]+)"')


def _declared_resource_activation_error_codes_from_source() -> set[str]:
    """从 resource_activation 包源码派生被 raise/校验的错误码字面量。"""

    declared_error_codes: set[str] = set()
    for path in sorted(_RESOURCE_ACTIVATION_PACKAGE_DIR.glob("*.py")):
        declared_error_codes.update(
            _CODE_LITERAL.findall(path.read_text(encoding="utf-8"))
        )
    return declared_error_codes


def test_resource_activation_error_allowlist_matches_declared_literals() -> None:
    """闭集必须与源码现存的 code 字面量严格相等（不许多、不许少）。"""

    declared_error_codes = _declared_resource_activation_error_codes_from_source()
    assert declared_error_codes == set(RESOURCE_ACTIVATION_ERROR_CODES)


def test_resource_activation_error_allowlist_cannot_shrink() -> None:
    """变异自检：闭集缩回旧 11 个码或漏枚举时立即失败。"""

    assert RESOURCE_ACTIVATION_ERROR_CODES == _EXPECTED_RESOURCE_ACTIVATION_ERROR_CODES


def test_contract_error_rejects_codes_outside_resource_activation_allowlist() -> None:
    """闭集是 raise 的唯一闸门：域外 code 必须被显式拒绝。"""

    with pytest.raises(ValueError):
        ResourceActivationContractError("resource-activation-not-a-real-code", "x")
