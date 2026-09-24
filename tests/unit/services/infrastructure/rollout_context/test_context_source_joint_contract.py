"""A4 联合合同：CSM / ContextStore owner / itemized registry / typed prefix。

OpenSpec add-context-injection-lifecycle 1.6：extensions/metadata 中的同名
控制 key 不改变 tracking、base/delta、selection、role、stable prefix；
缺失 typed identity/revision/source binding/source ordinal 显式报错；
registry 重启后 ordinal 稳定、replace 保留同 slot、不同 owner thread 隔离；
watch 乱序/重连、rewind/compaction、fork 恢复只消费已封存 typed 字段。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    contribution_content_hash,
)
from app.domain.itemized.plan_hash import context_plan_hash
from app.domain.itemized.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    PendingPrefixEpochTransition,
    ProviderProfileItemFrame,
    initial_epoch_state,
    open_rebuild_epoch,
    seal_assembly,
    stable_prefix_bytes,
    stable_prefix_hash,
)
from app.domain.itemized.request_plan import (
    ContextContribution,
    resolve_contribution_for_ref,
    validate_included_overlay_chain,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    SourceObservation,
    build_source_lifecycle_decision,
)
from app.services.infrastructure.rollout_context.runtime.ledger import (
    RuntimeContextLedger,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.unit.services.infrastructure.test_context_source_reactor import (
    _write_skill,
)

MAIN_SESSION_ID = "ses_1cb2d44643ae45818a69dc2c654c06c7"
CHILD_SESSION_ID = "ses_29399ea68ac24d0d8dfbb63d746c985e"
ALT_SESSION_ID = "ses_47a1c2e9b0d34f5a8c6e7d2b1f0a9e83"


TOOL_KEY_TOKEN = "sha256:jcs:v1:" + "a" * 64


ADVERSARIAL_CONTROL_KEYS = {
    "source_ordinal": 999,
    "replaceable_source": True,
    "selection_only": True,
    "tracking_state": "untracked",
    "tracking_status": "untracked",
    "role": "delta",
    "base_delta_role": "delta",
}
"""metadata/extensions 中与旧控制 flag 同名的对抗 key 全集。"""


def _install_demo_snapshot(manager: ContextSourceManager, content: str) -> None:
    """按正式 typed API 安装与 registered source 一致的冻结 binding。"""
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={
                "demo": SkillCatalogBinding(
                    name="demo",
                    resource_id="skill-entry:test:demo:activation",
                    entry_identity="skill-entry:test:demo",
                    display_uri="boxteam://workspace/skill/demo",
                    activation_revision=_revision(content),
                    body=content,
                )
            },
        )
    )


def _create_session_node(root: Path, session_id: str, parent_node_id: str | None = None):
    seed_catalog_session_bundle(
        root,
        session_id,
        title="联合合同测试",
        parent_node_id=parent_node_id,
    )


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    _create_session_node(root, MAIN_SESSION_ID)
    _create_session_node(root, CHILD_SESSION_ID, parent_node_id=MAIN_SESSION_ID)
    _create_session_node(root, ALT_SESSION_ID)
    return root


@pytest.fixture
def saver(sessions_root: Path):
    """唯一 ContextStore owner；每次注入新实例等价一次进程重启。"""
    return RolloutCheckpointSaver(sessions_root)


def _restarted(sessions_root: Path) -> RolloutCheckpointSaver:
    """模拟进程重启：全新 Saver/Storage 实例，状态只来自 SQLite。"""
    return RolloutCheckpointSaver(sessions_root)


def _contribution(
    contribution_id: str,
    *,
    body: str = "source body",
    metadata: dict[str, object] | None = None,
    source_ordinal: int | None = None,
    contribution_kind: str = "prompt",
    source_kind: str = "workspace_instructions",
    replaceable_source: bool = False,
) -> ContextContribution:
    return ContextContribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_revision="rev-" + contribution_id,
        content_hash=contribution_content_hash(contribution_kind, body),
        request_only=True,
        metadata=metadata or {},
        contribution_kind=contribution_kind,
        body=body,
        source_ordinal=source_ordinal,
        replaceable_source=replaceable_source,
    )


def _register(
    saver: RolloutCheckpointSaver,
    session_id: str,
    contribution: ContextContribution,
) -> int:
    """经唯一 owner 注册；返回 registry 分配的 typed source_ordinal。"""
    saver.register_context_contribution(session_id, contribution)
    for row in saver._storage.list_context_contributions(session_id):
        if row["contribution_id"] == contribution.contribution_id:
            ordinal = row["source_ordinal"]
            assert isinstance(ordinal, int) and not isinstance(ordinal, bool)
            return ordinal
    raise AssertionError("registry 行缺失")


def _rows(saver: RolloutCheckpointSaver, session_id: str) -> list[dict]:
    return list(saver._storage.list_context_contributions(session_id))


# ---------------------------------------------------------------- 对抗 key


class TestAdversarialControlKeys:
    def test_same_name_keys_do_not_change_plan_hash_or_registry_slot(
        self, saver: RolloutCheckpointSaver, sessions_root: Path,
    ) -> None:
        clean = _contribution("contribution-clean", metadata={"source_ref": "c"})
        adversarial = _contribution(
            "contribution-clean",
            metadata={
                "source_ref": "c",
                **ADVERSARIAL_CONTROL_KEYS,
            },
        )
        ordinal_clean = _register(saver, MAIN_SESSION_ID, clean)
        assert ordinal_clean == 0
        plan_clean = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-1",
        )

        # 同一 session 内只换 metadata：SQL 清理后经全新 owner（重启等价）
        # 重注册对抗孪生，typed 事实逐字段相同。
        with saver._storage._connect(MAIN_SESSION_ID, "") as probe:
            database_path = probe.execute("PRAGMA database_list").fetchone()[2]
        tamper = sqlite3.connect(database_path)
        try:
            with tamper:
                tamper.execute("DELETE FROM context_contributions")
        finally:
            tamper.close()
        restarted = _restarted(sessions_root)
        ordinal_adversarial = _register(restarted, MAIN_SESSION_ID, adversarial)
        # SQL 清理后重注册：registry slot 仍从 0 起且与清理前一致。
        assert ordinal_adversarial == ordinal_clean
        plan_adversarial = restarted.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-1",
        )
        # metadata 是开放 provenance，不进入 plan hash preimage；typed 事实
        # 相同则 hash、selection、ordinal binding 全部一致。
        assert context_plan_hash(plan_clean) == context_plan_hash(plan_adversarial)
        assert [
            (entry.ref.ref_id, entry.contribution_id, entry.contribution_ordinal)
            for entry in plan_clean.selection
        ] == [
            (entry.ref.ref_id, entry.contribution_id, entry.contribution_ordinal)
            for entry in plan_adversarial.selection
        ]

    def test_selection_only_flag_has_no_role_power(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # typed contribution_kind 说它是 full selection：metadata 的
        # selection_only 键不能把它降级为 manifest backing。
        full_with_flag = _contribution(
            "full-with-flag",
            metadata={"selection_only": True},
        )
        _register(saver, MAIN_SESSION_ID, full_with_flag)
        # typed contribution_kind 是 overlay backing：没有 selection_only
        # 键也必须跳过，不能凭缺 key 变成第二个 request-only ref。
        overlay_without_flag = _contribution(
            "overlay:ov-1:base",
            contribution_kind="overlay_base",
            metadata={"overlay_ref": "ov-1"},
        )
        _register(saver, MAIN_SESSION_ID, overlay_without_flag)

        plan = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-2",
        )
        ref_ids = {ref.ref_id for ref in plan.refs if ref.ref_type == "request_only"}
        assert "full-with-flag" in ref_ids
        assert "overlay:ov-1:base" not in ref_ids

    def test_observation_extensions_are_inert_for_decisions(self) -> None:
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID, thread_id=MAIN_THREAD_ID,
        )
        def observation(**extensions: object) -> SourceObservation:
            return SourceObservation(
                owner=owner,
                source_id="skill:demo",
                source_kind="skill",
                name="demo",
                revision="rev-7",
                tracking_mode="tracked",
                from_revision="rev-6",
                content="正文",
                **extensions,
            )

        clean = observation()
        adversarial = observation(
            extensions={
                "boxteam.control/v1": dict(ADVERSARIAL_CONTROL_KEYS),
            },
        )
        assert clean.observation_id == adversarial.observation_id
        clean_delta = build_source_lifecycle_decision(clean, decision_kind="delta")
        adversarial_delta = build_source_lifecycle_decision(
            adversarial, decision_kind="delta",
        )
        assert clean_delta == adversarial_delta

    def test_stable_prefix_inputs_are_typed_only(self) -> None:
        """stable prefix 的输入只有 typed 字段：metadata/_extensions 同名
        key 不改变 frame 字节与前缀 hash（A2 合同的联合锚定）。"""
        def frame(item_ref: AppendedItemRef) -> ProviderProfileItemFrame:
            return ProviderProfileItemFrame(
                ordinal=1,
                provider_profile="openai-responses:v1",
                role="user",
                payload_kind="text",
                content="body",
                item_ref=item_ref,
            )

        # frame/ref 的 typed 合同结构上不携带 metadata/extensions：对抗 key
        # 没有进入 stable prefix 的任何输入通道。
        import dataclasses

        for typed_type in (ProviderProfileItemFrame, AppendedItemRef):
            field_names = {f.name for f in dataclasses.fields(typed_type)}
            assert not field_names & {"metadata", "extensions", "source_ordinal"}

        clean_ref = AppendedItemRef(
            ref_type="source_item",
            ref_id="ref-1",
            source_identity="skill:demo",
            source_revision="rev-1",
            tracking_state="tracked",
        )
        prefix_clean = stable_prefix_hash(
            stable_prefix_bytes((frame(clean_ref),)),
        )
        # 同一 typed 事实重复构建：prefix hash 确定性稳定。
        prefix_repeat = stable_prefix_hash(
            stable_prefix_bytes((frame(clean_ref),)),
        )
        assert prefix_clean == prefix_repeat


# ---------------------------------------------------------------- 缺 typed 报错


class TestMissingTypedFieldsFailClosed:
    def test_ledger_rejects_contribution_without_registry_ordinal(self) -> None:
        ledger = RuntimeContextLedger()
        with pytest.raises(ValueError, match="typed source_ordinal"):
            ledger.add_contribution(_contribution("no-ordinal", source_ordinal=None))

    def test_restore_rejects_registry_row_with_null_ordinal(
        self, saver: RolloutCheckpointSaver, sessions_root: Path,
    ) -> None:
        _register(saver, MAIN_SESSION_ID, _contribution("null-ordinal"))
        with saver._storage._connect(MAIN_SESSION_ID, "") as probe:
            database_path = probe.execute("PRAGMA database_list").fetchone()[2]
        tamper = sqlite3.connect(database_path)
        try:
            with tamper:
                tamper.execute("PRAGMA ignore_check_constraints = ON")
                tamper.execute(
                    "UPDATE context_contributions SET source_ordinal = NULL",
                )
        finally:
            tamper.close()
        restarted = _restarted(sessions_root)
        with pytest.raises((ValueError, RuntimeError), match="source_ordinal"):
            restarted.compose_committed_context_plan(
                MAIN_SESSION_ID, plan_id="plan-3",
            )

    def test_contribution_requires_identity_and_revision(self) -> None:
        with pytest.raises(ItemSchemaError):
            _contribution("", source_ordinal=0)
        with pytest.raises(ItemSchemaError):
            replace(_contribution("ok", source_ordinal=0), source_revision="")

    def test_tracked_restore_without_revision_fails_closed(
        self, saver: RolloutCheckpointSaver, sessions_root: Path,
    ) -> None:
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID, thread_id=MAIN_THREAD_ID,
        )
        manager = ContextSourceManager(owner=owner, control_state_port=saver)
        manager.register(
            ContextSourceDescriptor(
                source_id="skill:demo",
                source_kind="skill",
                name="demo",
                description="联合合同",
                internal_locator="/.boxteam/skills/demo/SKILL.md",
            )
        )
        manager.activate_skill_content("demo", "v1\n")
        _install_demo_snapshot(manager, "v1\n")
        manager.load_skill("demo", mode="tracked")
        batch = manager.prepare_pending()
        assert batch is not None
        manager.commit_model_call_pending(batch)
        with saver._storage._connect(MAIN_SESSION_ID, "") as probe:
            database_path = probe.execute("PRAGMA database_list").fetchone()[2]
        tamper = sqlite3.connect(database_path)
        try:
            with tamper:
                tamper.execute("PRAGMA ignore_check_constraints = ON")
                tamper.execute(
                    "UPDATE context_source_control_states SET latest_revision = NULL",
                )
        finally:
            tamper.close()
        with pytest.raises((ValueError, RuntimeError), match="latest"):
            ContextSourceManager(owner=owner, control_state_port=saver)

    def test_binding_errors_are_explicit(self) -> None:
        first = _contribution("dup-1", metadata={"source_ref": "alias"})
        second = _contribution("dup-2", metadata={"source_ref": "alias"})
        ref = replace(
            _ref_for(first, "plan-4"),
            source_ref="alias",
        )
        with pytest.raises(ValueError, match="映射到多个 contribution"):
            resolve_contribution_for_ref(ref, (first, second))

        overlay = _contribution(
            "overlay:ov:base",
            contribution_kind="overlay_base",
            metadata={"overlay_id": "ov", "overlay_ref": "ov", "overlay_role": "base"},
        )
        entry = ContextSelectionEntry(
            assembly_id="assembly-1",
            plan_ordinal=0,
            ref=replace(
                _ref_for(overlay, "plan-5"),
                base_delta_role="base",
                source_overlay_epoch=1,
            ),
            selection_kind="overlay_base",
            base_delta_role="base",
            source_overlay_epoch=1,
            included=True,
            detail_ref=DetailRef("assembly-1", "assembly-1", "detail-ov"),
            contribution_id="overlay:ov:base",
            contribution_ordinal=0,
            source_revision=overlay.source_revision,
            content_length=overlay.content_length,
            content_hash=overlay.content_hash,
            availability="available",
        )
        with pytest.raises(ItemSchemaError, match="included overlay 缺少 contribution"):
            validate_included_overlay_chain((entry,), ())


def _ref_for(contribution: ContextContribution, plan_id: str):
    from app.domain.itemized.enums import PayloadKind, SemanticKind
    from app.domain.itemized.refs import ContextRef

    return ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id="assembly-1",
        thread_id=MAIN_THREAD_ID,
        plan_id=plan_id,
        source_revision=contribution.source_revision,
        semantic_kind=SemanticKind.EXTENSION.value,
        payload_kind=PayloadKind.STRUCTURED_CONTENT.value,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref=contribution.contribution_id,
    )


# ---------------------------------------------------------------- registry 稳定性


class TestRegistryStabilityAcrossRestart:
    def test_ordinals_stable_and_continuous_after_restart(
        self, saver: RolloutCheckpointSaver, sessions_root: Path,
    ) -> None:
        first = _register(saver, MAIN_SESSION_ID, _contribution("c-1"))
        second = _register(saver, MAIN_SESSION_ID, _contribution("c-2"))
        assert (first, second) == (0, 1)

        restarted = _restarted(sessions_root)
        third = _register(restarted, MAIN_SESSION_ID, _contribution("c-3"))
        assert third == 2
        rows = {row["contribution_id"]: row["source_ordinal"] for row in _rows(restarted, MAIN_SESSION_ID)}
        assert rows == {"c-1": 0, "c-2": 1, "c-3": 2}

    def test_replace_keeps_same_registry_slot(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # middleware prompt-slot 的 typed 合同：replaceable slot 原位更新，
        # 不产生第二行、不漂移 registry slot。
        original = ContextContribution(
            contribution_id="prompt-slot:system",
            source_kind="sealed_request:system",
            source_revision="rev-a",
            content_hash=contribution_content_hash("prompt", "v1"),
            request_only=True,
            # replaceable slot 只能由 typed core 字段声明；metadata 中的
            # 同名历史 key 不再拥有解释权（旧路径已物理下线）。
            replaceable_source=True,
            contribution_kind="prompt",
            body="v1",
            source_ordinal=0,
        )
        first = _register(saver, MAIN_SESSION_ID, original)
        updated = replace(
            original,
            source_revision="rev-b",
            content_hash=contribution_content_hash("prompt", "v2"),
            body="v2",
        )
        saver.register_context_contribution(MAIN_SESSION_ID, updated)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-b"

    def test_typed_replaceable_source_enables_in_place_revision_update(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # (a) typed replaceable_source=True 时替换链路生效：同一 owner slot
        # 原位更新 revision，不产生第二行、不漂移 registry slot。
        original = _contribution("typed-replaceable", replaceable_source=True)
        first = _register(saver, MAIN_SESSION_ID, original)
        updated = replace(
            original,
            source_revision="rev-updated",
            content_hash=contribution_content_hash("prompt", "updated body"),
            content_length=None,
            body="updated body",
        )
        saver.register_context_contribution(MAIN_SESSION_ID, updated)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-updated"

    def test_metadata_only_replaceable_key_is_no_longer_replaceable(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # (b) 只放 metadata 键、没有 typed 字段时不再被视为可替换：旧
        # metadata 路径已物理下线，内容漂移必须显式报 identity 冲突。
        metadata_only = _contribution(
            "metadata-only-replaceable",
            metadata={"replaceable_source": True},
        )
        first = _register(saver, MAIN_SESSION_ID, metadata_only)
        drifted = replace(
            metadata_only,
            source_revision="rev-drifted",
            content_hash=contribution_content_hash("prompt", "drifted body"),
            content_length=None,
            body="drifted body",
        )
        with pytest.raises(ValueError, match="identity 冲突"):
            saver.register_context_contribution(MAIN_SESSION_ID, drifted)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-metadata-only-replaceable"

    def test_reregistration_of_same_source_is_idempotent(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # prepare 链路以原对象（source_ordinal=None）重注册同一 source：
        # registry 分配的 typed ordinal 不属于 caller 内容，不得触发
        # identity 冲突（R24 回归：typed 迁移后比较只豁免了 metadata 键）。
        contribution = _contribution("prepare-source")
        first = _register(saver, MAIN_SESSION_ID, contribution)
        # 重注册使用与首次完全相同的 caller 内容（ordinal 仍为 None）。
        saver.register_context_contribution(MAIN_SESSION_ID, contribution)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        # 内容漂移（source_revision 变化）仍必须显式拒绝。
        drifted = replace(contribution, source_revision="rev-drift")
        with pytest.raises(ValueError, match="identity 冲突"):
            saver.register_context_contribution(MAIN_SESSION_ID, drifted)

    def test_owner_threads_are_isolated(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        main_ordinal = _register(
            saver, MAIN_SESSION_ID, _contribution("shared-id"),
        )
        child_ordinal = _register(
            saver, CHILD_SESSION_ID, _contribution("shared-id"),
        )
        assert (main_ordinal, child_ordinal) == (0, 0)
        main_ids = {row["contribution_id"] for row in _rows(saver, MAIN_SESSION_ID)}
        child_ids = {row["contribution_id"] for row in _rows(saver, CHILD_SESSION_ID)}
        assert main_ids == {"shared-id"}
        assert child_ids == {"shared-id"}
        # CSM 控制状态同轮隔离：main 的 tracking 事实不出现在 child owner。
        main_owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID, thread_id=MAIN_THREAD_ID,
        )
        child_owner = ContextSourceOwnerKey(
            session_id=CHILD_SESSION_ID, thread_id=MAIN_THREAD_ID,
        )
        main_manager = ContextSourceManager(
            owner=main_owner, control_state_port=saver,
        )
        main_manager.register(
            ContextSourceDescriptor(
                source_id="skill:main-only",
                source_kind="skill",
                name="main-only",
                description="main",
                internal_locator="/x",
            )
        )
        child_manager = ContextSourceManager(
            owner=child_owner, control_state_port=saver,
        )
        with pytest.raises(KeyError):
            child_manager.source_observation_state("skill:main-only")


# ------------------------------------------------ watch/rewind/恢复 typed 消费


class TestComposerThreadIdentity:
    def test_request_only_refs_carry_catalog_main_thread_id(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        # composer 不持有 resolver，thread identity 只能由唯一 owner 从权威
        # catalog 解析后显式传入；request-only ref 必须携带该 main_thread_id，
        # 不能留空或自造，否则 (session_id, thread_id) 定位与其它 ref 不一致。
        _register(saver, MAIN_SESSION_ID, _contribution("thread-identity"))
        expected_thread_id = saver._resolve_main_thread_control(MAIN_SESSION_ID)[1]
        plan = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-thread",
        )
        request_only_refs = [
            ref for ref in plan.refs if ref.ref_type == "request_only"
        ]
        assert request_only_refs
        assert {ref.thread_id for ref in request_only_refs} == {expected_thread_id}


class TestSealedTypedFieldConsumption:
    def test_rewind_reconciles_only_from_frozen_control_state(
        self, saver: RolloutCheckpointSaver,
    ) -> None:
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID, thread_id=MAIN_THREAD_ID,
        )
        manager = ContextSourceManager(owner=owner, control_state_port=saver)
        manager.register(
            ContextSourceDescriptor(
                source_id="skill:demo",
                source_kind="skill",
                name="demo",
                description="联合合同",
                internal_locator="/x",
            )
        )
        manager.activate_skill_content("demo", "v1\n")
        _install_demo_snapshot(manager, "v1\n")
        manager.load_skill("demo", mode="tracked")
        batch = manager.prepare_pending()
        assert batch is not None
        manager.commit_model_call_pending(batch)

        # rewind：目标 view 的 active revision 回到旧值。rebuild 决策只
        # 消费控制状态的 applied/latest typed 事实，不读取来源正文以外
        # 的当前文件或事件。
        assert manager.restore_active_revision("skill:demo", "rev-stale-view") is True
        rebuild = manager.prepare_pending()
        assert rebuild is not None
        assert [delta.kind for delta in rebuild.deltas] == ["rebuild"]
        assert rebuild.deltas[0].content == "v1\n"
        manager.commit_model_call_pending(rebuild)
        (stored,) = saver.load_context_source_control_states(owner)
        assert stored.latest_visible_committed_revision == stored.latest_revision

        # untrack(frozen) 后 rewind 不恢复：snapshot/untracked 不重建 item。
        manager.load_skill("demo", mode="untrack")
        assert manager.restore_active_revision("skill:demo", "rev-older") is False
        assert manager.prepare_pending() is None

    def test_rewind_epoch_transition_is_consumed_once(self) -> None:
        """rewind/compaction 的 pending transition 无 wire bytes，只能被
        首个成功 seal 消费一次（A2 合同在联合场景的锚定）。"""
        state = initial_epoch_state(
            provider_profile="openai-responses:v1",
            tool_compatibility_key=TOOL_KEY_TOKEN,
        )
        transition = PendingPrefixEpochTransition(
            transition_id="transition-1",
            reason=EpochReason.REWIND,
            turn_id="turn-1",
        )
        rebuilt = open_rebuild_epoch(
            state,
            reason=EpochReason.REWIND,
            provider_profile="openai-responses:v1",
            tool_compatibility_key=TOOL_KEY_TOKEN,
            transition=transition,
            turn_id="turn-1",
        )
        sealed = seal_assembly(rebuilt, assembly_id="asm-1", transition=transition)
        assert sealed.consumed_transition is not None
        assert sealed.consumed_transition.consumed is True
        # 消费副本再次进入 seal：重复应用显式失败，不产生第二个 epoch。
        with pytest.raises(Exception, match="已消费"):
            seal_assembly(
                rebuilt,
                assembly_id="asm-2",
                transition=sealed.consumed_transition,
            )

    def test_restart_restore_consumes_only_registry_typed_fields(
        self, saver: RolloutCheckpointSaver, sessions_root: Path,
    ) -> None:
        _register(
            saver,
            MAIN_SESSION_ID,
            _contribution(
                "typed-1",
                metadata={"source_ordinal": 123, "selection_only": True},
            ),
        )
        before = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-6",
        )
        before_hash = context_plan_hash(before)

        restarted = _restarted(sessions_root)
        after = restarted.compose_committed_context_plan(
            MAIN_SESSION_ID, plan_id="plan-6",
        )
        # 恢复只消费 registry 封存 typed 字段：metadata 中的旧 ordinal
        # 键与对抗 selection_only 键均为 inert，前后 hash 一致。
        assert context_plan_hash(after) == before_hash
        (row,) = _rows(restarted, MAIN_SESSION_ID)
        contribution = after.contributions[0]
        assert contribution.source_ordinal == row["source_ordinal"]
        assert contribution.source_ordinal != 123

    @pytest.mark.asyncio
    async def test_out_of_order_watch_consumes_authoritative_revision(
        self, tmp_path: Path,
    ) -> None:
        from tests.unit.services.infrastructure.test_context_source_reactor import (
            _reactor_fixture,
        )

        manager, registry, watch_service, reactor, _lifetime_scope = _reactor_fixture(
            tmp_path,
        )
        try:
            registration = registry.snapshot(
                "boxteam://workspace/skill/demo",
            )
            manager.activate_skill_content("demo", registration.content)
            _install_demo_snapshot(manager, registration.content)
            manager.load_skill("demo", mode="tracked")
            baseline = manager.prepare_pending()
            assert baseline is not None
            manager.commit_model_call_pending(baseline)
            reactor.sync_sources()

            # 乱序：v2 的事件晚于 v3 到达，标记被回写为旧 revision；
            # 消费时以来源 owner 的权威内存快照 revision 裁决。
            latest_visible_committed_revision = registration.revision
            _write_skill(tmp_path / "workspace", "v2\n")
            mid_revision = registry.refresh(
                "boxteam://workspace/skill/demo"
            ).revision
            assert mid_revision != latest_visible_committed_revision
            assert manager.mark_pending_observation(
                "skill:demo", mid_revision,
            ) is True
            _write_skill(tmp_path / "workspace", "v3\n")
            fresh_revision = registry.refresh(
                "boxteam://workspace/skill/demo"
            ).revision
            assert fresh_revision != mid_revision
            assert manager.mark_pending_observation(
                "skill:demo", fresh_revision,
            ) is True
            # 乱序：v2 的事件晚于 v3 到达，标记被回写为旧 revision；
            # 消费时以来源 owner 的权威内存快照 revision 裁决。
            assert manager.mark_pending_observation(
                "skill:demo", mid_revision,
            ) is True
            assert manager.pending_observation_count() == 1

            observations = tuple(reactor.drain())
            assert [item.revision for item in observations] == [fresh_revision]
            # 消费是 owner 的显式动作：正文只从权威内存快照读取。
            assert manager.observe(
                observations[0].source_id,
                reactor.content_for(observations[0]),
                revision=observations[0].revision,
            ) is True
            batch = manager.prepare_pending()
            assert batch is not None
            assert [delta.revision for delta in batch.deltas] == [fresh_revision]
            assert [delta.kind for delta in batch.deltas] == ["delta"]
            manager.commit_model_call_pending(batch)
            # reactor fixture 的 CSM 无持久化端口（纯内存）；applied 推进
            # 以 latest revision 收敛为证。
            assert manager.source_observation_state("skill:demo") == (
                "tracked",
                fresh_revision,
            )
        finally:
            await watch_service.shutdown()
