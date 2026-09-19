"""ResourceActivationCoordinator 的 Turn/ModelCall 冻结合同测试。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.resource_activation import (
    ModelCallResourceSnapshot,
    ResourceActivationCoordinator,
    ResourceActivationError,
    ResourceActivationPolicySnapshot,
    TurnResourceSnapshot,
)


class _FakeSaver:
    def __init__(self) -> None:
        self.snapshots: list[TurnResourceSnapshot | ModelCallResourceSnapshot] = []

    async def save_resource_activation_snapshot(
        self,
        snapshot: TurnResourceSnapshot | ModelCallResourceSnapshot,
    ) -> None:
        self.snapshots.append(snapshot)


def _snapshot(
    resource_id: str,
    resource_kind: str,
    payload: object,
    *,
    generation: int,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        resource_id=resource_id,
        resource_kind=resource_kind,
        facet="activation",
        display_uri=f"boxteam://workspace/test/{resource_kind}",
        revision=f"rev-{resource_id}",
        payload=payload,
        source_lineage=(("source-1", f"source-rev-{resource_id}"),),
        generation=generation,
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
    registry.publish(_snapshot("mcp:catalog", "mcp_tool_catalog", {"mcp": 1}, generation=3))
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


@pytest.mark.asyncio
async def test_freeze_turn_and_model_call_reuses_parent_bytes() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = ResourceActivationCoordinator(
        registry=registry,
        saver=saver,  # type: ignore[arg-type]
    )
    policy = _policy()
    turn = await coordinator.freeze_turn(
        owner_session_id="session-1",
        owner_thread_id="thread-1",
        turn_id="turn-1",
        policy=policy,
    )

    assert turn.snapshot_kind == "turn"
    assert [binding.resource_kind for binding in turn.bindings] == ["skills"]
    assert turn.registry_generation == 3
    assert len(saver.snapshots) == 1

    first_call = await coordinator.prepare_model_call(
        parent=turn,
        model_call_id="call-1",
    )
    assert isinstance(first_call, ModelCallResourceSnapshot)
    assert first_call.bindings[: len(turn.bindings)] == turn.bindings
    assert first_call.bindings[-1].effective_boundary == "model_call"
    assert first_call.registry_generation == 3
    assert len(saver.snapshots) == 2

    # Registry 后续推进只影响未来 snapshot，已冻结对象不变。
    registry.publish(
        ResourceSnapshot(
            resource_id="mcp:catalog",
            resource_kind="mcp_tool_catalog",
            facet="activation",
            display_uri="boxteam://workspace/test/mcp_tool_catalog",
            revision="rev-mcp-catalog-2",
            payload={"mcp": 2},
            source_lineage=(("source-1", "source-rev-mcp-catalog-2"),),
            generation=4,
        )
    )
    second_call = await coordinator.prepare_model_call(
        parent=turn,
        model_call_id="call-2",
    )
    assert isinstance(second_call, ModelCallResourceSnapshot)
    assert first_call.bindings[: len(turn.bindings)] == turn.bindings
    assert second_call.bindings[-1].semantic_revision == "rev-mcp-catalog-2"
    assert second_call.bindings[-1].content_hash != first_call.bindings[-1].content_hash
    assert len(saver.snapshots) == 3


@pytest.mark.asyncio
async def test_no_model_call_kind_reuses_turn_snapshot_without_saving() -> None:
    coordinator = ResourceActivationCoordinator(
        registry=_registry(),
        saver=_FakeSaver(),  # type: ignore[arg-type]
    )
    policy = _policy(overrides={})
    turn = await coordinator.freeze_turn(
        owner_session_id="session-1",
        owner_thread_id="thread-1",
        turn_id="turn-1",
        policy=policy,
    )
    prepared = await coordinator.prepare_model_call(parent=turn, model_call_id="call-1")
    assert prepared is turn


@pytest.mark.asyncio
async def test_unknown_override_kind_and_unavailable_registry_fail_closed() -> None:
    registry = _registry()
    saver = _FakeSaver()
    coordinator = ResourceActivationCoordinator(registry=registry, saver=saver)  # type: ignore[arg-type]
    policy = _policy(overrides={"memory": "model_call"})
    with pytest.raises(ResourceActivationError, match="未登记 resource kind"):
        await coordinator.freeze_turn(
            owner_session_id="session-1",
            owner_thread_id="thread-1",
            turn_id="turn-1",
            policy=policy,
        )

    registry.publish(
        ResourceSnapshot(
            resource_id="skill:demo",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/skills",
            revision="rev-old",
            payload=None,
            source_lineage=(("source-1", "source-rev"),),
            generation=5,
            available=False,
            error="source unavailable",
            error_code="source-unavailable",
            retained_revision="rev-old",
        )
    )
    with pytest.raises(ResourceActivationError, match="resource-unavailable"):
        await coordinator.freeze_turn(
            owner_session_id="session-1",
            owner_thread_id="thread-1",
            turn_id="turn-2",
            policy=_policy(),
        )
    assert saver.snapshots == []
