"""McpToolGuidanceSourcePort 最小合同测试（OpenSpec E4 provenance port）。

覆盖：tail_only 固定声明、字段闭合校验、sha256 provenance 形式、
frozen 不可变、typed port 结构化实现提交合同（记录式替身）。
"""

from __future__ import annotations

import pytest

from app.services.infrastructure.mcp.guidance_source_port import (
    MCP_GUIDANCE_SOURCE_ID,
    McpToolGuidanceSourcePort,
    McpToolGuidanceSourceRegistration,
)


def _registration(**overrides: object) -> McpToolGuidanceSourceRegistration:
    values: dict[str, object] = {
        "source_id": MCP_GUIDANCE_SOURCE_ID,
        "activation_snapshot_id": "mcp-activation:s:thr:turn",
        "catalog_revision": "sha256:" + "0" * 64,
        "guidance_revision": "sha256:" + "1" * 64,
        "provenance_hash": "sha256:" + "2" * 64,
    }
    values.update(overrides)
    return McpToolGuidanceSourceRegistration(**values)


def test_registration_declares_tail_only_by_default() -> None:
    assert _registration().root_placement == "tail_only"


def test_registration_rejects_root_placement_promotion() -> None:
    with pytest.raises(ValueError, match="tail_only"):
        _registration(root_placement="root_eligible")


def test_registration_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="source_id"):
        _registration(source_id="  ")


def test_registration_rejects_non_sha256_provenance() -> None:
    with pytest.raises(ValueError, match="sha256"):
        _registration(provenance_hash="md5:deadbeef")


def test_registration_is_frozen() -> None:
    registration = _registration()
    with pytest.raises(AttributeError):
        registration.catalog_revision = "sha256:" + "9" * 64  # type: ignore[misc]


def test_source_port_contract_records_registration() -> None:
    recorded: list[McpToolGuidanceSourceRegistration] = []

    class _RecordingOwner:
        def register_tail_only_guidance(
            self, registration: McpToolGuidanceSourceRegistration
        ) -> None:
            recorded.append(registration)

    owner: McpToolGuidanceSourcePort = _RecordingOwner()
    registration = _registration()
    owner.register_tail_only_guidance(registration)
    assert recorded == [registration]
