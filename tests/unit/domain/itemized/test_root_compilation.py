"""E1 纵向切片:root compilation 与 user-role projection 的 typed 合同测试。

覆盖 root 资格只由 owner 显式声明、tail_only 永不进入 system root、
首次组装/新 epoch 编译边界、同 epoch 唯一 root 与 wire role 决策。
"""

from __future__ import annotations

import pytest

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.prefix_epoch import (
    EpochReason,
    PendingPrefixEpochTransition,
    PrefixEpoch,
    PrefixEpochState,
    initial_epoch_state,
    open_rebuild_epoch,
)
from app.domain.itemized.root_compilation import (
    RootCandidateSource,
    RootCompileBoundaryError,
    RootCompileConflictError,
    RootPlacementViolationError,
    compile_root_item,
    resolve_source_wire_role,
    validate_epoch_root_uniqueness,
)

PROFILE = "test-profile"
KEY_A = "sha256:jcs:v1:" + "a" * 64
KEY_B = "sha256:jcs:v1:" + "b" * 64
REV_1 = "sha256:jcs:v1:" + "1" * 64
REV_2 = "sha256:jcs:v1:" + "2" * 64
REV_3 = "sha256:jcs:v1:" + "3" * 64


def _candidate(
    source_id: str,
    ordinal: int,
    *,
    content: str,
    revision: str,
    placement: object = "root_eligible",
    reason: object = "initial",
) -> RootCandidateSource:
    return RootCandidateSource(
        source_identity=source_id,
        source_kind="instruction",
        name=source_id,
        revision=revision,
        content=content,
        source_ordinal=ordinal,
        root_placement=placement,
        included_reason=reason,
    )


def test_compile_initial_epoch_builds_single_system_root() -> None:
    """首次组装把 root_eligible 初始贡献按 ordinal 确定序编译为唯一 system root。"""
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A
    )
    item = compile_root_item(
        state,
        [
            _candidate("agents", 1, content="AGENTS 正文", revision=REV_1),
            _candidate("identity", 0, content="identity 正文", revision=REV_2),
        ],
    )
    assert item is not None
    assert item.role == "system"
    assert item.epoch.token == state.epoch.token
    assert item.provider_profile == PROFILE
    assert item.tool_compatibility_key == KEY_A
    assert item.content == "identity 正文\n\nAGENTS 正文"
    assert [p.source_ordinal for p in item.sources] == [0, 1]
    assert [p.source_identity for p in item.sources] == ["identity", "agents"]
    assert item.sources[0].revision == REV_2
    assert item.sources[0].included_reason == "initial"
    assert item.sources[0].content_hash == contribution_content_hash(
        "root_source", "identity 正文"
    )
    manifest = item.frame_manifest()
    assert manifest["role"] == "system"
    assert manifest["epoch"] == state.epoch.token
    # 同一输入逐字节确定性;来源变化会得到不同 frame hash。
    again = compile_root_item(
        state,
        [
            _candidate("agents", 1, content="AGENTS 正文", revision=REV_1),
            _candidate("identity", 0, content="identity 正文", revision=REV_2),
        ],
    )
    assert again is not None and again.frame_hash() == item.frame_hash()
    changed = compile_root_item(
        state,
        [
            _candidate("agents", 1, content="AGENTS 正文 v2", revision=REV_1),
            _candidate("identity", 0, content="identity 正文", revision=REV_2),
        ],
    )
    assert changed is not None and changed.frame_hash() != item.frame_hash()


def test_compile_rejects_tail_only_candidate() -> None:
    """tail_only 候选进入 root 编译即失败,保证其永不进入 system root。"""
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A
    )
    with pytest.raises(RootPlacementViolationError):
        compile_root_item(
            state,
            [
                _candidate("agents", 0, content="A", revision=REV_1),
                _candidate(
                    "mcp-guidance",
                    1,
                    content="指引",
                    revision=REV_2,
                    placement="tail_only",
                ),
            ],
        )


def test_compile_requires_unapplied_epoch_and_rebuild_boundary() -> None:
    """epoch 已 applied 后同 epoch 只能尾部追加;hard rebase 新 epoch 可重编译。"""
    applied = PrefixEpochState(
        epoch=PrefixEpoch(ordinal=1, reason=EpochReason.INITIAL),
        provider_profile=PROFILE,
        tool_compatibility_key=KEY_A,
        epoch_applied=True,
    )
    with pytest.raises(RootCompileBoundaryError):
        compile_root_item(
            applied,
            [_candidate("agents", 0, content="A", revision=REV_1)],
        )
    rebuilt = open_rebuild_epoch(
        applied,
        reason=EpochReason.TOOLSET_CHANGED,
        provider_profile=PROFILE,
        tool_compatibility_key=KEY_B,
    )
    item = compile_root_item(
        rebuilt,
        [
            _candidate(
                "todo-rules",
                0,
                content="Todo 规则",
                revision=REV_3,
                reason="toolset_policy",
            ),
        ],
    )
    assert item is not None
    assert item.epoch.reason == EpochReason.TOOLSET_CHANGED
    assert item.tool_compatibility_key == KEY_B
    assert item.sources[0].included_reason == "toolset_policy"


def test_compile_empty_candidates_returns_none() -> None:
    """root context 至多一个,可以为零;不伪造空 system item。"""
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A
    )
    assert compile_root_item(state, []) is None


def test_compile_conflicts_fail_closed() -> None:
    """重复 identity/ordinal 与非法 included reason 直接失败。"""
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A
    )
    with pytest.raises(RootCompileConflictError):
        compile_root_item(
            state,
            [
                _candidate("agents", 0, content="A", revision=REV_1),
                _candidate("agents", 1, content="A2", revision=REV_2),
            ],
        )
    with pytest.raises(RootCompileConflictError):
        compile_root_item(
            state,
            [
                _candidate("agents", 0, content="A", revision=REV_1),
                _candidate("identity", 0, content="B", revision=REV_2),
            ],
        )
    with pytest.raises(ItemSchemaError):
        compile_root_item(
            state,
            [
                _candidate(
                    "agents",
                    0,
                    content="A",
                    revision=REV_1,
                    reason="unknown",
                ),
            ],
        )


def test_candidate_declared_fields_fail_closed() -> None:
    """资格/内容/revision/ordinal 声明非法时直接 schema error。"""
    with pytest.raises(ItemSchemaError):
        _candidate("agents", 0, content="", revision=REV_1)
    with pytest.raises(ItemSchemaError):
        _candidate("agents", 0, content="A", revision="rev-1")
    with pytest.raises(ItemSchemaError):
        _candidate("agents", -1, content="A", revision=REV_1)
    with pytest.raises(RootPlacementViolationError):
        _candidate("agents", 0, content="A", revision=REV_1, placement="root")


def test_wire_role_decision_by_position() -> None:
    """user-role projection:tail_only 恒为独立 user;root_eligible 仅入 root 才 system。"""
    assert resolve_source_wire_role("tail_only", compiled_into_root=False) == "user"
    assert (
        resolve_source_wire_role("root_eligible", compiled_into_root=True) == "system"
    )
    assert (
        resolve_source_wire_role("root_eligible", compiled_into_root=False) == "user"
    )
    with pytest.raises(RootPlacementViolationError):
        resolve_source_wire_role("tail_only", compiled_into_root=True)
    with pytest.raises(RootPlacementViolationError):
        resolve_source_wire_role("root", compiled_into_root=False)  # type: ignore[arg-type]


def test_post_user_source_recovery_is_user_role() -> None:
    """第一条真实用户消息之后的 full/delta/恢复一律 compiled_into_root=False。"""
    # rewind/compaction 未实际重建时,root_eligible 完整恢复也只尾部追加 user item。
    assert resolve_source_wire_role("root_eligible", compiled_into_root=False) == "user"
    assert resolve_source_wire_role("tail_only", compiled_into_root=False) == "user"


def test_epoch_root_uniqueness() -> None:
    """同一 prefix epoch 至多投影一个 root system item。"""
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A
    )
    first = compile_root_item(
        state, [_candidate("agents", 0, content="A", revision=REV_1)]
    )
    assert first is not None
    validate_epoch_root_uniqueness(None, first)
    second = compile_root_item(
        state, [_candidate("identity", 1, content="B", revision=REV_2)]
    )
    assert second is not None
    with pytest.raises(RootCompileConflictError):
        validate_epoch_root_uniqueness(first, second)
    rebuilt = open_rebuild_epoch(
        PrefixEpochState(
            epoch=PrefixEpoch(ordinal=1, reason=EpochReason.INITIAL),
            provider_profile=PROFILE,
            tool_compatibility_key=KEY_A,
            epoch_applied=True,
        ),
        reason=EpochReason.REWIND,
        provider_profile=PROFILE,
        tool_compatibility_key=KEY_B,
        transition=PendingPrefixEpochTransition(
            transition_id="transition-1", reason=EpochReason.REWIND
        ),
    )
    third = compile_root_item(
        rebuilt, [_candidate("agents", 0, content="A", revision=REV_1)]
    )
    assert third is not None
    validate_epoch_root_uniqueness(first, third)
