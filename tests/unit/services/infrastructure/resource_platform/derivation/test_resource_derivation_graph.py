"""ResourceDerivationGraph 的无环注册、派生与发布行为测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event

import pytest

from app.services.infrastructure.resource_platform.derivation import (
    ResourceDerivationGraph,
    SemanticInput,
    SemanticPayload,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.observed_source import (
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    SourceReconciler,
)


def _observed_file_source(
    source_id: str,
    source_path: Path,
    allowed_root: Path,
) -> ObservedSourceHandle:
    return ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id=source_id,
            source_kind="file",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
            entry_identity=source_id,
        ),
        file_path=str(source_path),
        allowed_root=str(allowed_root),
    )


def _metadata_loader(inputs: dict[str, SemanticInput]) -> SemanticPayload:
    content = inputs["src-skill"].content or ""
    description = ""
    for line in content.splitlines():
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
    return SemanticPayload(payload={"facet": "metadata", "description": description})


def _activation_loader(inputs: dict[str, SemanticInput]) -> SemanticPayload:
    content = inputs["src-skill"].content or ""
    body = content.split("---", 2)[-1].strip() if "---" in content else content
    return SemanticPayload(payload={"facet": "activation", "body": body})


def _build_skill_graph(
    reconciler: SourceReconciler,
    registry: ResourceRegistry,
) -> ResourceDerivationGraph:
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="skill:demo:metadata",
            facet="metadata",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
            source_ids=("src-skill",),
        ),
        _metadata_loader,
    )
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="skill:demo:activation",
            facet="activation",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
            source_ids=("src-skill",),
        ),
        _activation_loader,
    )
    return graph


def _reconcile_skill(reconciler: SourceReconciler) -> None:
    reconciler.reconcile("src-skill")


@pytest.fixture
def skill_file_path(tmp_path: Path) -> Path:
    source_path = tmp_path / "SKILL.md"
    source_path.write_text("description: v1\n---\nbody v1\n", encoding="utf-8")
    return source_path


@pytest.fixture
def skill_source_reconciler(skill_file_path: Path) -> SourceReconciler:
    return SourceReconciler(
        handles={
            "src-skill": _observed_file_source(
                "src-skill", skill_file_path, skill_file_path.parent
            )
        }
    )


@pytest.fixture
def skill_resource_graph(
    skill_source_reconciler: SourceReconciler,
    resource_registry: ResourceRegistry,
) -> ResourceDerivationGraph:
    return _build_skill_graph(skill_source_reconciler, resource_registry)


def test_registration_rejects_dependency_cycle(
    resource_registry: ResourceRegistry,
) -> None:
    reconciler = SourceReconciler()
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="a",
            facet="facet",
            display_uri="boxteam://workspace/test/resources/skills/a/SKILL.md",
            dependency_resource_ids=("b",),
        ),
        lambda inputs: SemanticPayload(payload={"a": 1}),
    )
    with pytest.raises(ValueError, match="语义依赖成环"):
        graph.register(
            SemanticResourceDescriptor(
                resource_kind="skills",
                resource_id="b",
                facet="facet",
                display_uri="boxteam://workspace/test/resources/skills/b/SKILL.md",
                dependency_resource_ids=("a",),
            ),
            lambda inputs: SemanticPayload(payload={"b": 1}),
        )


def test_fan_out_facets_advance_independently(
    skill_file_path: Path,
    skill_source_reconciler: SourceReconciler,
    skill_resource_graph: ResourceDerivationGraph,
) -> None:
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    metadata_v1 = skill_resource_graph.publish("skill:demo:metadata")
    activation_v1 = skill_resource_graph.publish("skill:demo:activation")
    assert metadata_v1.revision != activation_v1.revision

    # 只改 description:metadata 推进,activation 不推进。
    skill_file_path.write_text("description: v2\n---\nbody v1\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    metadata_v2 = skill_resource_graph.publish("skill:demo:metadata")
    activation_still = skill_resource_graph.publish("skill:demo:activation")
    assert metadata_v2.revision != metadata_v1.revision
    assert activation_still.revision == activation_v1.revision
    assert activation_still.source_lineage == metadata_v2.source_lineage

    # 只改正文:activation 推进,metadata 不推进。
    skill_file_path.write_text("description: v2\n---\nbody v2\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    metadata_still = skill_resource_graph.publish("skill:demo:metadata")
    activation_v2 = skill_resource_graph.publish("skill:demo:activation")
    assert metadata_still.revision == metadata_v2.revision
    assert metadata_still.source_lineage == activation_v2.source_lineage
    assert activation_v2.revision != activation_v1.revision

    # 只改被忽略的 frontmatter 字段:来源 raw revision 推进,两个 facet 都不推进。
    skill_file_path.write_text(
        "description: v2\nignored: x\n---\nbody v2\n", encoding="utf-8"
    )
    _reconcile_skill(skill_source_reconciler)
    source_revision = skill_source_reconciler.observed_revision("src-skill").revision
    skill_resource_graph.observe_source("src-skill")
    metadata_ignored = skill_resource_graph.publish("skill:demo:metadata")
    activation_ignored = skill_resource_graph.publish("skill:demo:activation")
    assert metadata_ignored.revision == metadata_v2.revision
    assert activation_ignored.revision == activation_v2.revision
    assert metadata_ignored.source_lineage == (("src-skill", source_revision),)
    assert activation_ignored.source_lineage == metadata_ignored.source_lineage
    assert skill_resource_graph.publish("skill:demo:metadata") is metadata_ignored


def test_fan_in_multiple_sources_one_resource(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    first = tmp_path / "inline.jsonc"
    second = tmp_path / "user.jsonc"
    first.write_text("layer-inline\n", encoding="utf-8")
    second.write_text("layer-user\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={
            "src-inline": _observed_file_source("src-inline", first, tmp_path),
            "src-user": _observed_file_source("src-user", second, tmp_path),
        }
    )
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="config:effective",
            facet="config",
            display_uri="boxteam://workspace/test/resources/config/workspace",
            source_ids=("src-inline", "src-user"),
        ),
        lambda inputs: SemanticPayload(
            payload={
                "inline": inputs["src-inline"].content,
                "user": inputs["src-user"].content,
            }
        ),
    )
    reconciler.reconcile("src-inline")
    reconciler.reconcile("src-user")
    reconciler.reconcile("src-user")
    graph.observe_source("src-inline")
    graph.observe_source("src-user")
    snapshot = graph.publish("config:effective")
    identities = {identity for identity, _revision in snapshot.source_lineage}
    assert identities == {"src-inline", "src-user"}
    assert snapshot.payload == {"inline": "layer-inline\n", "user": "layer-user\n"}


def test_dependency_chain_missing_and_mixed_generation(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("agents v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-agents": _observed_file_source("src-agents", source, tmp_path)}
    )
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:content",
            facet="content",
            display_uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
            source_ids=("src-agents",),
        ),
        lambda inputs: SemanticPayload(
            payload={"content": inputs["src-agents"].content}
        ),
    )
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:projected",
            facet="projected",
            display_uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
            dependency_resource_ids=("agents:content",),
        ),
        lambda inputs: SemanticPayload(
            payload={"projected": inputs["agents:content"].content}
        ),
    )

    # 依赖缺失:上游未发布时,下游显式 unavailable 且不伪造空默认值。
    blocked = graph.publish("agents:projected")
    assert blocked.available is False
    assert blocked.error_code == "dependency-missing"
    with pytest.raises(KeyError):
        resource_registry.last_valid_snapshot("agents:projected")

    # 正常链路发布。
    reconciler.reconcile("src-agents")
    graph.observe_source("src-agents")
    content_v1 = graph.publish("agents:content")
    projected_v1 = graph.publish("agents:projected")
    assert projected_v1.available is True
    assert projected_v1.generation >= content_v1.generation

    # 混合代际:来源推进后,不重发布上游就直接发布下游 → 显式拒绝并保旧。
    source.write_text("agents v2\n", encoding="utf-8")
    reconciler.reconcile("src-agents")
    graph.observe_source("src-agents")
    stale = graph.publish("agents:projected")
    assert stale.available is False
    assert stale.error_code == "generation-mismatch"
    assert stale.retained_revision == projected_v1.revision
    assert resource_registry.last_valid_snapshot("agents:projected") is projected_v1

    # 先发布上游,下游即可用新依赖向量发布。
    content_v2 = graph.publish("agents:content")
    projected_v2 = graph.publish("agents:projected")
    assert projected_v2.available is True
    assert projected_v2.revision != projected_v1.revision
    assert dict(projected_v2.source_lineage)["agents:content"] == content_v2.revision


def test_publish_all_follows_topological_order(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("agents v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-agents": _observed_file_source("src-agents", source, tmp_path)}
    )
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:projected",
            facet="projected",
            display_uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
            dependency_resource_ids=("agents:content",),
        ),
        lambda inputs: SemanticPayload(
            payload={"projected": inputs["agents:content"].content}
        ),
    )
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:content",
            facet="content",
            display_uri="boxteam://workspace/test/resources/agent-spec/root/AGENTS.md",
            source_ids=("src-agents",),
        ),
        lambda inputs: SemanticPayload(
            payload={"content": inputs["src-agents"].content}
        ),
    )
    reconciler.reconcile("src-agents")
    graph.observe_source("src-agents")
    published = graph.publish_all()
    assert all(snapshot.available for snapshot in published)
    by_id = {snapshot.resource_id: snapshot for snapshot in published}
    lineage = dict(by_id["agents:projected"].source_lineage)
    assert lineage["agents:content"] == by_id["agents:content"].revision


def test_topological_order_is_cached_until_registration_changes_graph(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("source v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"source": _observed_file_source("source", source, tmp_path)}
    )
    reconciler.reconcile("source")
    graph = ResourceDerivationGraph(
        reconciler=reconciler,
        registry=resource_registry,
    )
    compute_order = graph._compute_topological_order
    computation_count = 0

    def count_computations() -> tuple[str, ...]:
        nonlocal computation_count
        computation_count += 1
        return compute_order()

    monkeypatch.setattr(graph, "_compute_topological_order", count_computations)

    def register_resource(
        resource_id: str,
        *,
        source_ids: tuple[str, ...] = (),
        dependency_resource_ids: tuple[str, ...] = (),
    ) -> None:
        graph.register(
            SemanticResourceDescriptor(
                resource_kind="test",
                resource_id=resource_id,
                facet=resource_id,
                display_uri=None,
                source_ids=source_ids,
                dependency_resource_ids=dependency_resource_ids,
            ),
            lambda inputs: SemanticPayload(
                payload={identity: item.content for identity, item in inputs.items()}
            ),
        )

    register_resource("first", source_ids=("source",))
    register_resource("second", dependency_resource_ids=("first",))
    assert computation_count == 2

    for _ in range(2):
        assert all(snapshot.available for snapshot in graph.publish_all())
    assert graph.publish("first").available
    assert computation_count == 2

    register_resource("third", dependency_resource_ids=("second",))
    assert computation_count == 3
    published = graph.publish_all()
    assert [snapshot.resource_id for snapshot in published] == [
        "first",
        "second",
        "third",
    ]
    assert all(snapshot.available for snapshot in published)
    assert computation_count == 3


def test_source_unavailable_retains_last_valid(
    tmp_path: Path,
    skill_file_path: Path,
    skill_source_reconciler: SourceReconciler,
    resource_registry: ResourceRegistry,
    skill_resource_graph: ResourceDerivationGraph,
) -> None:
    skill_file_path.write_text("description: v1\n---\nbody\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    valid = skill_resource_graph.publish("skill:demo:metadata")

    skill_file_path.write_bytes(b"\xff\xfe")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    unavailable = skill_resource_graph.publish("skill:demo:metadata")
    assert unavailable.available is False
    assert unavailable.error_code == "source-unavailable"
    assert unavailable.retained_revision == valid.revision
    assert unavailable.payload is None
    assert str(skill_file_path) not in repr(unavailable)
    source_failure = skill_source_reconciler.observed_revision("src-skill")
    assert source_failure.error_code == "invalid_utf8"
    assert source_failure.error is not None
    assert (
        "boxteam://workspace/test/resources/skills/demo/SKILL.md"
        in source_failure.error
    )
    assert str(tmp_path) not in source_failure.error
    # 旧 valid 快照仍可审计,不冒充新版本。
    assert resource_registry.last_valid_snapshot("skill:demo:metadata") is valid
    repeated = skill_resource_graph.publish("skill:demo:metadata")
    assert repeated.retained_revision == valid.revision
    assert resource_registry.last_valid_snapshot("skill:demo:metadata") is valid

    skill_file_path.write_text("description: v1\n---\nbody\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    restored = skill_resource_graph.publish("skill:demo:metadata")
    assert restored.available and restored.revision == valid.revision
    assert resource_registry.snapshot("skill:demo:metadata") is restored

    skill_file_path.write_text("description: v2\n---\nbody\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    recovered = skill_resource_graph.publish("skill:demo:metadata")
    assert recovered.available is True
    assert recovered.revision != valid.revision


def test_loader_failure_marks_unavailable_and_keeps_old(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _observed_file_source("src-skill", source, tmp_path)}
    )
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    state = {"fail": False}

    def loader(inputs: dict[str, SemanticInput]) -> SemanticPayload:
        if state["fail"]:
            return SemanticPayload(available=False, error="invalid frontmatter")
        return SemanticPayload(payload={"description": "v1"})

    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="skill:demo:metadata",
            facet="metadata",
            display_uri="boxteam://workspace/test/resources/skills/demo/SKILL.md",
            source_ids=("src-skill",),
        ),
        loader,
    )
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    valid = graph.publish("skill:demo:metadata")
    state["fail"] = True
    graph.observe_source("src-skill")
    failed = graph.publish("skill:demo:metadata")
    assert failed.available is False
    assert failed.error_code == "loader-error"
    assert failed.retained_revision == valid.revision
    assert resource_registry.last_valid_snapshot("skill:demo:metadata") is valid


def test_loader_defect_blocks_the_whole_publication_and_is_not_swallowed(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("v1", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src": _observed_file_source("src", source, tmp_path)}
    )
    reconciler.reconcile("src")
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    state = {"fail": False}
    defect = ValueError(f"loader 程序缺陷: {source}")

    def load(inputs):
        if state["fail"]:
            raise defect
        return SemanticPayload(payload={"content": inputs["src"].content})

    for resource_id in ("first", "second"):
        graph.register(
            SemanticResourceDescriptor(
                resource_id=resource_id,
                resource_kind="skill",
                facet="metadata",
                display_uri=None,
                source_ids=("src",),
            ),
            load,
        )
    valid = graph.publish_all()
    state["fail"] = True
    with pytest.raises(ValueError, match="程序缺陷") as caught:
        graph.publish_all()
    assert caught.value is defect
    for previous in valid:
        current = resource_registry.snapshot(previous.resource_id)
        assert not current.available and current.retained_revision == previous.revision
        assert current.error == "语义派生图求值失败: ValueError"
        assert str(source) not in repr(current)
        assert resource_registry.last_valid_snapshot(previous.resource_id) is previous


def test_graph_publication_hides_a_partially_updated_facet_batch(
    tmp_path: Path,
    resource_registry: ResourceRegistry,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("v1", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src": _observed_file_source("src", source, tmp_path)}
    )
    reconciler.reconcile("src")
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=resource_registry)
    inside_second = Event()
    release = Event()
    capture_started = Event()
    state = {"pause": False}

    def first(inputs):
        return SemanticPayload(payload={"body": inputs["src"].content})

    def second(inputs):
        if state["pause"]:
            inside_second.set()
            assert release.wait(2)
        return first(inputs)

    for resource_id, loader in (("first", first), ("second", second)):
        graph.register(
            SemanticResourceDescriptor(
                resource_id=resource_id,
                resource_kind="skill",
                facet="activation",
                display_uri=None,
                source_ids=("src",),
            ),
            loader,
        )
    graph.publish_all()
    source.write_text("v2", encoding="utf-8")
    reconciler.reconcile("src")
    state["pause"] = True

    def capture():
        capture_started.set()
        return resource_registry.capture()

    with ThreadPoolExecutor(max_workers=2) as workers:
        publishing = workers.submit(graph.publish_all)
        try:
            assert inside_second.wait(1)
            captured = workers.submit(capture)
            assert capture_started.wait(1)
            with pytest.raises(FutureTimeoutError):
                captured.result(timeout=0.05)
        finally:
            release.set()
        assert len(publishing.result(timeout=1)) == 2
        snapshots = captured.result(timeout=1).snapshots
    assert len(snapshots) == 2
    assert all(snapshot.payload == {"body": "v2"} for snapshot in snapshots)


def test_cas_publish_is_idempotent(
    skill_source_reconciler: SourceReconciler,
    resource_registry: ResourceRegistry,
    skill_resource_graph: ResourceDerivationGraph,
) -> None:
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    first = skill_resource_graph.publish("skill:demo:metadata")
    second = skill_resource_graph.publish("skill:demo:metadata")
    assert second is first
    # 直接 CAS:相同 revision 幂等返回已存快照。
    assert resource_registry.publish(first) is first


def test_restart_rebuilds_identity_stable(
    skill_file_path: Path,
    skill_source_reconciler: SourceReconciler,
    skill_resource_graph: ResourceDerivationGraph,
) -> None:
    skill_file_path.write_text("description: v1\n---\nbody\n", encoding="utf-8")
    _reconcile_skill(skill_source_reconciler)
    skill_resource_graph.observe_source("src-skill")
    before = {
        "skill:demo:metadata": skill_resource_graph.publish("skill:demo:metadata"),
        "skill:demo:activation": skill_resource_graph.publish("skill:demo:activation"),
    }

    # 模拟进程重启:全新 reconciler/graph/registry 实例,owner 重新登记 handle。
    rebuilt_reconciler = SourceReconciler(
        handles={
            "src-skill": _observed_file_source(
                "src-skill", skill_file_path, skill_file_path.parent
            )
        }
    )
    rebuilt_registry = ResourceRegistry()
    rebuilt_graph = _build_skill_graph(rebuilt_reconciler, rebuilt_registry)
    rebuilt_reconciler.reconcile("src-skill")
    rebuilt_graph.observe_source("src-skill")
    after = {
        "skill:demo:metadata": rebuilt_graph.publish("skill:demo:metadata"),
        "skill:demo:activation": rebuilt_graph.publish("skill:demo:activation"),
    }
    for resource_id, snapshot in before.items():
        assert after[resource_id].revision == snapshot.revision
        assert after[resource_id].source_lineage == snapshot.source_lineage
        assert after[resource_id].resource_id == snapshot.resource_id
        assert after[resource_id].display_uri == snapshot.display_uri
    # registry 快照不含任何物理路径/locator。
    for snapshot in after.values():
        assert skill_file_path.parent.as_posix() not in repr(snapshot)
        assert str(skill_file_path) not in repr(snapshot)
        assert (
            snapshot.display_uri
            == "boxteam://workspace/test/resources/skills/demo/SKILL.md"
        )
