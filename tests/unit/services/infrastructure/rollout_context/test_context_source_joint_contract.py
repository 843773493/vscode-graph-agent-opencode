"""A4 联合合同：CSM / ContextStore owner / itemized registry / typed prefix。

OpenSpec add-context-injection-lifecycle 1.6：extensions/metadata 中的同名
控制 key 不改变 tracking、base/delta、selection、role、stable prefix；
缺失 typed identity/revision/source binding/source ordinal 显式报错；
registry 重启后 ordinal 稳定、replace 保留同 slot、不同 owner thread 隔离；
watch 乱序/重连、rewind/compaction、fork 恢复只消费已封存 typed 字段。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.epoch.prefix_epoch import (
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
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.plan_hash import context_plan_hash
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
)
from app.domain.itemized.request_plan import (
    ContextContribution,
    SourceBinding,
    resolve_contribution_for_ref,
    validate_included_overlay_chain,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
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
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.unit.services.infrastructure.rollout_context.conftest import SourceSealCase
from tests.unit.services.infrastructure.test_context_source_reactor import (
    _write_skill,
)

MAIN_SESSION_ID = "ses_019c3dbcb2f6749b85bed7b51dfe889d"
CHILD_SESSION_ID = "ses_019c52c6b01c7733850a1aaf64ac579e"
ALT_SESSION_ID = "ses_019c58bdb8c670748915904c8d96a75f"


TOOL_KEY_TOKEN = "sha256:jcs:v1:" + "a" * 64


ADVERSARIAL_CONTROL_KEYS = {
    "source_ordinal": 999,
    "replaceable_source": True,
    "replacement_policy": "replaceable",
    "selection_only": True,
    "selection_role": "backing_only",
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


def _create_session_node(
    root: Path, session_id: str, parent_node_id: str | None = None
):
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


@pytest.fixture
def empty_tool_set() -> ToolSetSnapshot:
    """联合来源场景显式冻结空工具集合，不依赖生产接口补默认值。"""
    return ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@pytest.fixture
def source_seal_owner(saver: RolloutCheckpointSaver, frozen_provider_wire) -> SourceSealCase:
    owner = ContextSourceOwnerKey(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID))
    accepted = saver.accept_turn(
        owner.session_id, thread_id=owner.thread_id,
        accepted_ingress_id="joint-ingress", acceptance_idempotency_key="joint-accept",
        job_id="joint-job", admission_idempotency_key="joint-admission",
        payload="联合来源真实 seal 合同", root_item_id="item-joint-ingress",
    )
    return SourceSealCase(saver, owner, accepted, frozen_provider_wire)


@pytest.fixture
def source_reactor(tmp_path: Path, source_seal_owner: SourceSealCase):
    workspace = tmp_path / "workspace"
    skill_path = _write_skill(workspace, "v1\n")
    watch_service = WorkspaceFileWatchService(workspace_root=workspace)
    registry = WorkspaceFileResourceRegistry(
        workspace_root=workspace, watch_service=watch_service, project_root=tmp_path,
    )
    registry.register_file(uri="boxteam://workspace/skill/demo", path=skill_path)
    manager = source_seal_owner.manager()
    manager.register(ContextSourceDescriptor(
        source_id="skill:demo", source_kind="skill", name="demo", description="事件驱动测试 Skill",
        internal_locator="/.boxteam/skills/demo/SKILL.md", resource_uri="boxteam://workspace/skill/demo",
    ))
    lifetime_scope = LifetimeScope("joint-source-reactor")
    reactor = ContextSourceReactor(
        sources=registry, context_sources=manager, lifetime_scope=lifetime_scope, reactor_id="joint-source-reactor",
    )
    return manager, registry, watch_service, reactor, lifetime_scope


def _restarted(sessions_root: Path) -> RolloutCheckpointSaver:
    """模拟进程重启：全新 Saver/Storage 实例，状态只来自 SQLite。"""
    return RolloutCheckpointSaver(sessions_root)


def _contribution(
    contribution_id: str,
    *,
    body: str = "source body",
    annotations: dict[str, object] | None = None,
    source_binding: SourceBinding | None = None,
    source_ordinal: int | None = None,
    contribution_kind: str = "prompt",
    source_kind: str = "workspace_instructions",
    selection_role: str = "direct",
    replacement_policy: str = "immutable",
) -> ContextContribution:
    return ContextContribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_revision="rev-" + contribution_id,
        source_binding=source_binding,
        content_hash=contribution_content_hash(contribution_kind, body),
        request_only=True,
        extensions={"boxteam.test.annotation": {"extension_version": "1", "protection": "public", "value": annotations or {}}},
        contribution_kind=contribution_kind,
        body=body,
        source_ordinal=source_ordinal,
        selection_role=selection_role,
        replacement_policy=replacement_policy,
    )


def _register(
    saver: RolloutCheckpointSaver,
    session_id: str,
    contribution: ContextContribution,
) -> int:
    """经唯一 owner 注册；返回 registry 分配的 typed source_ordinal。"""
    saver.register_context_contribution(session_id, saver._storage.main_thread_id(session_id), contribution)
    thread_id = saver._storage.main_thread_id(session_id)
    for row in saver._storage.list_context_contributions(session_id, thread_id):
        if row["contribution_id"] == contribution.contribution_id:
            ordinal = row["source_ordinal"]
            assert isinstance(ordinal, int) and not isinstance(ordinal, bool)
            return ordinal
    raise AssertionError("registry 行缺失")


def _rows(saver: RolloutCheckpointSaver, session_id: str) -> list[dict]:
    thread_id = saver._storage.main_thread_id(session_id)
    return list(saver._storage.list_context_contributions(session_id, thread_id))


# ---------------------------------------------------------------- 对抗 key


class TestAdversarialControlKeys:
    def test_same_name_keys_do_not_change_plan_hash_or_registry_slot(
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        clean = _contribution("contribution-clean", annotations={}, source_binding=SourceBinding(source_ref='c'))
        adversarial = _contribution(
            "contribution-clean",
            annotations={**ADVERSARIAL_CONTROL_KEYS}, source_binding=SourceBinding(source_ref='c'),
        )
        ordinal_clean = _register(saver, MAIN_SESSION_ID, clean)
        assert ordinal_clean == 0
        plan_clean = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-1", tool_set=empty_tool_set,
        )

        # 同一 session 内只换 metadata：SQL 清理后经全新 owner（重启等价）
        # 重注册对抗孪生，typed 事实逐字段相同。
        with saver._storage._connect(
            saver._storage.main_thread_id(MAIN_SESSION_ID),
            "",
            session_id=MAIN_SESSION_ID,
        ) as probe:
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
            MAIN_SESSION_ID, restarted._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-1", tool_set=empty_tool_set,
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
        self,
        saver: RolloutCheckpointSaver,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        # typed selection_role=direct 说它可独立候选：metadata 的 selection_only
        # 键不能把它降级为 manifest backing。
        full_with_flag = _contribution(
            "full-with-flag",
            annotations={'selection_only': True},
        )
        _register(saver, MAIN_SESSION_ID, full_with_flag)
        # typed selection_role=backing_only 是 manifest backing：即使没有
        # selection_only 键也必须跳过，不能凭缺 key 变成第二个 request-only ref。
        overlay_without_flag = _contribution(
            "overlay:ov-1:base",
            contribution_kind="overlay_base",
            selection_role="backing_only",
            source_binding=SourceBinding(source_ref="ov-1", overlay_ref="ov-1", overlay_id="ov-1", overlay_role="base", source_overlay_epoch=0),
        )
        _register(saver, MAIN_SESSION_ID, overlay_without_flag)

        plan = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-2", tool_set=empty_tool_set,
        )
        ref_ids = {ref.ref_id for ref in plan.refs if ref.ref_type == "request_only"}
        assert "full-with-flag" in ref_ids
        assert "overlay:ov-1:base" not in ref_ids

    def test_observation_extensions_are_inert_for_decisions(
        self,
        saver: RolloutCheckpointSaver,
    ) -> None:
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID,
            thread_id=saver._storage.main_thread_id(MAIN_SESSION_ID),
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
                "boxteam.control/v1": {
                    "extension_version": "future-7", "protection": "public",
                    "value": dict(ADVERSARIAL_CONTROL_KEYS),
                },
            },
        )
        assert clean.observation_id == adversarial.observation_id
        clean_delta = build_source_lifecycle_decision(clean, decision_kind="delta")
        adversarial_delta = build_source_lifecycle_decision(
            adversarial,
            decision_kind="delta",
        )
        assert clean_delta == replace(adversarial_delta, extensions={})
        assert adversarial_delta.extensions == adversarial.extensions

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

    def test_restore_rejects_registry_row_with_invalid_ordinal(
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        _register(saver, MAIN_SESSION_ID, _contribution("null-ordinal"))
        with saver._storage._connect(
            saver._storage.main_thread_id(MAIN_SESSION_ID),
            "",
            session_id=MAIN_SESSION_ID,
        ) as probe:
            database_path = probe.execute("PRAGMA database_list").fetchone()[2]
        tamper = sqlite3.connect(database_path)
        try:
            with tamper:
                tamper.execute("PRAGMA ignore_check_constraints = ON")
                tamper.execute(
                    "UPDATE context_contributions SET source_ordinal = -1",
                )
        finally:
            tamper.close()
        restarted = _restarted(sessions_root)
        with pytest.raises((ValueError, RuntimeError), match="source_ordinal"):
            restarted.compose_committed_context_plan(
                MAIN_SESSION_ID, restarted._storage.main_thread_id(MAIN_SESSION_ID),
                plan_id="plan-3", tool_set=empty_tool_set,
            )

    def test_contribution_requires_identity_and_revision(self) -> None:
        with pytest.raises(ItemSchemaError):
            _contribution("", source_ordinal=0)
        with pytest.raises(ItemSchemaError):
            replace(_contribution("ok", source_ordinal=0), source_revision="")

    def test_tracked_restore_without_revision_fails_closed(
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
        source_seal_owner: SourceSealCase,
    ) -> None:
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID,
            thread_id=saver._storage.main_thread_id(MAIN_SESSION_ID),
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
        committed, _binding = source_seal_owner.seal(manager)
        assert committed.deltas == batch.deltas
        with saver._storage._connect(
            saver._storage.main_thread_id(MAIN_SESSION_ID),
            "",
            session_id=MAIN_SESSION_ID,
        ) as probe:
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
        first = _contribution("dup-1", annotations={}, source_binding=SourceBinding(source_ref='alias'))
        second = _contribution("dup-2", annotations={}, source_binding=SourceBinding(source_ref='alias'))
        ref = replace(
            _ref_for(first, "plan-4"),
            source_ref="alias",
        )
        with pytest.raises(ValueError, match="映射到多个 contribution"):
            resolve_contribution_for_ref(ref, (first, second))

        overlay = _contribution(
            "overlay:ov:base",
            contribution_kind="overlay_base",
            source_binding=SourceBinding(source_ref="ov", overlay_ref="ov", overlay_id="ov", overlay_role="base", source_overlay_epoch=0),
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
            detail_ref=DetailRef(
                "assembly-1", "thr_019bfb6b5f3c72178c7e8393ce89e7a6", "assembly-1", "detail-ov"
            ),
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
    from app.domain.itemized.identity.refs import ContextRef

    return ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id="assembly-1",
        thread_id="thr_019bfb6b5f3c72178c7e8393ce89e7a6",
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
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
    ) -> None:
        first = _register(saver, MAIN_SESSION_ID, _contribution("c-1"))
        second = _register(saver, MAIN_SESSION_ID, _contribution("c-2"))
        assert (first, second) == (0, 1)

        restarted = _restarted(sessions_root)
        third = _register(restarted, MAIN_SESSION_ID, _contribution("c-3"))
        assert third == 2
        rows = {
            row["contribution_id"]: row["source_ordinal"]
            for row in _rows(restarted, MAIN_SESSION_ID)
        }
        assert rows == {"c-1": 0, "c-2": 1, "c-3": 2}

    def test_replace_keeps_same_registry_slot(
        self,
        saver: RolloutCheckpointSaver,
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
            replacement_policy="replaceable",
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
        saver.register_context_contribution(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), updated, expected_source_revision=original.source_revision)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-b"

    def test_typed_replaceable_source_enables_in_place_revision_update(
        self,
        saver: RolloutCheckpointSaver,
    ) -> None:
        # (a) typed replacement_policy=replaceable 时替换链路生效：同一 owner
        # slot 原位更新 revision，不产生第二行、不漂移 registry slot。
        original = _contribution("typed-replaceable", replacement_policy="replaceable")
        first = _register(saver, MAIN_SESSION_ID, original)
        updated = replace(
            original,
            source_revision="rev-updated",
            content_hash=contribution_content_hash("prompt", "updated body"),
            content_length=None,
            body="updated body",
        )
        saver.register_context_contribution(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), updated, expected_source_revision=original.source_revision)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-updated"

    def test_metadata_only_replaceable_key_is_no_longer_replaceable(
        self,
        saver: RolloutCheckpointSaver,
    ) -> None:
        # (b) 只放 metadata 键、没有 typed 字段时不再被视为可替换：旧
        # metadata 路径已物理下线，内容漂移必须显式报 identity 冲突。
        metadata_only = _contribution(
            "metadata-only-replaceable",
            annotations={'replaceable_source': True},
        )
        first = _register(saver, MAIN_SESSION_ID, metadata_only)
        drifted = replace(
            metadata_only,
            source_revision="rev-drifted",
            content_hash=contribution_content_hash("prompt", "drifted body"),
            content_length=None,
            body="drifted body",
        )
        with pytest.raises(ValueError, match="identity.*冲突"):
            saver.register_context_contribution(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), drifted)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        assert rows[0]["source_revision"] == "rev-metadata-only-replaceable"

    def test_reregistration_of_same_source_is_idempotent(
        self,
        saver: RolloutCheckpointSaver,
    ) -> None:
        # prepare 链路以原对象（source_ordinal=None）重注册同一 source：
        # registry 分配的 typed ordinal 不属于 caller 内容，不得触发
        # identity 冲突（R24 回归：typed 迁移后比较只豁免了 metadata 键）。
        contribution = _contribution("prepare-source")
        first = _register(saver, MAIN_SESSION_ID, contribution)
        # 重注册使用与首次完全相同的 caller 内容（ordinal 仍为 None）。
        saver.register_context_contribution(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), contribution)
        rows = _rows(saver, MAIN_SESSION_ID)
        assert len(rows) == 1
        assert rows[0]["source_ordinal"] == first
        # 内容漂移（source_revision 变化）仍必须显式拒绝。
        drifted = replace(contribution, source_revision="rev-drift")
        with pytest.raises(ValueError, match="identity.*冲突"):
            saver.register_context_contribution(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), drifted)

    def test_owner_threads_are_isolated(
        self,
        saver: RolloutCheckpointSaver,
    ) -> None:
        main_ordinal = _register(
            saver,
            MAIN_SESSION_ID,
            _contribution("shared-id"),
        )
        child_ordinal = _register(
            saver,
            CHILD_SESSION_ID,
            _contribution("shared-id"),
        )
        assert (main_ordinal, child_ordinal) == (0, 0)
        main_ids = {row["contribution_id"] for row in _rows(saver, MAIN_SESSION_ID)}
        child_ids = {row["contribution_id"] for row in _rows(saver, CHILD_SESSION_ID)}
        assert main_ids == {"shared-id"}
        assert child_ids == {"shared-id"}
        # CSM 控制状态同轮隔离：main 的 tracking 事实不出现在 child owner。
        main_owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID,
            thread_id=saver._storage.main_thread_id(MAIN_SESSION_ID),
        )
        child_owner = ContextSourceOwnerKey(
            session_id=CHILD_SESSION_ID,
            thread_id=saver._storage.main_thread_id(CHILD_SESSION_ID),
        )
        main_manager = ContextSourceManager(
            owner=main_owner,
            control_state_port=saver,
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
            owner=child_owner,
            control_state_port=saver,
        )
        with pytest.raises(KeyError):
            child_manager.source_observation_state("skill:main-only")


# ------------------------------------------------ watch/rewind/恢复 typed 消费


class TestComposerThreadIdentity:
    def test_request_only_refs_carry_catalog_main_thread_id(
        self,
        saver: RolloutCheckpointSaver,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        # composer 不持有 resolver，thread identity 只能由唯一 owner 从权威
        # catalog 解析后显式传入；request-only ref 必须携带该 main_thread_id，
        # 不能留空或自造，否则 (session_id, thread_id) 定位与其它 ref 不一致。
        _register(saver, MAIN_SESSION_ID, _contribution("thread-identity"))
        expected_thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        plan = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-thread", tool_set=empty_tool_set,
        )
        request_only_refs = [ref for ref in plan.refs if ref.ref_type == "request_only"]
        assert request_only_refs
        assert {ref.thread_id for ref in request_only_refs} == {expected_thread_id}

    def test_canonical_refs_carry_catalog_main_thread_id(
        self,
        saver: RolloutCheckpointSaver,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        # committed_context_refs 从已提交 item 生成 canonical ref 时，thread 半
        # 必须取 catalog 权威 main_thread_id，不能把承载 session 定位的
        # snapshot.thread_id 同时当 session_id 与 thread_id 填两遍。
        from app.core.execution_admission_context import execution_admission_scope
        from app.core.session_thread_context import session_thread_owner_scope
        from app.domain.itemized.enums import (
            CanonicalItemStatus,
            PayloadKind,
            SemanticKind,
            TurnScope,
        )
        from app.domain.itemized.mutation_intents import MutationIntentOwner
        from app.domain.itemized.records import CanonicalItemRecord

        expected_thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        accepted = saver.accept_turn(
            MAIN_SESSION_ID,
            thread_id=expected_thread_id,
            accepted_ingress_id="canonical-thread-ingress",
            acceptance_idempotency_key="canonical-thread-acceptance",
            job_id="canonical-thread-job",
            admission_idempotency_key="canonical-thread-admission",
            payload="真实 user root",
            payload_kind="text",
        )
        admission = saver.execution_admission(
            MAIN_SESSION_ID,
            thread_id=expected_thread_id,
            execution_id=accepted["initial_execution_id"],
        )
        item = CanonicalItemRecord.create(
            item_sequence=2,
            item_id="item-thread-identity",
            semantic_kind=SemanticKind.ASSISTANT_OUTPUT.value,
            payload_kind=PayloadKind.TEXT.value,
            status=CanonicalItemStatus.COMPLETED,
            producer_ref={"producer_kind": "provider", "producer_id": "model-call-1"},
            payload="最终回答",
            projection_identity=ItemProjectionIdentity(projection_message_id="message-thread-identity"),
            turn_id=accepted["turn_id"],
            turn_scope=TurnScope.TURN_MEMBER,
        )
        with (
            session_thread_owner_scope(
                MutationIntentOwner(MAIN_SESSION_ID, expected_thread_id),
                langgraph_thread_id=expected_thread_id,
            ),
            execution_admission_scope(admission, langgraph_thread_id=expected_thread_id),
        ):
            saver.append_items(MAIN_SESSION_ID, (item,), thread_id=expected_thread_id)
        plan = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-canonical-thread", tool_set=empty_tool_set,
            include_pending_notices=False,
        )
        canonical_refs = [ref for ref in plan.refs if ref.ref_type == "canonical_item"]
        assert canonical_refs
        assert {ref.session_id for ref in canonical_refs} == {MAIN_SESSION_ID}
        assert {ref.thread_id for ref in canonical_refs} == {expected_thread_id}
        # thread 半绝不能退化成 session id。
        assert expected_thread_id != MAIN_SESSION_ID


class TestSealedTypedFieldConsumption:
    def test_explicit_tracked_rebind_freezes_old_item_bytes(
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
        source_seal_owner: SourceSealCase,
    ) -> None:
        """6.2-B：显式 tracked rebind 冻结旧 registration，真实 seal 追加
        当前 effective entry 的完整 activation；旧 source frame/detail 逐字节不变。"""
        owner = ContextSourceOwnerKey(
            session_id=MAIN_SESSION_ID,
            thread_id=saver._storage.main_thread_id(MAIN_SESSION_ID),
        )
        manager = ContextSourceManager(
            owner=owner,
            control_state_port=saver,
            mutation_intent_port=saver,
        )
        manager.register(
            ContextSourceDescriptor(
                source_id="skill:gateway:demo",
                source_kind="skill",
                name="demo",
                description="Gateway 全局 Skill",
                internal_locator="/.boxteam/skills/demo/SKILL.md",
                resource_uri="boxteam://gateway/local/resources/skills/demo/SKILL.md",
            )
        )

        def _install(
            entry_layer: str,
            body: str,
            display_uri: str,
        ) -> SkillCatalogBinding:
            binding = SkillCatalogBinding(
                name="demo",
                resource_id=f"skill-entry:{entry_layer}:demo:activation",
                entry_identity=f"skill-entry:{entry_layer}:demo",
                display_uri=display_uri,
                activation_revision=_revision(body),
                body=body,
            )
            manager.install_skill_activation_snapshot(
                SkillCatalogActivationSnapshot(
                    catalog_revision=f"sha256:catalog-{entry_layer}",
                    entries={"demo": binding},
                )
            )
            return binding

        _install(
            "gateway",
            "gateway body\n",
            "boxteam://gateway/local/resources/skills/demo/SKILL.md",
        )
        first = manager.load_skill("demo", mode="tracked")
        assert first.status == "loaded"
        batch = manager.prepare_pending()
        assert batch is not None
        committed, first_binding = source_seal_owner.seal(manager)
        assert committed.deltas == batch.deltas
        first_snapshot = saver.get_context_assembly(
            owner.session_id, owner.thread_id, assembly_id=first_binding.prefix.assembly_id,
        )
        old_snapshot_bytes = canonical_json_bytes(first_snapshot.to_dict())
        old_bodies = saver._request_content_for_plan(
            owner.session_id, owner.thread_id, "", first_snapshot.as_sealed_plan(), None,
        )
        (old_frame,) = tuple(frame for frame in first_binding.prefix.state.frames
                             if frame.item_ref.source_identity == "skill:gateway:demo")
        assert old_frame.content == old_bodies[old_frame.item_ref.ref_id] == "gateway body\n"

        with saver._storage._connect(
            saver._storage.main_thread_id(MAIN_SESSION_ID),
            "",
            session_id=MAIN_SESSION_ID,
        ) as probe:
            database_path = probe.execute("PRAGMA database_list").fetchone()[2]
        conn = sqlite3.connect(database_path)
        try:
            old_rows = conn.execute(
                "SELECT * FROM assembly_item_refs WHERE assembly_id=? AND ref_id=?",
                (first_snapshot.assembly_id, old_frame.item_ref.ref_id),
            ).fetchall()
        finally:
            conn.close()
        assert len(old_rows) == 1

        # 同名 workspace 高优先级 effective entry 出现（不同 source identity）。
        manager.register(
            ContextSourceDescriptor(
                source_id="skill:workspace:demo",
                source_kind="skill",
                name="demo",
                description="Workspace Skill",
                internal_locator="/ws/.boxteam/skills/demo/SKILL.md",
                resource_uri="boxteam://workspace/ws/resources/skills/demo/SKILL.md",
            )
        )
        new_binding = _install(
            "workspace",
            "workspace body\n",
            "boxteam://workspace/ws/resources/skills/demo/SKILL.md",
        )

        rebound = manager.load_skill("demo", mode="tracked")
        assert rebound.status == "rebound"
        assert rebound.source_rebound is True
        assert rebound.tracked is True
        assert rebound.display_uri == new_binding.display_uri
        rebound_batch = manager.prepare_pending()
        assert rebound_batch is not None
        committed, rebound_binding = source_seal_owner.seal(manager)
        assert committed.deltas == rebound_batch.deltas
        rebound_snapshot = saver.get_context_assembly(
            owner.session_id, owner.thread_id, assembly_id=rebound_binding.prefix.assembly_id,
        )
        (new_frame,) = tuple(frame for frame in rebound_binding.prefix.state.frames
                             if frame.item_ref.source_identity == "skill:workspace:demo")
        new_bodies = saver._request_content_for_plan(
            owner.session_id, owner.thread_id, "", rebound_snapshot.as_sealed_plan(), None,
        )
        assert new_frame.content == new_bodies[new_frame.item_ref.ref_id] == "workspace body\n"

        conn = sqlite3.connect(database_path)
        try:
            after_old_rows = conn.execute(
                "SELECT * FROM assembly_item_refs WHERE assembly_id=? AND ref_id=?",
                (first_snapshot.assembly_id, old_frame.item_ref.ref_id),
            ).fetchall()
            new_rows = conn.execute(
                "SELECT * FROM assembly_item_refs WHERE assembly_id=? AND ref_id=?",
                (rebound_snapshot.assembly_id, new_frame.item_ref.ref_id),
            ).fetchall()
        finally:
            conn.close()
        # 旧 selected 行与冻结 detail 逐字节不变，且未被删除。
        assert after_old_rows == old_rows
        restored = saver.get_context_assembly(owner.session_id, owner.thread_id, assembly_id=first_snapshot.assembly_id)
        assert canonical_json_bytes(restored.to_dict()) == old_snapshot_bytes
        assert saver._request_content_for_plan(
            owner.session_id, owner.thread_id, "", restored.as_sealed_plan(), None,
        ) == old_bodies
        assert old_frame in rebound_binding.prefix.state.frames
        # 新 effective entry 追加完整 activation frame 与真实 selection。
        assert len(new_rows) == 1
        # 旧 registration 冻结为 untracked；新 registration tracked。
        states = {
            item.source_id: item
            for item in saver.load_context_source_control_states(owner)
        }
        assert states["skill:gateway:demo"].tracking_status == "untracked"
        assert states["skill:workspace:demo"].tracking_status == "tracked"
        assert states["skill:workspace:demo"].latest_visible_committed_revision == new_binding.activation_revision

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
        self,
        saver: RolloutCheckpointSaver,
        sessions_root: Path,
        empty_tool_set: ToolSetSnapshot,
    ) -> None:
        _register(
            saver,
            MAIN_SESSION_ID,
            _contribution(
                "typed-1",
                annotations={'source_ordinal': 123, 'selection_only': True, 'selection_role': 'backing_only', 'replacement_policy': 'replaceable'},
            ),
        )
        before = saver.compose_committed_context_plan(
            MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-6", tool_set=empty_tool_set,
        )
        before_hash = context_plan_hash(before)

        restarted = _restarted(sessions_root)
        after = restarted.compose_committed_context_plan(
            MAIN_SESSION_ID, restarted._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="plan-6", tool_set=empty_tool_set,
        )
        # 恢复只消费 registry 封存 typed 字段：metadata 中的旧 ordinal 键与
        # 对抗 selection_only/selection_role/replacement_policy 键均为 inert，
        # 前后 hash 一致。
        assert context_plan_hash(after) == before_hash
        (row,) = _rows(restarted, MAIN_SESSION_ID)
        contribution = after.contributions[0]
        assert contribution.source_ordinal == row["source_ordinal"]
        assert contribution.source_ordinal != 123
        assert contribution.selection_role == "direct"
        assert contribution.replacement_policy == "immutable"

    @pytest.mark.asyncio
    async def test_out_of_order_watch_consumes_authoritative_revision(
        self,
        tmp_path: Path,
        source_seal_owner: SourceSealCase,
        source_reactor,
    ) -> None:
        manager, registry, watch_service, reactor, lifetime_scope = source_reactor
        try:
            registration = registry.snapshot(
                "boxteam://workspace/skill/demo",
            )
            manager.activate_skill_content("demo", registration.content)
            _install_demo_snapshot(manager, registration.content)
            manager.load_skill("demo", mode="tracked")
            baseline = manager.prepare_pending()
            assert baseline is not None
            committed, _binding = source_seal_owner.seal(manager)
            assert committed.deltas == baseline.deltas
            reactor.sync_sources()

            # 乱序：v2 的事件晚于 v3 到达，标记被回写为旧 revision；
            # 消费时以来源 owner 的权威内存快照 revision 裁决。
            latest_visible_committed_revision = registration.revision
            _write_skill(tmp_path / "workspace", "v2\n")
            mid_revision = registry.refresh("boxteam://workspace/skill/demo").revision
            assert mid_revision != latest_visible_committed_revision
            assert (
                manager.mark_pending_observation(
                    "skill:demo",
                    mid_revision,
                )
                is True
            )
            _write_skill(tmp_path / "workspace", "v3\n")
            fresh_revision = registry.refresh("boxteam://workspace/skill/demo").revision
            assert fresh_revision != mid_revision
            assert (
                manager.mark_pending_observation(
                    "skill:demo",
                    fresh_revision,
                )
                is True
            )
            # 乱序：v2 的事件晚于 v3 到达，标记被回写为旧 revision；
            # 消费时以来源 owner 的权威内存快照 revision 裁决。
            assert (
                manager.mark_pending_observation(
                    "skill:demo",
                    mid_revision,
                )
                is True
            )
            assert manager.pending_observation_count() == 1

            observations = tuple(reactor.drain())
            assert [item.revision for item in observations] == [fresh_revision]
            # 消费是 owner 的显式动作：正文只从权威内存快照读取。
            assert (
                manager.observe(
                    observations[0].source_id,
                    reactor.content_for(observations[0]),
                    revision=observations[0].revision,
                )
                is True
            )
            batch = manager.prepare_pending()
            assert batch is not None
            assert [delta.revision for delta in batch.deltas] == [fresh_revision]
            assert [delta.kind for delta in batch.deltas] == ["delta"]
            committed, binding = source_seal_owner.seal(manager)
            assert committed.deltas == batch.deltas
            assert manager.source_observation_state("skill:demo") == (
                "tracked",
                fresh_revision,
            )
            (stored,) = source_seal_owner.saver.load_context_source_control_states(source_seal_owner.owner)
            assert stored.latest_visible_committed_revision == fresh_revision
            assert binding.source_revisions[0].revision == fresh_revision
        finally:
            await lifetime_scope.close()
            await watch_service.shutdown()


class TestTypedBindingRegistryBoundary:
    def test_restart_preserves_unknown_extensions_and_explicit_root(self, saver, sessions_root, empty_tool_set):
        contribution = replace(
            _contribution(
                "opaque-restart",
                source_binding=SourceBinding(source_ref="real-source"),
                annotations={
                    "source_ref": "fake-source",
                    "root_placement": "tail_only",
                    "source_binding": {"overlay_id": "fake-overlay"},
                    "source_ordinal": 999,
                },
            ),
            root_placement="root_eligible",
        )
        ordinal = _register(saver, MAIN_SESSION_ID, contribution)
        restarted = _restarted(sessions_root)
        plan = restarted.compose_committed_context_plan(
            MAIN_SESSION_ID, restarted._storage.main_thread_id(MAIN_SESSION_ID),
            plan_id="opaque-restart-plan", tool_set=empty_tool_set,
        )
        (restored,) = plan.contributions
        assert restored.extensions == contribution.extensions
        assert restored.source_binding == contribution.source_binding
        assert restored.root_placement == "root_eligible"
        assert restored.source_ordinal == ordinal
        assert restored.selection_role == "direct"
        assert restored.replacement_policy == "immutable"

    @pytest.mark.parametrize("missing", ["extensions", "source_binding", "root_placement"])
    def test_sql_rejects_incomplete_binding_manifest(self, saver, missing):
        contribution = _contribution("sql-required-binding")
        _register(saver, MAIN_SESSION_ID, contribution)
        invalid = contribution.binding_manifest()
        del invalid[missing]
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(
                    "UPDATE context_contributions SET binding_manifest_json = ?",
                    (json.dumps(invalid),),
                )
            assert connection.execute(
                "SELECT source_ordinal FROM context_contributions"
            ).fetchone() == (0,)

    @pytest.mark.parametrize("ordinal", [None, -1, 1.5])
    def test_sql_rejects_missing_or_invalid_registry_ordinal(self, saver, ordinal):
        _register(saver, MAIN_SESSION_ID, _contribution("sql-required-ordinal"))
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE context_contributions SET source_ordinal = ?", (ordinal,),
                )
            assert connection.execute(
                "SELECT source_ordinal FROM context_contributions"
            ).fetchone() == (0,)

    def test_restore_rejects_missing_binding_even_if_sql_check_is_bypassed(self, saver, sessions_root, empty_tool_set):
        contribution = _contribution("restore-required-binding")
        _register(saver, MAIN_SESSION_ID, contribution)
        invalid = contribution.binding_manifest()
        del invalid["source_binding"]
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE context_contributions SET binding_manifest_json = ?",
                (json.dumps(invalid, separators=(",", ":"), sort_keys=True),),
            )
        restarted = _restarted(sessions_root)
        with pytest.raises(ItemSchemaError, match="binding manifest"):
            restarted.compose_committed_context_plan(
                MAIN_SESSION_ID, thread_id, plan_id="missing-binding-plan", tool_set=empty_tool_set,
            )

    def test_old_experimental_schema_is_rejected_and_preserved(self, saver, sessions_root, empty_tool_set):
        _register(saver, MAIN_SESSION_ID, _contribution("old-schema-source"))
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            connection.execute("UPDATE database_meta SET schema_version = 9")
        restarted = _restarted(sessions_root)
        with pytest.raises(RuntimeError, match="schema-incompatible"):
            restarted.compose_committed_context_plan(
                MAIN_SESSION_ID, thread_id, plan_id="old-schema-plan", tool_set=empty_tool_set,
            )
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (9,)
            assert connection.execute("SELECT contribution_id FROM context_contributions").fetchone() == ("old-schema-source",)


class TestContributionRevisionCAS:
    def test_late_revision_is_rejected_before_registry_and_runtime_change(self, saver):
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        original = _contribution("cas-slot", replacement_policy="replaceable", body="revision a")
        ordinal = _register(saver, MAIN_SESSION_ID, original)
        second = replace(
            original, body="revision b", source_revision="rev-b", content_length=None,
            content_hash=contribution_content_hash("prompt", "revision b"),
        )
        saver.register_context_contribution(
            MAIN_SESSION_ID, thread_id, second,
            expected_source_revision=original.source_revision, request_content="revision b",
        )
        stale = replace(
            original, body="late revision", source_revision="opaque-late", content_length=None,
            content_hash=contribution_content_hash("prompt", "late revision"),
        )
        with pytest.raises(ValueError, match="CAS 冲突"):
            saver.register_context_contribution(
                MAIN_SESSION_ID, thread_id, stale,
                expected_source_revision=original.source_revision, request_content="late revision",
            )
        (row,) = _rows(saver, MAIN_SESSION_ID)
        assert row["source_revision"] == "rev-b" and row["source_ordinal"] == ordinal
        current = saver._composer_for(MAIN_SESSION_ID, thread_id).ledger.contributions["cas-slot"]
        assert current.source_revision == "rev-b" and current.body == "revision b"

    def test_replacement_requires_expected_revision(self, saver):
        original = _contribution("cas-required", replacement_policy="replaceable")
        _register(saver, MAIN_SESSION_ID, original)
        updated = replace(original, source_revision="rev-changed")
        with pytest.raises(ValueError, match="expected source revision CAS"):
            saver.register_context_contribution(
                MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID), updated,
            )
        assert _rows(saver, MAIN_SESSION_ID)[0]["source_revision"] == original.source_revision

    def test_immutable_slot_cannot_change_to_replaceable(self, saver):
        original = _contribution("immutable-slot")
        _register(saver, MAIN_SESSION_ID, original)
        with pytest.raises(ValueError, match="immutable"):
            saver.register_context_contribution(
                MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID),
                replace(original, source_revision="new", replacement_policy="replaceable"),
                expected_source_revision=original.source_revision,
            )
        assert _rows(saver, MAIN_SESSION_ID)[0]["replacement_policy"] == "immutable"

    def test_registry_bound_to_sealed_assembly_is_immutable(self, saver):
        original = _contribution("sealed-slot", replacement_policy="replaceable")
        ordinal = _register(saver, MAIN_SESSION_ID, original)
        thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
        with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
            connection.execute(
                "UPDATE context_contributions SET assembly_id='sealed-assembly', contribution_ordinal=0 WHERE contribution_id=?",
                (original.contribution_id,),
            )
        with pytest.raises(ValueError, match="CAS 冲突.*封存"):
            saver.register_context_contribution(
                MAIN_SESSION_ID, thread_id, replace(original, source_revision="rev-forged"),
                expected_source_revision=original.source_revision,
            )
        (row,) = _rows(saver, MAIN_SESSION_ID)
        assert row["source_revision"] == original.source_revision
        assert row["source_ordinal"] == ordinal


def test_observation_unknown_extension_survives_owner_commit_and_restart(saver, sessions_root):
    thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
    extensions = {"vendor.future/context": {
        "extension_version": "future-19", "protection": "public", "encoding": "jcs:v1",
        "value": {"source_ordinal": 900, "model_call_id": "opaque-call", "nested": {"session_id": MAIN_SESSION_ID}},
    }}
    observation = SourceObservation(
        owner=ContextSourceOwnerKey(MAIN_SESSION_ID, thread_id),
        source_id="skill:extension-contract", source_kind="skill", name="extension-contract",
        revision="extension-rev-1", tracking_mode="tracked", content="冻结来源正文",
        extensions=extensions,
    )
    decision = build_source_lifecycle_decision(observation, decision_kind="base")
    saver.consume_mutation_intent(decision)
    restored = _restarted(sessions_root)._storage.read_items(
        MAIN_SESSION_ID, thread_id, item_ids=(decision.item_id,),
    )[0]
    assert restored.extensions == extensions
    assert restored.source_manifest.source_revision == "extension-rev-1"
    assert restored.source_manifest.visibility == "internal"


def test_source_observation_rejects_unversioned_extension(saver):
    with pytest.raises(ItemSchemaError, match="extension_version"):
        SourceObservation(
            owner=ContextSourceOwnerKey(MAIN_SESSION_ID, saver._storage.main_thread_id(MAIN_SESSION_ID)),
            source_id="skill:invalid-extension", source_kind="skill", name="invalid-extension",
            revision="rev-1", tracking_mode="tracked", content="正文",
            extensions={"vendor.future/context": {"value": {}, "protection": "public"}},
        )


def test_real_sealed_assembly_rejects_registry_cas_without_mutating_owner_state(saver, empty_tool_set):
    thread_id = saver._storage.main_thread_id(MAIN_SESSION_ID)
    accepted = saver.accept_turn(
        MAIN_SESSION_ID, thread_id=thread_id,
        accepted_ingress_id="seal-cas-ingress", acceptance_idempotency_key="seal-cas-accept",
        job_id="seal-cas-job", admission_idempotency_key="seal-cas-admission", payload="验证真实封存",
    )
    original = _contribution("real-sealed-cas", replacement_policy="replaceable")
    ordinal = _register(saver, MAIN_SESSION_ID, original)
    draft = saver.compose_committed_context_plan(
        MAIN_SESSION_ID, thread_id, plan_id="plan-real-sealed-cas", tool_set=empty_tool_set,
    )
    registered = saver.create_context_plan(MAIN_SESSION_ID, replace(draft, plan_creation_idempotency_key="real-sealed-cas:create"))
    snapshot = saver.seal_context_plan(
        MAIN_SESSION_ID, thread_id, registered.draft,
        wire_profile=None, wire_params=None, turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
        provider_version="cas-contract", seal_idempotency_key="real-sealed-cas:seal",
        request_only_content={original.contribution_id: original.body},
    )
    assert snapshot.sealed and snapshot.contributions[0].source_revision == original.source_revision
    before_jsonl = saver._storage.jsonl_path(MAIN_SESSION_ID, thread_id=thread_id).read_bytes()
    with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
        before_sql = tuple(connection.iterdump())
    before_ledger = saver._composer_for(MAIN_SESSION_ID, thread_id).ledger.contributions.copy()
    before_body = saver._request_only_content.copy()
    with pytest.raises(ValueError, match="已封存"):
        saver.register_context_contribution(
            MAIN_SESSION_ID, thread_id, replace(original, source_revision="seal-cas-new"),
            expected_source_revision=original.source_revision, request_content="拒绝的正文",
        )
    with saver._storage._connect(thread_id, "", session_id=MAIN_SESSION_ID) as connection:
        assert tuple(connection.iterdump()) == before_sql
    assert saver._storage.jsonl_path(MAIN_SESSION_ID, thread_id=thread_id).read_bytes() == before_jsonl
    assert saver._composer_for(MAIN_SESSION_ID, thread_id).ledger.contributions == before_ledger
    assert saver._request_only_content == before_body
    row = next(row for row in _rows(saver, MAIN_SESSION_ID) if row["contribution_id"] == original.contribution_id)
    assert row["source_revision"] == original.source_revision and row["source_ordinal"] == ordinal


def test_root_policy_fields_survive_observation_mapping_with_adversarial_extensions():
    owner = ContextSourceOwnerKey(MAIN_SESSION_ID, "thread-contract")
    opaque = {"vendor.future/context": {
        "extension_version": "v1", "protection": "public", "encoding": "jcs:v1",
        "value": {"root_placement": "tail_only", "policy_key": "forged", "wire_role": "assistant"},
    }}
    clean = SourceObservation(
        owner=owner, source_id="instruction:R06", source_kind="filesystem_rules",
        name="Filesystem 规则", revision="rules-v1", tracking_mode="tracked",
        content="文件系统规则", root_placement="root_eligible", policy_key="filesystem_rules",
    )
    adversarial = replace(clean, extensions=opaque)
    clean_decision = build_source_lifecycle_decision(clean, decision_kind="base")
    decision = build_source_lifecycle_decision(adversarial, decision_kind="base")
    assert decision.root_placement == "root_eligible"
    assert decision.policy_key == "filesystem_rules"
    assert replace(decision, extensions={}) == clean_decision
    assert decision.extensions == opaque
    tail = replace(clean, root_placement="tail_only", policy_key=None)
    assert build_source_lifecycle_decision(tail, decision_kind="base").root_placement == "tail_only"
    with pytest.raises(ValueError, match="root_eligible"):
        replace(clean, root_placement="tail_only")
