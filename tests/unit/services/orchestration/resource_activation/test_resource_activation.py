"""ResourceActivationCoordinator 的 Execution/ModelCall 冻结合同测试（9.3）。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace

import pytest

from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_owner import (
    ContextOwnerMixin,
)
from app.services.orchestration.resource_activation import (
    ResourceActivationCoordinator,
    ResourceActivationError,
)
from app.services.orchestration.resource_activation.contracts import (
    require_current_execution_resource_activation,
    reset_current_execution_resource_activation,
    resource_activation_policy_from_config,
    set_current_execution_resource_activation,
)

SESSION_ID = "ses_019c5facabc37e5c89a5c9584ed39862"


class _FakeSaver:
    context_owner_namespace = staticmethod(ContextOwnerMixin.context_owner_namespace)

    def __init__(self) -> None:
        self.snapshots: list[ResourceActivationSnapshotRef] = []

    def frozen_resource_activation_for_admission(self, admission, *, checkpoint_ns=""):
        return next((
            snapshot for snapshot in self.snapshots
            if snapshot.snapshot_kind == "execution"
            and snapshot.owner_session_id == admission.session_id
            and snapshot.owner_thread_id == admission.thread_id
            and (
                snapshot.execution_id == admission.execution_id
                or (admission.turn_id is not None and snapshot.turn_id == admission.turn_id)
            )
        ), None)

    def frozen_resource_activation_for_model_call(
        self, session_id, *, thread_id, model_call_id, checkpoint_ns=""
    ):
        return next((
            snapshot for snapshot in self.snapshots
            if snapshot.owner_session_id == session_id and snapshot.owner_thread_id == thread_id
            and snapshot.snapshot_kind == "model_call" and snapshot.model_call_id == model_call_id
        ), None)

    async def save_resource_activation_snapshot(
        self, snapshot: ResourceActivationSnapshotRef, *, checkpoint_ns: str = ""
    ) -> None:
        self.snapshots.append(snapshot)


class _FakeBodyStore:
    """只接受内存 payload；记录写入顺序，不做任何源读取。"""

    def __init__(self) -> None:
        self.written: list[tuple[str, int]] = []
        self.payloads: list[object] = []

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        resource_id: str,
        activation_ordinal: int,
        payload: object,
        checkpoint_ns: str,
    ) -> DetailRef:
        assert owner_session_id
        assert activation_snapshot_id
        assert checkpoint_ns == "" or isinstance(checkpoint_ns, str)
        assert payload is not None
        self.written.append((resource_id, activation_ordinal))
        self.payloads.append(payload)
        return DetailRef(
            owner_session_id,
            owner_thread_id,
            activation_snapshot_id,
            f"body-{resource_id}-{activation_ordinal}",
        )


def _snapshot(
    resource_id: str,
    resource_kind: str,
    payload: object,
    *,
    generation: int,
    available: bool = True,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource_id=resource_id,
        resource_kind=resource_kind,
        facet="activation",
        display_uri=None if resource_kind == "mcp_tool_catalog" else "boxteam://workspace/test/resources/skills/demo/SKILL.md",
        revision=f"rev-{resource_id}",
        payload=payload if available else None,
        source_lineage=(("source-1", f"source-rev-{resource_id}"),),
        generation=generation,
        available=available,
        error=None if available else "source unavailable",
        error_code=None if available else "source-unavailable",
        retained_revision=None if available else "rev-retained",
    )


def _registry() -> ResourceRegistry:
    registry = ResourceRegistry()
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="skill:demo",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
        )
    )
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri=None,
        )
    )
    registry.publish(_snapshot("skill:demo", "skills", {"skill": 1}, generation=2))
    registry.publish(
        _snapshot("mcp:catalog", "mcp_tool_catalog", {"mcp": 1}, generation=3)
    )
    return registry


def _policy(*, default: str = "turn", overrides: dict[str, str] | None = None):
    if overrides is None:
        overrides = {"mcp_tool_catalog": "model_call"}
    return resource_activation_policy_from_config(
        {
            "context": {
                "resource_activation": {
                    "default_boundary": default,
                    "overrides": overrides,
                }
            }
        }
    )



def _admission(turn_id: str | None, *, execution_id: str | None = None):
    identity = execution_id or f"execution-{turn_id or 'internal'}"
    return ExecutionAdmission(
        session_id=SESSION_ID, thread_id="main", execution_id=identity,
        job_id=f"job-{identity}", admission_idempotency_key=f"admission-{identity}",
        execution_origin="internal" if turn_id is None else "user_turn", turn_id=turn_id,
    )


def _coordinator(registry: ResourceRegistry, saver: _FakeSaver):
    return ResourceActivationCoordinator(
        registry=registry,
        saver=saver,  # type: ignore[arg-type]
        body_store=_FakeBodyStore(),  # type: ignore[arg-type]
        wait_seconds=0.05,
        poll_seconds=0.005,
    )


@pytest.mark.asyncio
async def test_freeze_execution_and_model_call_reuses_parent_bytes() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    policy = _policy()
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=policy,
    )

    assert turn.snapshot_kind == "execution"
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]
    assert turn.registry_generation == 3
    assert turn.activation_policy_hash == policy.hash
    assert len(saver.snapshots) == 1

    first_call = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    ))
    assert isinstance(first_call, ResourceActivationSnapshotRef)
    assert first_call.snapshot_kind == "model_call"
    assert first_call.bindings[: len(turn.bindings)] == turn.bindings
    assert first_call.bindings[-1].effective_boundary == "model_call"
    assert first_call.parent is turn
    assert len(saver.snapshots) == 2

    # Registry 后续推进只影响未来 snapshot，已冻结对象不变。
    registry.publish(
        ResourceSnapshot(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri=None,
            revision="rev-mcp:catalog:2",
            payload={"mcp": 2},
            source_lineage=(("source-1", "source-rev-mcp:catalog-2"),),
            generation=4,
        )
    )
    second_call = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=turn, model_call_id="call-2", policy=policy
    ))
    assert isinstance(second_call, ResourceActivationSnapshotRef)
    assert second_call.bindings[: len(turn.bindings)] == turn.bindings
    assert second_call.bindings[-1].revision == "rev-mcp:catalog:2"
    assert second_call.bindings[-1].content_hash != first_call.bindings[-1].content_hash
    assert len(saver.snapshots) == 3
    # model-call snapshot 只追加，绝不重复 parent 的 turn-bound binding。
    for call in (first_call, second_call):
        resource_ids = [binding.resource_id for binding in call.bindings]
        assert len(resource_ids) == len(set(resource_ids))
        assert resource_ids[: len(turn.bindings)] == [
            binding.resource_id for binding in turn.bindings
        ]


@pytest.mark.asyncio
async def test_snapshot_ids_are_bounded_and_unambiguous_for_native_storage() -> None:
    saver = _FakeSaver()
    coordinator = _coordinator(_registry(), saver)
    policy = _policy()
    roots = [await coordinator.freeze_execution(
        admission=_admission(f"turn-{number}", execution_id=execution), policy=policy,
    ) for number, execution in enumerate(("execution", "execution:call", "长身份:" * 100))]
    calls = [await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=root, model_call_id=call,
    )) for root, call in zip(roots, ("call:tail", "tail", "模型调用:" * 100), strict=True)]
    ids = [snapshot.activation_snapshot_id for snapshot in (*roots, *calls)]
    assert len(set(ids)) == 6
    assert all(re.fullmatch(r"activation-[0-9a-f]{64}", identity) for identity in ids)
    repeated = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=roots[0], model_call_id="call:tail",
    ))
    assert repeated is calls[0]
    assert len(saver.snapshots) == 6


@pytest.mark.asyncio
async def test_resume_uses_recorded_snapshot_identity_without_refreezing() -> None:
    saver = _FakeSaver()
    coordinator = _coordinator(_registry(), saver)
    root = await coordinator.freeze_execution(admission=_admission("historical"), policy=_policy())
    call = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=root, model_call_id="historical-call",
    ))
    historical = replace(call, activation_snapshot_id="historical:opaque:model-call")
    saver.snapshots[-1] = historical
    # 冷恢复没有 live Registry；若按新命名重算旧 ID 就会尝试重新冻结并失败。
    restarted = _coordinator(ResourceRegistry(), saver)
    restored = await restarted.commit_model_call(await restarted.capture_model_call(
        parent=root, model_call_id="historical-call",
    ))
    assert restored is historical
    assert restored.to_dict() == historical.to_dict()
    assert len(saver.snapshots) == 2


@pytest.mark.asyncio
async def test_model_call_identity_cannot_reuse_another_execution_snapshot() -> None:
    saver = _FakeSaver()
    coordinator = _coordinator(_registry(), saver)
    first = await coordinator.freeze_execution(admission=_admission("first"), policy=_policy())
    second = await coordinator.freeze_execution(admission=_admission("second"), policy=_policy())
    await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=first, model_call_id="shared-call",
    ))
    with pytest.raises(ResourceActivationError) as error:
        await coordinator.capture_model_call(parent=second, model_call_id="shared-call")
    assert error.value.code == "resource-activation-parent-invalid"
    assert len(saver.snapshots) == 3


@pytest.mark.asyncio
async def test_no_model_call_kind_reuses_turn_snapshot_without_saving() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    policy = _policy(overrides={})
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=policy,
    )
    prepared = await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    ))
    assert prepared is turn


@pytest.mark.asyncio
async def test_model_call_policy_without_required_published_snapshot_fails_closed() -> None:
    """已登记的 required kind 缺 published snapshot 时不得复用 parent 掩盖缺失。"""

    registry = ResourceRegistry()
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="skill:demo",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
        )
    )
    # mcp_tool_catalog 已登记但尚未发布任何可用 snapshot。
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri=None,
        )
    )
    registry.publish(_snapshot("skill:demo", "skills", {"skill": 1}, generation=2))
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    policy = _policy()
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=policy,
    )
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]

    with pytest.raises(ResourceActivationError) as error:
        await coordinator.commit_model_call(await coordinator.capture_model_call(parent=turn, model_call_id="call-1", policy=policy))
    assert error.value.code == "resource-unavailable"
    assert len(saver.snapshots) == 1


@pytest.mark.asyncio
async def test_turn_policy_drift_is_rejected() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=_policy(),
    )
    drifted = _policy(default="model_call", overrides={})
    with pytest.raises(ResourceActivationError) as error:
        await coordinator.commit_model_call(await coordinator.capture_model_call(
            parent=turn, model_call_id="call-1", policy=drifted
        ))
    assert error.value.code == "resource-activation-policy-drift"


@pytest.mark.asyncio
async def test_unknown_override_kind_fails_closed() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    with pytest.raises(ResourceActivationError, match="未登记 resource kind"):
        await coordinator.freeze_execution(
            admission=_admission("turn-1"),
            policy=_policy(overrides={"memory": "model_call"}),
        )


@pytest.mark.asyncio
async def test_unavailable_required_resource_fails_closed_after_bounded_wait() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    registry.publish(
        _snapshot("skill:demo", "skills", None, generation=5, available=False)
    )
    with pytest.raises(ResourceActivationError) as error:
        await coordinator.freeze_execution(
            admission=_admission("turn-2"),
            policy=_policy(),
        )
    assert error.value.code == "resource-unavailable"
    assert saver.snapshots == []


@pytest.mark.asyncio
async def test_unavailable_required_resource_does_bounded_wait_before_failing() -> None:
    """不可用 required resource 必须先有界等待，不能立即或无限重试。"""

    registry = _registry()
    registry.publish(
        _snapshot("skill:demo", "skills", None, generation=5, available=False)
    )
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        if len(sleeps) > 100:
            raise AssertionError("bounded wait 未收敛：等待次数无上界")
        sleeps.append(seconds)
        # 真实推进事件循环时间，使 time.monotonic() 预算真实耗尽。
        await asyncio.sleep(seconds)

    coordinator = ResourceActivationCoordinator(
        registry=registry,
        saver=_FakeSaver(),  # type: ignore[arg-type]
        body_store=_FakeBodyStore(),  # type: ignore[arg-type]
        wait_seconds=0.05,
        poll_seconds=0.005,
        sleep=_sleep,
    )
    with pytest.raises(ResourceActivationError) as error:
        await coordinator.freeze_execution(
            admission=_admission("turn-wait"),
            policy=_policy(),
        )
    assert error.value.code == "resource-unavailable"
    assert sleeps, "required resource 不可用时必须经过有界等待"
    assert all(seconds == 0.005 for seconds in sleeps)


@pytest.mark.asyncio
async def test_bounded_wait_recovers_when_resource_becomes_available() -> None:
    registry = ResourceRegistry()
    saver = _FakeSaver()
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            # 第二次轮询前 registry 完成发布：等待应当成功收敛。
            registry.publish(
                _snapshot("skill:demo", "skills", {"skill": 1}, generation=2)
            )

    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="skill:demo",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
        )
    )
    coordinator = ResourceActivationCoordinator(
        registry=registry,
        saver=saver,  # type: ignore[arg-type]
        body_store=_FakeBodyStore(),  # type: ignore[arg-type]
        wait_seconds=1.0,
        poll_seconds=0.01,
        sleep=_sleep,
    )
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=_policy(overrides={}),
    )
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_seal_path_creates_no_source_io() -> None:
    """冻结只消费内存 snapshot + body port；不 stat/scan/read/HTTP fetch。"""

    registry = _registry()
    body_store = _FakeBodyStore()
    saver = _FakeSaver()
    coordinator = ResourceActivationCoordinator(
        registry=registry,
        saver=saver,  # type: ignore[arg-type]
        body_store=body_store,  # type: ignore[arg-type]
        wait_seconds=0.05,
        poll_seconds=0.005,
    )
    policy = _policy()
    turn = await coordinator.freeze_execution(
        admission=_admission("turn-1"),
        policy=policy,
    )
    await coordinator.commit_model_call(await coordinator.capture_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    ))
    # 每个新 binding 恰有一次正文写入，顺序与 activation_ordinal 一致。
    assert body_store.written == [("skill:demo", 0), ("mcp:catalog", 1)]
    assert all(binding.detail_ref is not None for binding in turn.bindings)
    assert all(binding.snapshot_ref is None for binding in turn.bindings)


@pytest.mark.asyncio
async def test_same_turn_retry_reuses_complete_policy_and_binding_bytes() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    first = await coordinator.freeze_execution(
        admission=_admission("turn-retry", execution_id="execution-first"),
        policy=_policy(overrides={"mcp_tool_catalog": "model_call"}),
    )
    registry.publish(_snapshot("skill:demo", "skills", {"skill": 2}, generation=9))
    retried = await coordinator.freeze_execution(
        admission=_admission("turn-retry", execution_id="execution-retry"),
        policy=_policy(default="model_call", overrides={}),
    )
    assert retried.execution_id == "execution-retry"
    assert retried.turn_id == first.turn_id
    assert retried.activation_snapshot_id != first.activation_snapshot_id
    assert retried.activation_policy.to_dict() == first.activation_policy.to_dict()
    assert retried.bindings == first.bindings
    assert retried.bindings_hash == first.bindings_hash
    assert retried.activation_provenance_hash == first.activation_provenance_hash
    call = await coordinator.commit_model_call(await coordinator.capture_model_call(parent=retried, model_call_id="retry-call"))
    assert call.parent_execution_snapshot_id == retried.activation_snapshot_id
    assert call.execution_id == retried.execution_id


@pytest.mark.asyncio
async def test_turnless_internal_uses_own_execution_and_complete_default_model_call_policy() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    root = await coordinator.freeze_execution(
        admission=_admission(None), policy=_policy(default="model_call", overrides={}),
    )
    assert root.turn_id is None and root.execution_id == "execution-internal"
    assert root.snapshot_kind == "execution" and root.bindings == ()
    call = await coordinator.commit_model_call(await coordinator.capture_model_call(parent=root, model_call_id="internal-call"))
    assert call.turn_id is None and call.execution_id == root.execution_id
    assert call.parent_execution_snapshot_id == root.activation_snapshot_id
    assert len(call.bindings) == 2


@pytest.mark.asyncio
async def test_task_local_scope_passes_only_matching_frozen_root_and_resets() -> None:
    admission = _admission("turn-scope", execution_id="execution-scope")
    root = await _coordinator(_registry(), _FakeSaver()).freeze_execution(
        admission=admission, policy=_policy(),
    )
    with pytest.raises(RuntimeError, match="尚未冻结"):
        require_current_execution_resource_activation(admission)
    token = set_current_execution_resource_activation(root, admission=admission)
    try:
        assert require_current_execution_resource_activation(admission) is root
        with pytest.raises(ValueError, match="admission 不一致"):
            require_current_execution_resource_activation(
                _admission("turn-other", execution_id="execution-other"),
            )
        call = await _coordinator(_registry(), _FakeSaver()).commit_model_call(await _coordinator(_registry(), _FakeSaver()).capture_model_call(
            parent=root, model_call_id="scope-model-call",
        ))
        with pytest.raises(ValueError, match="admission 不一致"):
            set_current_execution_resource_activation(call, admission=admission)
    finally:
        reset_current_execution_resource_activation(token)
    with pytest.raises(RuntimeError, match="尚未冻结"):
        require_current_execution_resource_activation(admission)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member_changes",
    [
        {"member_id": "mcp:server:other"},
        {"member_kind": "custom", "server_id": None},
        {"schema_hash": "sha256:" + "b" * 64},
        {"server_id": "server-other"},
        {"revision": "catalog-revision-2"},
        None,
    ],
)
async def test_registry_to_coordinator_freezes_typed_members_outside_generic_hashes(
    member_changes: dict[str, object] | None,
) -> None:
    """只验通用冻结链路；真实 MCP producer 的目录代际验收由组合测试负责。"""
    registry = ResourceRegistry()
    payload = {"guidance": "固定目录指引"}
    member = ResourceMemberBinding(
        member_id="mcp:server:demo", member_kind="mcp",
        schema_hash="sha256:" + "a" * 64, server_id="server-demo",
        revision="catalog-revision-1",
    )
    published = registry.publish(ResourceSnapshot(
        resource_id="mcp:catalog", resource_kind="mcp_tool_catalog",
        facet="guidance", display_uri=None,
        revision=sha256_jcs(payload), payload=payload,
        source_lineage=(("mcp:catalog-source", "catalog-revision-1"),),
        generation=1, members=(member,),
    ))
    body_store = _FakeBodyStore()
    coordinator = ResourceActivationCoordinator(
        registry=registry, saver=_FakeSaver(), body_store=body_store,
        wait_seconds=0.05, poll_seconds=0.005,
    )
    first = await coordinator.freeze_execution(
        admission=_admission("turn-first"), policy=_policy(overrides={}),
    )
    members = () if member_changes is None else (replace(member, **member_changes),)
    registry.publish(replace(published, members=members))
    second = await coordinator.freeze_execution(
        admission=_admission("turn-second"), policy=_policy(overrides={}),
    )
    first_binding, second_binding = first.bindings[0], second.bindings[0]
    assert first_binding.members == (member,)
    assert second_binding.members == members
    assert first_binding.members != second_binding.members
    assert second_binding.revision == first_binding.revision == sha256_jcs(payload)
    assert second_binding.content_hash == first_binding.content_hash == sha256_jcs(payload)
    assert second_binding.content_length == first_binding.content_length == len(
        canonical_json_bytes(payload)
    )
    assert second.bindings_hash == first.bindings_hash
    assert second.activation_provenance_hash == first.activation_provenance_hash
    assert body_store.payloads == [payload, payload]
    assert ResourceActivationSnapshotRef.from_dict(second.to_dict()) == second
    assert ResourceActivationSnapshotRef.from_dict(first.to_dict()) == first


@pytest.mark.asyncio
async def test_coordinator_freezes_the_capture_it_validated_even_if_registry_advances() -> None:
    registry = _registry()
    published = registry.snapshot("skill:demo")
    captured = registry.capture()

    class AdvancingRegistry(ResourceRegistry):
        def __init__(self) -> None:
            super().__init__()
            for descriptor in captured.descriptors:
                self.register_descriptor(descriptor)
            for snapshot in captured.snapshots:
                self.publish(snapshot)

        def capture(self):
            value = super().capture()
            self.publish(replace(
                published, payload=None, available=False, generation=10,
                error="发布在捕获完成后变为不可用", error_code="source-unavailable",
                retained_revision=published.revision,
            ))
            return value

        def snapshots(self):
            raise AssertionError("Coordinator 不得再次分离读取 snapshots")

        def descriptors(self):
            raise AssertionError("Coordinator 不得再次分离读取 descriptors")

        def resource_kinds(self):
            raise AssertionError("Coordinator 不得再次分离读取 resource kinds")

    advancing = AdvancingRegistry()
    result = await _coordinator(advancing, _FakeSaver()).freeze_execution(
        admission=_admission("turn-capture"), policy=_policy(),
    )
    assert len(result.bindings) == 1
    assert result.bindings[0].revision == published.revision
    assert result.bindings[0].content_hash == sha256_jcs(published.payload)
    assert result.registry_generation == 3
    assert advancing.snapshot("skill:demo").available is False
