"""prefix_epoch 稳定前缀/epoch 合同的 golden vectors 与错误合同测试。

所有预期 hash/字节串均为独立固化的字面量 golden,不由生产代码现算生成。
覆盖 Unicode、空内容、连续 revision、frame serialization、not_tracked、
source conflict、非法 epoch 组合、ToolSet mismatch、stable-prefix
violation 与 provider-profile-change-requires-rebuild。
"""

from __future__ import annotations

import pytest

from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.prefix_epoch import (
    PENDING_TRANSITION_REASONS,
    REBUILD_EPOCH_REASONS,
    AppendedItemRef,
    EpochContractError,
    EpochReason,
    ParentAssemblyRef,
    PendingPrefixEpochTransition,
    PrefixEpoch,
    PrefixEpochState,
    ProviderProfileChangeRequiresRebuildError,
    ProviderProfileItemFrame,
    SourceIdentityConflictError,
    StablePrefixViolationError,
    ToolSetRebaseRequiredError,
    append_items,
    initial_epoch_state,
    open_rebuild_epoch,
    seal_assembly,
    stable_prefix_bytes,
    stable_prefix_hash,
    toolset_compatibility_key,
    verify_stable_prefix,
)
from app.domain.itemized.refs import ToolSetRef

PROFILE = "openai-responses:v1"
PROFILE_NEW = "anthropic-messages:v2"
KEY_ZERO = "sha256:jcs:v1:" + "0" * 64
KEY_A = "sha256:jcs:v1:" + "a" * 64
KEY_B = "sha256:jcs:v1:" + "b" * 64

# V1: Unicode 正文 frame 的独立 golden。
V1_UNICODE_TEXT = "你好，世界 🌍"
V1_UNICODE_FRAME_HASH = (
    "sha256:jcs:v1:68ff95a9ef999a99a70a817c51c5a61be82f6d0f2df97a0873098a56c39aa056"
)
V1_UNICODE_FRAME_BYTES_HEX = (
    "7b22636f6e74656e74223a22e4bda0e5a5bdefbc8ce4b896e7958c20f09f8c8d222c"
    "226f7264696e616c223a312c227061796c6f61645f6b696e64223a227465787422"
    "2c2270726f76696465725f70726f66696c65223a226f70656e61692d726573706f"
    "6e7365733a7631222c22726f6c65223a2275736572227d"
)
V1_UNICODE_FRAME_LEN = 123

# V2: 空内容 frame 的独立 golden。
V2_EMPTY_FRAME_HASH = (
    "sha256:jcs:v1:e556ccec05393df6035b99078fe663e2e3be721f347a6d1069faf424642bfb2b"
)
V2_EMPTY_FRAME_BYTES_HEX = (
    "7b22636f6e74656e74223a22222c226f7264696e616c223a322c227061796c6f61"
    "645f6b696e64223a2274657874222c2270726f76696465725f70726f66696c6522"
    "3a226f70656e61692d726573706f6e7365733a7631222c22726f6c65223a226173"
    "73697374616e74227d"
)
V2_EMPTY_FRAME_LEN = 108

# V3: 空稳定前缀的独立 golden(sha256 of b"")。
V3_EMPTY_PREFIX_HASH = (
    "sha256:bytes:v1:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

# V5: 结构化 content frame 的独立 golden。
V5_FRAME_HASH = (
    "sha256:jcs:v1:52c279baf79052cf1c7868af002d9af012451da6fdbeb86125002cb72f9a0fca"
)
V5_FRAME_BYTES_HEX = (
    "7b22636f6e74656e74223a7b2261223a312c2262223a322c22e4b8ad223a22e6968"
    "7227d2c226f7264696e616c223a392c227061796c6f61645f6b696e64223a227374"
    "72756374757265645f636f6e74656e74222c2270726f76696465725f70726f6669"
    "6c65223a226f70656e61692d726573706f6e7365733a7631222c22726f6c65223a"
    "22746f6f6c227d"
)
V5_FRAME_LEN = 140

# V4: 同一 tracked source 连续 revision 的 frame hash 与累计前缀 golden。
V4_REVISION_BODIES = (
    ("rev-1", "# AGENTS v1\n工程规则"),
    ("rev-2", "# AGENTS v2\n工程规则+安全"),
    ("rev-3", "# AGENTS v3\n工程规则+安全+性能"),
)
V4_FRAME_HASHES = (
    "sha256:jcs:v1:62c33c1ec8a0889e1d6decb037b6fe863aee4a3b271efaff60f39933764342e0",
    "sha256:jcs:v1:db5c0f06c859ca6ddb5499ea9be47f232ea712a583f3b5ba6c775728f69e7354",
    "sha256:jcs:v1:3c305c1bff158c4f1fe16b8c67f72aae3c7875f78771154d931fc55521504a7e",
)
V4_PREFIX_LENGTHS = (128, 263, 405)
V4_PREFIX_HASHES = (
    "sha256:bytes:v1:62c33c1ec8a0889e1d6decb037b6fe863aee4a3b271efaff60f39933764342e0",
    "sha256:bytes:v1:6f68277e1a0077089d582f1f83bfb285534e9a1740e2a77aa9e8131cff84033c",
    "sha256:bytes:v1:575c952661c00d7a84a9484f63e9c7ab13976e33fe56dff709f8a198c7736fe3",
)

# V6: 精确 ToolSetRef/policy compatibility key 的独立 golden。
V6_KEY1 = (
    "sha256:jcs:v1:86a6c0391056ffd2143d5238d17b57eac64d8a948f0593179d7b34b6ac1eb51b"
)
V6_KEY2 = (
    "sha256:jcs:v1:f303a82fe78f31dd0946ed7ca79cfedf15b307f05773e9fad102b4c9509e35e2"
)
V6_KEY3 = (
    "sha256:jcs:v1:889e6886395c2eb782a2cd5845f66799717637c95145c6d12ae5728ef8142d6f"
)

# V7: initial -> append -> seal -> hard rebase 的前缀链 golden。
V7_ASM1_HASH = (
    "sha256:bytes:v1:5c95fb4559e8afd262dc27eb22d8d5700834858d548489d51a5501073695ccc0"
)
V7_ASM2_HASH = (
    "sha256:bytes:v1:340de5e952caec2699142c08e211581e45851d084084efbdef672db19b0f9f3d"
)


def _ref(
    ref_type: str,
    ref_id: str,
    *,
    source_identity: str | None = None,
    source_revision: str | None = None,
    tracking_state: str = "not_tracked",
) -> AppendedItemRef:
    return AppendedItemRef(
        ref_type=ref_type,
        ref_id=ref_id,
        source_identity=source_identity,
        source_revision=source_revision,
        tracking_state=tracking_state,
    )


def _frame(
    ordinal: int,
    role: str,
    content: object,
    item_ref: AppendedItemRef,
    *,
    profile: str = PROFILE,
    payload_kind: str = PayloadKind.TEXT,
) -> ProviderProfileItemFrame:
    return ProviderProfileItemFrame(
        ordinal=ordinal,
        provider_profile=profile,
        role=role,
        payload_kind=payload_kind,
        content=content,
        item_ref=item_ref,
    )


def _skill_load_toolset_ref(snapshot_id: str, plan_id: str, *, with_mode: bool) -> ToolSetRef:
    """构造 golden 兼容 key 使用的 Provider 可见 ToolSet manifest。"""
    skill_load_parameters: dict[str, object] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    if with_mode:
        skill_load_parameters["properties"] = {
            "name": {"type": "string"},
            "mode": {"type": "string", "enum": ["snapshot", "tracked", "untrack"]},
        }
    envelope_parameters = {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["tool_name", "arguments"],
    }
    return ToolSetRef.from_tool_snapshot(
        snapshot_id=snapshot_id,
        session_id="ses_" + "0" * 32,
        plan_id=plan_id,
        tools=(
            {
                "type": "function",
                "function": {"name": "skill_load", "parameters": skill_load_parameters},
            },
            {
                "type": "function",
                "function": {
                    "name": "invoke_extension_tool",
                    "parameters": envelope_parameters,
                },
            },
        ),
        source_revision="toolrev-2" if with_mode else "toolrev-1",
        tool_policy={"confirmation": "always", "visibility": "direct+envelope"},
        tool_policy_version="v1",
    )


def test_unicode_frame_golden_vector() -> None:
    frame = _frame(1, "user", V1_UNICODE_TEXT, _ref("canonical_item", "item-0001"))
    assert frame.frame_hash() == V1_UNICODE_FRAME_HASH
    assert frame.frame_bytes().hex() == V1_UNICODE_FRAME_BYTES_HEX
    assert len(frame.frame_bytes()) == V1_UNICODE_FRAME_LEN
    assert frame.frame_bytes().decode("utf-8").count("你好") == 1


def test_empty_content_golden_vector() -> None:
    frame = _frame(2, "assistant", "", _ref("canonical_item", "item-0002"))
    assert frame.frame_hash() == V2_EMPTY_FRAME_HASH
    assert frame.frame_bytes().hex() == V2_EMPTY_FRAME_BYTES_HEX
    assert len(frame.frame_bytes()) == V2_EMPTY_FRAME_LEN
    assert stable_prefix_hash(b"") == V3_EMPTY_PREFIX_HASH
    assert stable_prefix_bytes(()) == b""
    # 空 frame 与缺省内容不同:空字符串仍是显式正文,hash 不同。
    assert V2_EMPTY_FRAME_HASH != V1_UNICODE_FRAME_HASH


def test_consecutive_revision_golden_chain() -> None:
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_ZERO, turn_id="turn-0001"
    )
    hashes: list[str] = []
    lengths: list[int] = []
    prefix_hashes: list[str] = []
    for index, (revision, body) in enumerate(V4_REVISION_BODIES):
        frame = _frame(
            index + 3,
            "user",
            body,
            _ref(
                "source_item",
                "src-agents-1",
                source_identity="boxteam://resources/agents-md",
                source_revision=revision,
                tracking_state="tracked",
            ),
        )
        state = append_items(
            state,
            [frame],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=KEY_ZERO,
        )
        hashes.append(frame.frame_hash())
        lengths.append(state.prefix_byte_length)
        prefix_hashes.append(state.prefix_hash)
    assert tuple(hashes) == V4_FRAME_HASHES
    assert tuple(lengths) == V4_PREFIX_LENGTHS
    assert tuple(prefix_hashes) == V4_PREFIX_HASHES
    # 同一 source 的 revision 链:前缀随追加单调增长,旧 frame 字节不变。
    assert lengths == sorted(lengths)


def test_frame_serialization_is_order_independent_jcs() -> None:
    content_a = {"b": 2, "a": 1, "中": "文"}
    content_b = {"a": 1, "中": "文", "b": 2}
    frame_a = _frame(
        9,
        "tool",
        content_a,
        _ref("canonical_item", "item-0009"),
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
    )
    frame_b = _frame(
        9,
        "tool",
        content_b,
        _ref("canonical_item", "item-0009"),
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
    )
    assert frame_a.frame_hash() == frame_b.frame_hash()
    assert frame_a.frame_bytes() == frame_b.frame_bytes()
    # 独立 golden:JCS 键排序 + UTF-8 中文原文编码。
    assert frame_a.frame_hash() == V5_FRAME_HASH
    assert frame_a.frame_bytes().hex() == V5_FRAME_BYTES_HEX
    assert len(frame_a.frame_bytes()) == V5_FRAME_LEN


def test_not_tracked_source_repeats_without_conflict() -> None:
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_ZERO
    )
    # snapshot 模式:同一 source identity 的 not_tracked item 携带 revision,
    # 但不参与 tracked revision 冲突校验。
    first = _frame(
        1,
        "user",
        "# SKILL snapshot",
        _ref(
            "source_item",
            "src-skill-1",
            source_identity="boxteam://resources/skills/report",
            source_revision="skill-rev-7",
        ),
    )
    second = _frame(
        2,
        "user",
        "# SKILL snapshot",
        _ref(
            "source_item",
            "src-skill-2",
            source_identity="boxteam://resources/skills/report",
            source_revision="skill-rev-7",
        ),
    )
    state = append_items(
        state,
        [first, second],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=KEY_ZERO,
    )
    assert state.appended_refs[0].tracking_state == "not_tracked"
    # tracked ref 缺 source_revision 直接失败。
    with pytest.raises(ItemSchemaError):
        _ref(
            "source_item",
            "src-skill-3",
            source_identity="boxteam://resources/skills/report",
            tracking_state="tracked",
        )


def test_source_identity_conflict_and_duplicate_body() -> None:
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_ZERO
    )
    first = _frame(
        1,
        "user",
        "正文 A",
        _ref(
            "source_item",
            "src-1",
            source_identity="boxteam://resources/agents-md",
            source_revision="rev-9",
            tracking_state="tracked",
        ),
    )
    state = append_items(
        state, [first], desired_provider_profile=PROFILE, desired_tool_compatibility_key=KEY_ZERO
    )
    # 同 identity/revision、不同正文:source identity conflict。
    conflicting = _frame(
        2,
        "user",
        "正文 B",
        _ref(
            "source_item",
            "src-2",
            source_identity="boxteam://resources/agents-md",
            source_revision="rev-9",
            tracking_state="tracked",
        ),
    )
    with pytest.raises(SourceIdentityConflictError) as conflict_info:
        append_items(
            state,
            [conflicting],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=KEY_ZERO,
        )
    assert conflict_info.value.error_code == "source-identity-conflict"
    # 同 identity/revision、相同正文:同一 revision 仍不得重复选择。
    duplicate = _frame(
        2,
        "user",
        "正文 A",
        _ref(
            "source_item",
            "src-3",
            source_identity="boxteam://resources/agents-md",
            source_revision="rev-9",
            tracking_state="tracked",
        ),
    )
    with pytest.raises(SourceIdentityConflictError):
        append_items(
            state,
            [duplicate],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=KEY_ZERO,
        )


def test_illegal_epoch_combinations_are_rejected() -> None:
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=KEY_A, turn_id="turn-0001"
    )
    # epoch_reason 是闭合集合:第五类 reason(如 provider profile 变化)不存在。
    for illegal in ("initial", "provider_profile_changed", EpochReason.INITIAL):
        with pytest.raises(EpochContractError):
            open_rebuild_epoch(
                state,
                reason=illegal,  # type: ignore[arg-type]
                provider_profile=PROFILE,
                tool_compatibility_key=KEY_A,
            )
    # pending transition 只接受 compaction/rewind。
    for reason in (EpochReason.INITIAL, EpochReason.TOOLSET_CHANGED):
        with pytest.raises(EpochContractError):
            PendingPrefixEpochTransition(transition_id="t-illegal", reason=reason)
    # 非 typed 的裸字符串 reason 不是 EpochReason 成员。
    with pytest.raises(EpochContractError):
        PendingPrefixEpochTransition(transition_id="t-str", reason="rewind")
    # compaction/rewind 必须携带匹配且未消费的 transition。
    rewind_transition = PendingPrefixEpochTransition(
        transition_id="t-rewind", reason=EpochReason.REWIND
    )
    with pytest.raises(EpochContractError):
        open_rebuild_epoch(
            state,
            reason=EpochReason.REWIND,
            provider_profile=PROFILE,
            tool_compatibility_key=KEY_A,
        )
    with pytest.raises(EpochContractError):
        open_rebuild_epoch(
            state,
            reason=EpochReason.REWIND,
            provider_profile=PROFILE,
            tool_compatibility_key=KEY_A,
            transition=PendingPrefixEpochTransition(
                transition_id="t-mismatch", reason=EpochReason.COMPACTION
            ),
        )
    # toolset_changed 在 safe boundary 直接生效:不接受 pending transition。
    other_key = toolset_compatibility_key(
        _skill_load_toolset_ref("ts-x", "plan-x", with_mode=True), policy_hash=KEY_A
    )
    with pytest.raises(EpochContractError):
        open_rebuild_epoch(
            state,
            reason=EpochReason.TOOLSET_CHANGED,
            provider_profile=PROFILE,
            tool_compatibility_key=other_key,
            transition=rewind_transition,
        )
    # no-op ToolSet 变化不得伪造 hard rebase。
    with pytest.raises(EpochContractError):
        open_rebuild_epoch(
            state,
            reason=EpochReason.TOOLSET_CHANGED,
            provider_profile=PROFILE,
            tool_compatibility_key=KEY_A,
        )
    # 重复消费 transition 直接失败。
    consumed = rewind_transition.consume()
    assert consumed.consumed is True
    with pytest.raises(EpochContractError):
        consumed.consume()
    # initial epoch 的 ordinal 恒为 1;parent 必须属于当前 epoch。
    with pytest.raises(EpochContractError):
        PrefixEpoch(ordinal=2, reason=EpochReason.INITIAL)
    with pytest.raises(EpochContractError):
        PrefixEpochState(
            epoch=PrefixEpoch(ordinal=1, reason=EpochReason.INITIAL),
            provider_profile=PROFILE,
            tool_compatibility_key=KEY_A,
            epoch_applied=False,
            parent=ParentAssemblyRef(
                assembly_id="asm-x",
                epoch_ordinal=2,
                prefix_byte_length=0,
                prefix_hash=stable_prefix_hash(b""),
            ),
        )
    # 闭合集合常量即全部合法 reason。
    assert REBUILD_EPOCH_REASONS == frozenset(
        {EpochReason.COMPACTION, EpochReason.REWIND, EpochReason.TOOLSET_CHANGED}
    )
    assert PENDING_TRANSITION_REASONS == frozenset(
        {EpochReason.COMPACTION, EpochReason.REWIND}
    )


def test_toolset_mismatch_requires_rebase() -> None:
    tool_ref_a = _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False)
    tool_ref_b = _skill_load_toolset_ref("ts-0002", "plan-0002", with_mode=True)
    key_a = toolset_compatibility_key(tool_ref_a, policy_hash=KEY_A)
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=key_a
    )
    state = append_items(
        state,
        [_frame(1, "user", "第一轮", _ref("canonical_item", "item-0001"))],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    # desired ToolSet schema 变化:旧 epoch 禁止继续 seal。
    key_b = toolset_compatibility_key(tool_ref_b, policy_hash=KEY_A)
    assert key_b != key_a
    with pytest.raises(ToolSetRebaseRequiredError) as rebase_info:
        append_items(
            state,
            [_frame(2, "user", "续轮", _ref("canonical_item", "item-0002"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_b,
        )
    assert rebase_info.value.error_code == "toolset-hard-rebase-required"
    # 仅 policy hash 变化同样必须 rebase。
    key_policy = toolset_compatibility_key(tool_ref_a, policy_hash=KEY_B)
    with pytest.raises(ToolSetRebaseRequiredError):
        append_items(
            state,
            [_frame(2, "user", "续轮", _ref("canonical_item", "item-0002"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_policy,
        )
    # hard rebase 建立新 epoch 后按新 key 追加。
    rebased = open_rebuild_epoch(
        state,
        reason=EpochReason.TOOLSET_CHANGED,
        provider_profile=PROFILE,
        tool_compatibility_key=key_b,
        turn_id="turn-0001",
    )
    assert rebased.epoch == PrefixEpoch(
        ordinal=2, reason=EpochReason.TOOLSET_CHANGED, turn_id="turn-0001"
    )
    sealed = seal_assembly(
        append_items(
            rebased,
            [_frame(1, "user", "重建正文", _ref("canonical_item", "item-r1"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_b,
        ),
        assembly_id="asm-rebase",
    )
    assert sealed.state.epoch_applied is True
    assert sealed.state.tool_compatibility_key == key_b
    assert sealed.consumed_transition is None


def test_toolset_compatibility_key_golden() -> None:
    tool_ref_a = _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False)
    tool_ref_b = _skill_load_toolset_ref("ts-0002", "plan-0002", with_mode=True)
    assert toolset_compatibility_key(tool_ref_a, policy_hash=KEY_A) == V6_KEY1
    assert toolset_compatibility_key(tool_ref_b, policy_hash=KEY_A) == V6_KEY2
    assert toolset_compatibility_key(tool_ref_a, policy_hash=KEY_B) == V6_KEY3
    # 相同输入确定性;manifest 或 policy 任一变化都改变 key。
    assert toolset_compatibility_key(tool_ref_a, policy_hash=KEY_A) == V6_KEY1
    assert V6_KEY1 != V6_KEY2 != V6_KEY3
    with pytest.raises(ItemSchemaError):
        toolset_compatibility_key(tool_ref_a, policy_hash="sha256:jcs:v1:zz")


def test_provider_profile_change_requires_rebuild() -> None:
    tool_ref = _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False)
    key_a = toolset_compatibility_key(tool_ref, policy_hash=KEY_A)
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=key_a, turn_id="turn-0001"
    )
    state = append_items(
        state,
        [_frame(1, "user", "旧 profile 正文", _ref("canonical_item", "item-0001"))],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    sealed = seal_assembly(state, assembly_id="asm-0001")
    assert sealed.state.provider_profile == PROFILE
    # 已有 assembly 后 desired profile 变化:显式拒绝,不静默沿用。
    with pytest.raises(ProviderProfileChangeRequiresRebuildError) as profile_info:
        append_items(
            sealed.state,
            [_frame(2, "user", "新 profile 正文", _ref("canonical_item", "item-0002"))],
            desired_provider_profile=PROFILE_NEW,
            desired_tool_compatibility_key=key_a,
        )
    assert profile_info.value.error_code == "provider-profile-change-requires-rebuild"
    # 合法边界:rewind pending transition + 首个新 epoch seal 原子应用新 profile。
    transition = PendingPrefixEpochTransition(
        transition_id="t-rewind-1",
        reason=EpochReason.REWIND,
        turn_id="turn-0001",
    )
    rebuilt = open_rebuild_epoch(
        sealed.state,
        reason=EpochReason.REWIND,
        provider_profile=PROFILE_NEW,
        tool_compatibility_key=key_a,
        transition=transition,
        turn_id="turn-0001",
    )
    assert rebuilt.provider_profile == PROFILE_NEW
    assert rebuilt.epoch_applied is False
    rebuilt = append_items(
        rebuilt,
        [_frame(1, "user", "rewind 后正文", _ref("canonical_item", "item-r1"), profile=PROFILE_NEW)],
        desired_provider_profile=PROFILE_NEW,
        desired_tool_compatibility_key=key_a,
    )
    sealed_new = seal_assembly(
        rebuilt, assembly_id="asm-rewind-1", transition=transition
    )
    assert sealed_new.state.epoch_applied is True
    assert sealed_new.state.provider_profile == PROFILE_NEW
    assert sealed_new.state.epoch.reason == EpochReason.REWIND
    assert sealed_new.consumed_transition is not None
    assert sealed_new.consumed_transition.consumed is True
    # 原 transition 对象保持不可变,消费结果只出现在返回副本上。
    assert transition.consumed is False
    # frame 的 provider_profile 必须与当前 epoch 绑定一致。
    with pytest.raises(EpochContractError):
        append_items(
            sealed_new.state,
            [_frame(2, "user", "旧 profile frame", _ref("canonical_item", "item-x"), profile=PROFILE)],
            desired_provider_profile=PROFILE_NEW,
            desired_tool_compatibility_key=key_a,
        )


def test_stable_prefix_violation() -> None:
    frame1 = _frame(1, "user", V1_UNICODE_TEXT, _ref("canonical_item", "item-0001"))
    frame2 = _frame(2, "assistant", "", _ref("canonical_item", "item-0002"))
    frames = (frame1, frame2)
    good_digest = stable_prefix_bytes(frames)
    parent = ParentAssemblyRef(
        assembly_id="asm-0001",
        epoch_ordinal=1,
        prefix_byte_length=len(good_digest),
        prefix_hash=stable_prefix_hash(good_digest),
    )
    assert verify_stable_prefix(parent, frames) == 2
    # hash 不一致。
    wrong_hash_parent = ParentAssemblyRef(
        assembly_id="asm-0001",
        epoch_ordinal=1,
        prefix_byte_length=len(good_digest),
        prefix_hash=stable_prefix_hash(b"tampered"),
    )
    with pytest.raises(StablePrefixViolationError) as hash_info:
        verify_stable_prefix(wrong_hash_parent, frames)
    assert hash_info.value.error_code == "stable-prefix-violation"
    # 长度落在 frame 边界内(拆 frame)。
    split_parent = ParentAssemblyRef(
        assembly_id="asm-0001",
        epoch_ordinal=1,
        prefix_byte_length=V1_UNICODE_FRAME_LEN - 1,
        prefix_hash=stable_prefix_hash(good_digest[: V1_UNICODE_FRAME_LEN - 1]),
    )
    with pytest.raises(StablePrefixViolationError):
        verify_stable_prefix(split_parent, frames)
    # frames 不足以覆盖父前缀。
    short_parent = ParentAssemblyRef(
        assembly_id="asm-0001",
        epoch_ordinal=1,
        prefix_byte_length=len(good_digest) + 1,
        prefix_hash=stable_prefix_hash(good_digest + b"x"),
    )
    with pytest.raises(StablePrefixViolationError):
        verify_stable_prefix(short_parent, frames)
    # seal 时同样强制校验:父前缀被篡改的新 assembly 不能 seal。
    tampered_parent = ParentAssemblyRef(
        assembly_id="asm-parent",
        epoch_ordinal=1,
        prefix_byte_length=10,
        prefix_hash=stable_prefix_hash(b"0123456789"),
    )
    broken_state = PrefixEpochState(
        epoch=PrefixEpoch(ordinal=1, reason=EpochReason.INITIAL),
        provider_profile=PROFILE,
        tool_compatibility_key=KEY_A,
        epoch_applied=False,
        frames=frames,
        parent=tampered_parent,
    )
    with pytest.raises(StablePrefixViolationError):
        seal_assembly(broken_state, assembly_id="asm-child")


def test_pending_transition_only_consumed_after_successful_seal() -> None:
    key_a = toolset_compatibility_key(
        _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False), policy_hash=KEY_A
    )
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=key_a
    )
    state = append_items(
        state,
        [_frame(1, "user", "已提交正文", _ref("canonical_item", "item-0001"))],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    sealed = seal_assembly(state, assembly_id="asm-0001")
    transition = PendingPrefixEpochTransition(
        transition_id="t-compaction-1", reason=EpochReason.COMPACTION
    )
    rebuilt = open_rebuild_epoch(
        sealed.state,
        reason=EpochReason.COMPACTION,
        provider_profile=PROFILE,
        tool_compatibility_key=key_a,
        transition=transition,
    )
    rebuilt = append_items(
        rebuilt,
        [_frame(1, "user", "压缩摘要", _ref("source_item", "src-summary", source_identity="compaction"))],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    # 制造 seal 失败:伪造不一致 parent 触发 stable-prefix violation。
    broken = PrefixEpochState(
        epoch=rebuilt.epoch,
        provider_profile=rebuilt.provider_profile,
        tool_compatibility_key=rebuilt.tool_compatibility_key,
        epoch_applied=False,
        frames=rebuilt.frames,
        parent=ParentAssemblyRef(
            assembly_id="asm-ghost",
            epoch_ordinal=rebuilt.epoch.ordinal,
            prefix_byte_length=5,
            prefix_hash=stable_prefix_hash(b"ghost"),
        ),
    )
    with pytest.raises(StablePrefixViolationError):
        seal_assembly(broken, assembly_id="asm-0002", transition=transition)
    # seal 失败不消费 transition、不应用 epoch。
    assert transition.consumed is False
    # 成功 seal 才消费。
    ok = seal_assembly(rebuilt, assembly_id="asm-0002", transition=transition)
    assert ok.state.epoch_applied is True
    assert ok.consumed_transition is not None
    assert ok.consumed_transition.transition_id == "t-compaction-1"
    assert ok.consumed_transition.consumed is True
    assert ok.appended_refs == (
        AppendedItemRef(
            ref_type="source_item",
            ref_id="src-summary",
            source_identity="compaction",
            source_revision=None,
            tracking_state="not_tracked",
        ),
    )


def test_multi_model_call_epochs_within_same_turn() -> None:
    key_a = toolset_compatibility_key(
        _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False), policy_hash=KEY_A
    )
    key_b = toolset_compatibility_key(
        _skill_load_toolset_ref("ts-0002", "plan-0002", with_mode=True), policy_hash=KEY_A
    )
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=key_a, turn_id="turn-0001"
    )
    state = seal_assembly(
        append_items(
            state,
            [_frame(1, "user", "call-1", _ref("canonical_item", "item-0001"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_a,
        ),
        assembly_id="asm-0001",
    ).state
    # 同一 Turn 内第一次 model-call epoch:hard rebase。
    first_epoch = open_rebuild_epoch(
        state,
        reason=EpochReason.TOOLSET_CHANGED,
        provider_profile=PROFILE,
        tool_compatibility_key=key_b,
        turn_id="turn-0001",
    )
    first_epoch = seal_assembly(
        append_items(
            first_epoch,
            [_frame(1, "user", "call-2", _ref("canonical_item", "item-0002"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_b,
        ),
        assembly_id="asm-0002",
    ).state
    # 同一 Turn 内第二次 model-call epoch:rewind transition。
    transition = PendingPrefixEpochTransition(
        transition_id="t-rewind-2", reason=EpochReason.REWIND, turn_id="turn-0001"
    )
    second_epoch = open_rebuild_epoch(
        first_epoch,
        reason=EpochReason.REWIND,
        provider_profile=PROFILE,
        tool_compatibility_key=key_b,
        transition=transition,
        turn_id="turn-0001",
    )
    second_epoch = seal_assembly(
        append_items(
            second_epoch,
            [_frame(1, "user", "call-3", _ref("canonical_item", "item-0003"))],
            desired_provider_profile=PROFILE,
            desired_tool_compatibility_key=key_b,
        ),
        assembly_id="asm-0003",
        transition=transition,
    ).state
    assert [
        state.epoch.ordinal for state in (state, first_epoch, second_epoch)
    ] == [1, 2, 3]
    assert [state.epoch.reason for state in (state, first_epoch, second_epoch)] == [
        EpochReason.INITIAL,
        EpochReason.TOOLSET_CHANGED,
        EpochReason.REWIND,
    ]
    assert all(epoch.turn_id == "turn-0001" for epoch in (
        state.epoch, first_epoch.epoch, second_epoch.epoch
    ))


def test_append_seal_prefix_chain_golden() -> None:
    """V7:initial seal -> 同 epoch 追加 seal -> hard rebase 新 epoch。"""
    tool_ref_a = _skill_load_toolset_ref("ts-0001", "plan-0001", with_mode=False)
    tool_ref_b = _skill_load_toolset_ref("ts-0002", "plan-0002", with_mode=True)
    key_a = toolset_compatibility_key(tool_ref_a, policy_hash=KEY_A)
    key_b = toolset_compatibility_key(tool_ref_b, policy_hash=KEY_A)
    state = initial_epoch_state(
        provider_profile=PROFILE, tool_compatibility_key=key_a, turn_id="turn-0001"
    )
    unicode_frame = _frame(1, "user", V1_UNICODE_TEXT, _ref("canonical_item", "item-0001"))
    empty_frame = _frame(2, "assistant", "", _ref("canonical_item", "item-0002"))
    state = append_items(
        state,
        [unicode_frame, empty_frame],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    sealed1 = seal_assembly(state, assembly_id="asm-0001")
    assert sealed1.prefix_byte_length == V1_UNICODE_FRAME_LEN + V2_EMPTY_FRAME_LEN
    assert sealed1.prefix_hash == V7_ASM1_HASH
    assert sealed1.parent is None
    assert sealed1.appended_refs == (
        AppendedItemRef(ref_type="canonical_item", ref_id="item-0001"),
        AppendedItemRef(ref_type="canonical_item", ref_id="item-0002"),
    )
    # 同 epoch 第二个 assembly:逐字节继承 asm-0001 前缀。
    state2 = append_items(
        sealed1.state,
        [_frame(3, "user", "续后追加", _ref("canonical_item", "item-0003"))],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_a,
    )
    sealed2 = seal_assembly(state2, assembly_id="asm-0002")
    assert sealed2.parent == ParentAssemblyRef(
        assembly_id="asm-0001",
        epoch_ordinal=1,
        prefix_byte_length=V1_UNICODE_FRAME_LEN + V2_EMPTY_FRAME_LEN,
        prefix_hash=V7_ASM1_HASH,
    )
    assert stable_prefix_bytes(sealed2.state.frames[:2]) == stable_prefix_bytes(state.frames)
    assert sealed2.prefix_hash == V7_ASM2_HASH
    # hard rebase:新 epoch 不继承旧字节前缀。
    state3 = open_rebuild_epoch(
        sealed2.state,
        reason=EpochReason.TOOLSET_CHANGED,
        provider_profile=PROFILE,
        tool_compatibility_key=key_b,
        turn_id="turn-0001",
    )
    state3 = append_items(
        state3,
        [unicode_frame],
        desired_provider_profile=PROFILE,
        desired_tool_compatibility_key=key_b,
    )
    sealed3 = seal_assembly(state3, assembly_id="asm-0003")
    assert sealed3.parent is None
    assert sealed3.state.epoch.ordinal == 2
    assert sealed3.prefix_byte_length == V1_UNICODE_FRAME_LEN
    assert sealed3.state.provider_profile == PROFILE
