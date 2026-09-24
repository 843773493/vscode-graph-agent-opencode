"""ResourceActivationCoordinator 的 Turn/ModelCall 冻结合同测试（9.3）。"""

from __future__ import annotations

import asyncio

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.resource_activation import (
    ResourceActivationCoordinator,
    ResourceActivationError,
    ResourceActivationPolicySnapshot,
)

SESSION_ID = "ses_9d2f0e5a1c3b4a6f8e7d6c5b4a392817"


class _FakeSaver:
    def __init__(self) -> None:
        self.snapshots: list[ResourceActivationSnapshotRef] = []

    async def save_resource_activation_snapshot(
        self, snapshot: ResourceActivationSnapshotRef
    ) -> None:
        self.snapshots.append(snapshot)


class _FakeBodyStore:
    """只接受内存 payload；记录写入顺序，不做任何源读取。"""

    def __init__(self) -> None:
        self.written: list[tuple[str, int]] = []

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
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
        return DetailRef(
            owner_session_id,
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
        display_uri=f"boxteam://workspace/test/{resource_kind}",
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
            display_uri="boxteam://workspace/test/skills",
        )
    )
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri="boxteam://workspace/test/mcp_tool_catalog",
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
    return ResourceActivationPolicySnapshot.from_config(
        {
            "context": {
                "resource_activation": {
                    "default_boundary": default,
                    "overrides": overrides,
                }
            }
        }
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
async def test_freeze_turn_and_model_call_reuses_parent_bytes() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    policy = _policy()
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        policy=policy,
    )

    assert turn.snapshot_kind == "turn"
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]
    assert turn.registry_generation == 3
    assert turn.activation_policy_hash == policy.hash
    assert len(saver.snapshots) == 1

    first_call = await coordinator.prepare_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    )
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
            display_uri="boxteam://workspace/test/mcp_tool_catalog",
            revision="rev-mcp:catalog:2",
            payload={"mcp": 2},
            source_lineage=(("source-1", "source-rev-mcp:catalog-2"),),
            generation=4,
        )
    )
    second_call = await coordinator.prepare_model_call(
        parent=turn, model_call_id="call-2", policy=policy
    )
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
async def test_no_model_call_kind_reuses_turn_snapshot_without_saving() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    policy = _policy(overrides={})
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        policy=policy,
    )
    prepared = await coordinator.prepare_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    )
    assert prepared is turn


@pytest.mark.asyncio
async def test_model_call_policy_without_published_kind_reuses_parent() -> None:
    """policy 声明 model_call boundary 但当前无该 kind 资源时复用 parent，不漂移。"""

    registry = ResourceRegistry()
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="skill:demo",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/skills",
        )
    )
    # mcp_tool_catalog 已登记但尚未发布任何可用 snapshot。
    registry.register_descriptor(
        SemanticResourceDescriptor(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri="boxteam://workspace/test/mcp_tool_catalog",
        )
    )
    registry.publish(_snapshot("skill:demo", "skills", {"skill": 1}, generation=2))
    saver = _FakeSaver()
    coordinator = _coordinator(registry, saver)
    policy = _policy()
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        policy=policy,
    )
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]

    prepared = await coordinator.prepare_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    )
    assert prepared is turn
    assert len(saver.snapshots) == 1


@pytest.mark.asyncio
async def test_turn_policy_drift_is_rejected() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        policy=_policy(),
    )
    drifted = _policy(default="model_call", overrides={})
    with pytest.raises(ResourceActivationError) as error:
        await coordinator.prepare_model_call(
            parent=turn, model_call_id="call-1", policy=drifted
        )
    assert error.value.code == "resource-activation-policy-drift"


@pytest.mark.asyncio
async def test_unknown_override_kind_fails_closed() -> None:
    coordinator = _coordinator(_registry(), _FakeSaver())
    with pytest.raises(ResourceActivationError, match="未登记 resource kind"):
        await coordinator.freeze_turn(
            owner_session_id=SESSION_ID,
            owner_thread_id="main",
            turn_id="turn-1",
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
        await coordinator.freeze_turn(
            owner_session_id=SESSION_ID,
            owner_thread_id="main",
            turn_id="turn-2",
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
        await coordinator.freeze_turn(
            owner_session_id=SESSION_ID,
            owner_thread_id="main",
            turn_id="turn-wait",
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
            display_uri="boxteam://workspace/test/skills",
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
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
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
    turn = await coordinator.freeze_turn(
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        policy=policy,
    )
    await coordinator.prepare_model_call(
        parent=turn, model_call_id="call-1", policy=policy
    )
    # 每个新 binding 恰有一次正文写入，顺序与 activation_ordinal 一致。
    assert body_store.written == [("skill:demo", 0), ("mcp:catalog", 1)]
    assert all(binding.detail_ref is not None for binding in turn.bindings)
    assert all(binding.snapshot_ref is None for binding in turn.bindings)
