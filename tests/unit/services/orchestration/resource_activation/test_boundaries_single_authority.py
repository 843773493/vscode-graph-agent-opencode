"""activation boundary/token 的单一权威防漂移合同。

boundary 集合与 token 正则只在 domain common.py 定义一次；orchestration
contracts.py 必须引用 domain 的公开符号，不得重新定义本地副本。
"""

from __future__ import annotations

from pathlib import Path

from app.domain.itemized.resource_activation import (
    RESOURCE_ACTIVATION_BOUNDARIES,
    RESOURCE_ACTIVATION_TOKEN_PATTERN,
)

_CONTRACTS = Path(
    "app/services/orchestration/resource_activation/contracts.py"
)


def test_domain_exposes_expected_boundaries_and_pattern():
    assert RESOURCE_ACTIVATION_BOUNDARIES == frozenset({"turn", "model_call"})
    assert RESOURCE_ACTIVATION_TOKEN_PATTERN == "^[a-z][a-z0-9_-]{0,63}$"


def test_contracts_does_not_redefine_boundaries_or_token():
    text = _CONTRACTS.read_text(encoding="utf-8")
    # private 名的本地赋值即构成第二份定义。
    assert "_BOUNDARIES: Final" not in text
    assert "_BOUNDARIES =" not in text
    assert "_TOKEN_PATTERN =" not in text
    assert "frozenset({\"turn\", \"model_call\"})" not in text


def test_contracts_consumes_domain_public_symbols():
    text = _CONTRACTS.read_text(encoding="utf-8")
    assert "RESOURCE_ACTIVATION_BOUNDARIES" in text
    assert "RESOURCE_ACTIVATION_TOKEN_PATTERN" in text
