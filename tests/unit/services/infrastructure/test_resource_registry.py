"""OpenSpec 3.10 ResourceRegistry / ResourceDerivationGraph 定向合同测试。

覆盖无环校验、fan-out/fan-in、facet 独立语义 revision、依赖缺失、
混合代际(依赖快照落后于来源)拒绝、来源 unavailable 保旧、loader 失败、
CAS 幂等、进程重启 identity 稳定,以及禁止动态 import 的静态审计。
"""

from __future__ import annotations

import ast
from pathlib import Path

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

PROJECT_ROOT = Path.cwd()


def _skill_source(source_id: str, path: Path, root: Path) -> ObservedSourceHandle:
    return ObservedSourceHandle(
        descriptor=ObservedSourceDescriptor(
            source_id=source_id,
            source_kind="file",
            display_uri="boxteam://workspace/skill/demo",
            entry_identity=source_id,
        ),
        file_path=str(path),
        allowed_root=str(root),
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
    reconciler: SourceReconciler, registry: ResourceRegistry,
) -> ResourceDerivationGraph:
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="skill:demo:metadata",
            facet="metadata",
            display_uri="boxteam://workspace/skill/demo",
            source_ids=("src-skill",),
        ),
        _metadata_loader,
    )
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="skill:demo:activation",
            facet="activation",
            display_uri="boxteam://workspace/skill/demo",
            source_ids=("src-skill",),
        ),
        _activation_loader,
    )
    return graph


def _reconcile_skill(reconciler: SourceReconciler) -> None:
    reconciler.reconcile("src-skill")


def test_registration_rejects_dependency_cycle(tmp_path: Path) -> None:
    reconciler = SourceReconciler()
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=ResourceRegistry())
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="a",
            facet="facet",
            display_uri="boxteam://workspace/a",
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
                display_uri="boxteam://workspace/b",
                dependency_resource_ids=("a",),
            ),
            lambda inputs: SemanticPayload(payload={"b": 1}),
        )


def test_fan_out_facets_advance_independently(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n---\nbody v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = _build_skill_graph(reconciler, registry)
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    metadata_v1 = graph.publish("skill:demo:metadata")
    activation_v1 = graph.publish("skill:demo:activation")
    assert metadata_v1.revision != activation_v1.revision

    # 只改 description:metadata 推进,activation 不推进。
    source.write_text("description: v2\n---\nbody v1\n", encoding="utf-8")
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    metadata_v2 = graph.publish("skill:demo:metadata")
    activation_still = graph.publish("skill:demo:activation")
    assert metadata_v2.revision != metadata_v1.revision
    assert activation_still is activation_v1

    # 只改正文:activation 推进,metadata 不推进。
    source.write_text("description: v2\n---\nbody v2\n", encoding="utf-8")
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    metadata_still = graph.publish("skill:demo:metadata")
    activation_v2 = graph.publish("skill:demo:activation")
    assert metadata_still is metadata_v2
    assert activation_v2.revision != activation_v1.revision

    # 只改被忽略的 frontmatter 字段:来源 raw revision 推进,两个 facet 都不推进。
    source.write_text("description: v2\nignored: x\n---\nbody v2\n", encoding="utf-8")
    _reconcile_skill(reconciler)
    source_revision = reconciler.observed_revision("src-skill").revision
    graph.observe_source("src-skill")
    assert graph.publish("skill:demo:metadata") is metadata_v2
    assert graph.publish("skill:demo:activation") is activation_v2
    # 来源 lineage 仍绑定当前 raw revision,但语义 revision 未变。
    assert metadata_v2.source_lineage != (
        (("src-skill", source_revision)),
    )


def test_fan_in_multiple_sources_one_resource(tmp_path: Path) -> None:
    first = tmp_path / "inline.jsonc"
    second = tmp_path / "user.jsonc"
    first.write_text("layer-inline\n", encoding="utf-8")
    second.write_text("layer-user\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={
            "src-inline": _skill_source("src-inline", first, tmp_path),
            "src-user": _skill_source("src-user", second, tmp_path),
        }
    )
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=ResourceRegistry())
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="config:effective",
            facet="config",
            display_uri="boxteam://workspace/config",
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


def test_dependency_chain_missing_and_mixed_generation(tmp_path: Path) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("agents v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-agents": _skill_source("src-agents", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:content",
            facet="content",
            display_uri="boxteam://workspace/agents",
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
            display_uri="boxteam://workspace/agents-projected",
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
        registry.last_valid_snapshot("agents:projected")

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
    assert registry.last_valid_snapshot("agents:projected") is projected_v1

    # 先发布上游,下游即可用新依赖向量发布。
    content_v2 = graph.publish("agents:content")
    projected_v2 = graph.publish("agents:projected")
    assert projected_v2.available is True
    assert projected_v2.revision != projected_v1.revision
    assert dict(projected_v2.source_lineage)["agents:content"] == content_v2.revision


def test_publish_all_follows_topological_order(tmp_path: Path) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("agents v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-agents": _skill_source("src-agents", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=registry)
    graph.register(
        SemanticResourceDescriptor(
            resource_kind="skills",
            resource_id="agents:content",
            facet="content",
            display_uri="boxteam://workspace/agents",
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
            display_uri="boxteam://workspace/agents-projected",
            dependency_resource_ids=("agents:content",),
        ),
        lambda inputs: SemanticPayload(
            payload={"projected": inputs["agents:content"].content}
        ),
    )
    reconciler.reconcile("src-agents")
    graph.observe_source("src-agents")
    published = graph.publish_all()
    assert all(snapshot.available for snapshot in published)
    by_id = {snapshot.resource_id: snapshot for snapshot in published}
    lineage = dict(by_id["agents:projected"].source_lineage)
    assert lineage["agents:content"] == by_id["agents:content"].revision


def test_source_unavailable_retains_last_valid(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n---\nbody\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = _build_skill_graph(reconciler, registry)
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    valid = graph.publish("skill:demo:metadata")

    source.write_bytes(b"\xff\xfe")
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    unavailable = graph.publish("skill:demo:metadata")
    assert unavailable.available is False
    assert unavailable.error_code == "source-unavailable"
    assert unavailable.retained_revision == valid.revision
    assert unavailable.payload is None
    # 旧 valid 快照仍可审计,不冒充新版本。
    assert registry.last_valid_snapshot("skill:demo:metadata") is valid

    source.write_text("description: v2\n---\nbody\n", encoding="utf-8")
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    recovered = graph.publish("skill:demo:metadata")
    assert recovered.available is True
    assert recovered.revision != valid.revision


def test_loader_failure_marks_unavailable_and_keeps_old(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = ResourceDerivationGraph(reconciler=reconciler, registry=registry)
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
            display_uri="boxteam://workspace/skill/demo",
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
    assert registry.last_valid_snapshot("skill:demo:metadata") is valid


def test_cas_publish_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = _build_skill_graph(reconciler, registry)
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    first = graph.publish("skill:demo:metadata")
    second = graph.publish("skill:demo:metadata")
    assert second is first
    # 直接 CAS:相同 revision 幂等返回已存快照。
    assert registry.publish(first) is first


def test_restart_rebuilds_identity_stable(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text("description: v1\n---\nbody\n", encoding="utf-8")
    reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
    )
    registry = ResourceRegistry()
    graph = _build_skill_graph(reconciler, registry)
    _reconcile_skill(reconciler)
    graph.observe_source("src-skill")
    before = {
        "skill:demo:metadata": graph.publish("skill:demo:metadata"),
        "skill:demo:activation": graph.publish("skill:demo:activation"),
    }

    # 模拟进程重启:全新 reconciler/graph/registry 实例,owner 重新登记 handle。
    rebuilt_reconciler = SourceReconciler(
        handles={"src-skill": _skill_source("src-skill", source, tmp_path)}
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
        assert tmp_path.as_posix() not in repr(snapshot)
        assert "SKILL.md" not in repr(snapshot)


def test_no_dynamic_import_or_plugin_surface_in_core_modules() -> None:
    """derivation/registry 核心禁止动态 import/plugin manifest 面。"""
    forbidden = {"importlib", "__import__", "exec", "eval", "compile"}
    targets = [
        PROJECT_ROOT / "app/services/infrastructure/resource_platform/derivation",
        PROJECT_ROOT / "app/services/infrastructure/resource_platform/registry",
    ]
    violations: list[str] = []
    for directory in targets:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name.startswith("importlib") for alias in node.names
                ):
                    violations.append(f"{path}:{node.lineno}:import importlib")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (
                    node.func.id in forbidden
                ):
                    violations.append(f"{path}:{node.lineno}:{node.func.id}")
    assert violations == [], ("发现动态执行/插件面:", violations)
