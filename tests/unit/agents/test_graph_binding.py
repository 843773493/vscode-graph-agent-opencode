"""OpenSpec 8.4 GraphBinding 基础设施的单元测试。

覆盖：VO 四元组校验、hash 口径确定性、registry 注册/解析与四种不匹配、
不回退红线、持久化往返与重启重建、AgentFactory 集成、组合绑定辅助。
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from app.agents import agent_factory
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    DEEP_AGENT_GRAPH_ID,
    DEEP_AGENT_MIDDLEWARE_STACK,
    DEEP_AGENT_TOOL_FACE,
    GRAPH_FACTORY_REGISTRY,
    GraphBinding,
    GraphBindingOwnerKey,
    GraphBindingUnavailableError,
    GraphFactoryRegistrationConflictError,
    GraphFactoryRegistry,
    InvocationGraphBinding,
    JsonFileGraphBindingStore,
    binding_for,
    compute_capability_profile_hash,
    compute_graph_schema_hash,
)
from app.agents.tool_invocation_context import ThreadRuntimeBinding

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_DIGEST_C = "sha256:" + "c" * 64

_STORE_FILE_NAME = "graph-bindings.json"


def _binding(
    *,
    graph_id: str = "test-graph",
    graph_revision: int = 1,
    graph_schema_hash: str = _DIGEST_A,
    capability_profile_hash: str = _DIGEST_B,
) -> GraphBinding:
    return GraphBinding(
        graph_id=graph_id,
        graph_revision=graph_revision,
        graph_schema_hash=graph_schema_hash,
        capability_profile_hash=capability_profile_hash,
    )


def _make_builder(name: str):
    def builder() -> object:
        return name

    return builder


def _owner(
    session_id: str = "ses_graph",
    thread_id: str = "main",
) -> GraphBindingOwnerKey:
    return GraphBindingOwnerKey(session_id=session_id, thread_id=thread_id)


@pytest.fixture()
def registry() -> GraphFactoryRegistry:
    """每个测试独立的 registry，不污染进程级单例。"""
    return GraphFactoryRegistry()


# ---------------------------------------------------------------------------
# VO 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param({"graph_id": ""}, "graph_id", id="empty-graph-id"),
        pytest.param({"graph_id": "   "}, "graph_id", id="blank-graph-id"),
        pytest.param({"graph_id": 123}, "graph_id", id="non-string-graph-id"),
        pytest.param({"graph_id": "/abs/graph"}, "稳定标识", id="posix-path-graph-id"),
        pytest.param({"graph_id": "C:\\graph"}, "稳定标识", id="windows-path-graph-id"),
        pytest.param({"graph_id": "a/b"}, "路径分隔符", id="separator-graph-id"),
        pytest.param({"graph_id": ".."}, "路径分隔符", id="dotdot-graph-id"),
        pytest.param({"graph_id": "~graph"}, "路径分隔符", id="tilde-graph-id"),
        pytest.param({"graph_id": "x" * 513}, "长度上限", id="overlong-graph-id"),
        pytest.param({"graph_revision": 0}, "graph_revision", id="zero-revision"),
        pytest.param({"graph_revision": -1}, "graph_revision", id="negative-revision"),
        pytest.param({"graph_revision": True}, "graph_revision", id="bool-revision"),
        pytest.param({"graph_revision": "1"}, "graph_revision", id="string-revision"),
        pytest.param({"graph_revision": 1.0}, "graph_revision", id="float-revision"),
        pytest.param(
            {"graph_schema_hash": "sha256:" + "g" * 64},
            "graph_schema_hash",
            id="non-hex-schema-hash",
        ),
        pytest.param(
            {"graph_schema_hash": "sha256:" + "a" * 63},
            "graph_schema_hash",
            id="short-schema-hash",
        ),
        pytest.param(
            {"graph_schema_hash": "SHA256:" + "a" * 64},
            "graph_schema_hash",
            id="uppercase-prefix-schema-hash",
        ),
        pytest.param(
            {"graph_schema_hash": "abc"},
            "graph_schema_hash",
            id="plain-schema-hash",
        ),
        pytest.param(
            {"graph_schema_hash": None},
            "graph_schema_hash",
            id="none-schema-hash",
        ),
        pytest.param(
            {"capability_profile_hash": "sha256:nothex"},
            "capability_profile_hash",
            id="bad-capability-hash",
        ),
        pytest.param(
            {"capability_profile_hash": 123},
            "capability_profile_hash",
            id="non-string-capability-hash",
        ),
    ],
)
def test_graph_binding_rejects_invalid_fields(
    overrides: dict[str, object],
    match: str,
) -> None:
    values: dict[str, object] = {
        "graph_id": "test-graph",
        "graph_revision": 1,
        "graph_schema_hash": _DIGEST_A,
        "capability_profile_hash": _DIGEST_B,
    }
    values.update(overrides)
    # 类型非法抛 TypeError，值非法抛 ValueError；两者都是显式拒绝。
    with pytest.raises((ValueError, TypeError), match=match):
        GraphBinding(**values)  # type: ignore[arg-type]


def test_graph_binding_is_frozen_and_exposes_selector_fields() -> None:
    binding = _binding()
    assert binding.selector_fields() == (
        "test-graph",
        1,
        _DIGEST_A,
        _DIGEST_B,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.graph_id = "other"  # type: ignore[misc]


def test_owner_key_rejects_empty_identity() -> None:
    with pytest.raises(ValueError, match="session_id"):
        GraphBindingOwnerKey(session_id="", thread_id="main")
    with pytest.raises(ValueError, match="session_id"):
        GraphBindingOwnerKey(session_id=None, thread_id="main")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="thread_id"):
        GraphBindingOwnerKey(session_id="ses_graph", thread_id="")


# ---------------------------------------------------------------------------
# hash 口径
# ---------------------------------------------------------------------------


def test_graph_schema_hash_is_deterministic_content_digest() -> None:
    first = compute_graph_schema_hash(
        graph_id="deep-agent",
        middleware_stack=("A", "B"),
        tool_face=("t1", "t2"),
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    assert first == compute_graph_schema_hash(
        graph_id="deep-agent",
        middleware_stack=("A", "B"),
        tool_face=("t1", "t2"),
    )
    # middleware 栈是有序形状：换序即不同 hash。
    assert first != compute_graph_schema_hash(
        graph_id="deep-agent",
        middleware_stack=("B", "A"),
        tool_face=("t1", "t2"),
    )
    # 工具面变化 → 不同 hash。
    assert first != compute_graph_schema_hash(
        graph_id="deep-agent",
        middleware_stack=("A", "B"),
        tool_face=("t1",),
    )
    # graph_id 参与 hash。
    assert first != compute_graph_schema_hash(
        graph_id="other-agent",
        middleware_stack=("A", "B"),
        tool_face=("t1", "t2"),
    )


def test_capability_profile_hash_is_deterministic_content_digest() -> None:
    first = compute_capability_profile_hash(
        capability_profile={"goal_enabled": True, "thread_role": "main"}
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    assert first == compute_capability_profile_hash(
        capability_profile={"thread_role": "main", "goal_enabled": True}
    )
    assert first != compute_capability_profile_hash(
        capability_profile={"goal_enabled": False, "thread_role": "main"}
    )


def test_hash_helpers_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="graph_id"):
        compute_graph_schema_hash(
            graph_id="",
            middleware_stack=("A",),
            tool_face=("t",),
        )
    with pytest.raises(ValueError, match="middleware_stack"):
        compute_graph_schema_hash(
            graph_id="g",
            middleware_stack=(),
            tool_face=("t",),
        )
    with pytest.raises(ValueError, match="middleware_stack"):
        compute_graph_schema_hash(
            graph_id="g",
            middleware_stack=("A", ""),
            tool_face=("t",),
        )
    with pytest.raises(ValueError, match="tool_face"):
        compute_graph_schema_hash(
            graph_id="g",
            middleware_stack=("A",),
            tool_face=(),
        )
    with pytest.raises(ValueError, match="capability_profile"):
        compute_capability_profile_hash(capability_profile={})
    with pytest.raises((ValueError, TypeError), match="capability_profile"):
        compute_capability_profile_hash(capability_profile={"goal": {"nested": True}})
    with pytest.raises(ValueError, match="capability_profile"):
        compute_capability_profile_hash(capability_profile={"": "x"})


# ---------------------------------------------------------------------------
# registry：注册与解析
# ---------------------------------------------------------------------------


def test_registry_register_and_resolve_roundtrip(registry: GraphFactoryRegistry) -> None:
    binding = _binding()
    builder = _make_builder("blueprint")
    registry.register(binding, builder)
    assert registry.resolve(binding) is builder


def test_registry_register_is_idempotent_for_identical_entry(
    registry: GraphFactoryRegistry,
) -> None:
    binding = _binding()
    builder = _make_builder("blueprint")
    registry.register(binding, builder)
    registry.register(binding, builder)
    assert registry.resolve(binding) is builder


def test_registry_rejects_non_callable_builder(
    registry: GraphFactoryRegistry,
) -> None:
    with pytest.raises(TypeError, match="可调用对象"):
        registry.register(_binding(), "not-callable")  # type: ignore[arg-type]


def test_registry_conflict_on_different_schema_hash(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(), _make_builder("first"))
    with pytest.raises(GraphFactoryRegistrationConflictError) as excinfo:
        registry.register(
            _binding(graph_schema_hash=_DIGEST_C),
            _make_builder("second"),
        )
    message = str(excinfo.value)
    assert "graph-factory-registration-conflict" in message
    assert _DIGEST_A in message
    assert _DIGEST_C in message


def test_registry_conflict_on_different_capability_hash(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(), _make_builder("first"))
    with pytest.raises(GraphFactoryRegistrationConflictError) as excinfo:
        registry.register(
            _binding(capability_profile_hash=_DIGEST_C),
            _make_builder("second"),
        )
    message = str(excinfo.value)
    assert "graph-factory-registration-conflict" in message
    assert _DIGEST_B in message
    assert _DIGEST_C in message


def test_registry_conflict_on_same_binding_different_builder(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(), _make_builder("first"))
    with pytest.raises(GraphFactoryRegistrationConflictError):
        registry.register(_binding(), _make_builder("second"))


# ---------------------------------------------------------------------------
# registry：四种不匹配，均带期望与实际值
# ---------------------------------------------------------------------------


def test_resolve_fails_when_graph_id_not_registered(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(graph_id="known-graph"), _make_builder("b"))
    binding = _binding(graph_id="unknown-graph")
    with pytest.raises(GraphBindingUnavailableError) as excinfo:
        registry.resolve(binding)
    message = str(excinfo.value)
    assert "graph_binding_unavailable" in message
    assert "unknown-graph" in message
    assert "known-graph" in message
    # 消息含完整 binding 四元组。
    assert _DIGEST_A in message
    assert _DIGEST_B in message


def test_resolve_does_not_fallback_to_other_revision(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(graph_revision=2), _make_builder("v2"))
    stale_binding = _binding(graph_revision=1)
    with pytest.raises(GraphBindingUnavailableError) as excinfo:
        registry.resolve(stale_binding)
    message = str(excinfo.value)
    assert "graph_binding_unavailable" in message
    assert "不回退" in message
    assert "graph_revision=1" in message
    assert "[2]" in message


def test_resolve_fails_on_schema_hash_drift(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(), _make_builder("b"))
    drifted = _binding(graph_schema_hash=_DIGEST_C)
    with pytest.raises(GraphBindingUnavailableError) as excinfo:
        registry.resolve(drifted)
    message = str(excinfo.value)
    assert "graph_binding_unavailable" in message
    assert f"期望(注册)={_DIGEST_A!r}" in message
    assert f"实际(binding)={_DIGEST_C!r}" in message


def test_resolve_fails_on_capability_hash_drift(
    registry: GraphFactoryRegistry,
) -> None:
    registry.register(_binding(), _make_builder("b"))
    drifted = _binding(capability_profile_hash=_DIGEST_C)
    with pytest.raises(GraphBindingUnavailableError) as excinfo:
        registry.resolve(drifted)
    message = str(excinfo.value)
    assert "graph_binding_unavailable" in message
    assert f"期望(注册)={_DIGEST_B!r}" in message
    assert f"实际(binding)={_DIGEST_C!r}" in message


# ---------------------------------------------------------------------------
# AgentFactory 集成
# ---------------------------------------------------------------------------


def test_deep_agent_graph_binding_is_descriptor_stable() -> None:
    binding = DEEP_AGENT_GRAPH_BINDING
    assert binding.graph_id == DEEP_AGENT_GRAPH_ID == "deep-agent"
    assert binding.graph_revision >= 1
    assert binding.graph_schema_hash == compute_graph_schema_hash(
        graph_id=DEEP_AGENT_GRAPH_ID,
        middleware_stack=DEEP_AGENT_MIDDLEWARE_STACK,
        tool_face=DEEP_AGENT_TOOL_FACE,
    )
    assert binding.capability_profile_hash == compute_capability_profile_hash(
        capability_profile=dict(DEEP_AGENT_CAPABILITY_PROFILE)
    )


def test_deep_agent_graph_binding_is_registered_in_process_registry() -> None:
    # import app.agents.agent_factory 已触发模块级闭集注册。
    assert "deep-agent" in GRAPH_FACTORY_REGISTRY.registered_graph_ids()
    assert (
        GRAPH_FACTORY_REGISTRY.resolve(DEEP_AGENT_GRAPH_BINDING)
        is agent_factory.create_my_deep_agent
    )


# ---------------------------------------------------------------------------
# 持久化：JSON 独立小存储
# ---------------------------------------------------------------------------


def test_json_store_roundtrip_preserves_four_tuple(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    binding = _binding()
    store.save_graph_binding(_owner(), binding)
    loaded = store.load_graph_binding(_owner())
    assert loaded is not None
    assert loaded == binding
    assert loaded.selector_fields() == binding.selector_fields()


def test_json_store_load_returns_none_without_persistence(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    assert store.load_graph_binding(_owner()) is None


def test_json_store_save_is_idempotent_for_identical_binding(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    binding = _binding()
    store.save_graph_binding(_owner(), binding)
    store.save_graph_binding(_owner(), binding)
    assert store.load_graph_binding(_owner()) == binding


def test_json_store_conflict_on_different_binding(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    with pytest.raises(RuntimeError) as excinfo:
        store.save_graph_binding(_owner(), _binding(graph_revision=2))
    message = str(excinfo.value)
    assert "graph-binding-store-conflict" in message
    assert "graph_revision=1" in message
    assert "graph_revision=2" in message


def test_json_store_keeps_owners_isolated(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(thread_id="main"), _binding())
    store.save_graph_binding(
        _owner(thread_id="child"),
        _binding(graph_revision=2),
    )
    assert store.load_graph_binding(_owner(thread_id="main")) == _binding()
    assert (
        store.load_graph_binding(_owner(thread_id="child"))
        == _binding(graph_revision=2)
    )
    assert store.load_graph_binding(_owner(session_id="other")) is None


def test_json_store_rejects_wrong_types(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    with pytest.raises(TypeError):
        store.save_graph_binding("not-owner", _binding())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.save_graph_binding(_owner(), "not-binding")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.load_graph_binding("not-owner")  # type: ignore[arg-type]


def test_persisted_document_is_plain_selector_not_python_object(
    tmp_path: Path,
) -> None:
    """红线：持久化的是 factory selector 文档，不是 CompiledStateGraph 或对象引用。"""
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    raw = (tmp_path / _STORE_FILE_NAME).read_text(encoding="utf-8")
    document = json.loads(raw)
    entry = document["bindings"]["ses_graph"]["main"]
    assert set(entry) == {
        "graph_id",
        "graph_revision",
        "graph_schema_hash",
        "capability_profile_hash",
    }
    # 文档不含宿主机路径或任何对象引用形态。
    assert str(tmp_path) not in raw


def test_json_store_rejects_corrupted_json(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    (tmp_path / _STORE_FILE_NAME).write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="无法解析 JSON"):
        store.load_graph_binding(_owner())


def test_json_store_rejects_unknown_entry_fields(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    document = json.loads((tmp_path / _STORE_FILE_NAME).read_text(encoding="utf-8"))
    document["bindings"]["ses_graph"]["main"]["unexpected"] = "field"
    (tmp_path / _STORE_FILE_NAME).write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="字段集非法"):
        store.load_graph_binding(_owner())


def test_json_store_rejects_wrong_document_version(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    document = json.loads((tmp_path / _STORE_FILE_NAME).read_text(encoding="utf-8"))
    document["version"] = 99
    (tmp_path / _STORE_FILE_NAME).write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="版本非法"):
        store.load_graph_binding(_owner())


def test_json_store_rejects_invalid_persisted_digest(tmp_path: Path) -> None:
    store = JsonFileGraphBindingStore(tmp_path)
    store.save_graph_binding(_owner(), _binding())
    document = json.loads((tmp_path / _STORE_FILE_NAME).read_text(encoding="utf-8"))
    document["bindings"]["ses_graph"]["main"]["graph_schema_hash"] = "abc"
    (tmp_path / _STORE_FILE_NAME).write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="graph_schema_hash"):
        store.load_graph_binding(_owner())


# ---------------------------------------------------------------------------
# 重启重建
# ---------------------------------------------------------------------------


def test_restart_rebuild_resolves_persisted_binding(tmp_path: Path) -> None:
    directory = tmp_path / "graph_binding"
    binding = _binding()
    builder = _make_builder("blueprint")
    GraphFactoryRegistry().register(binding, builder)
    JsonFileGraphBindingStore(directory).save_graph_binding(_owner(), binding)

    # 模拟重启：全新 store 与 registry 实例，重新注册同一 family。
    restarted_store = JsonFileGraphBindingStore(directory)
    restarted_registry = GraphFactoryRegistry()
    restarted_builder = _make_builder("rebuilt")
    restarted_registry.register(binding, restarted_builder)

    loaded = restarted_store.load_graph_binding(_owner())
    assert loaded == binding
    assert restarted_registry.resolve(loaded) is restarted_builder


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda loaded: dataclasses.replace(loaded, graph_id="other-graph"),
            id="graph_id-drift",
        ),
        pytest.param(
            lambda loaded: dataclasses.replace(
                loaded,
                graph_revision=loaded.graph_revision + 1,
            ),
            id="graph_revision-drift",
        ),
        pytest.param(
            lambda loaded: dataclasses.replace(loaded, graph_schema_hash=_DIGEST_C),
            id="schema-hash-drift",
        ),
        pytest.param(
            lambda loaded: dataclasses.replace(
                loaded,
                capability_profile_hash=_DIGEST_C,
            ),
            id="capability-hash-drift",
        ),
    ],
)
def test_restart_rebuild_fails_when_persisted_binding_tampered(
    tmp_path: Path,
    tamper,
) -> None:
    directory = tmp_path / "graph_binding"
    JsonFileGraphBindingStore(directory).save_graph_binding(_owner(), _binding())
    registry = GraphFactoryRegistry()
    registry.register(_binding(), _make_builder("b"))

    loaded = JsonFileGraphBindingStore(directory).load_graph_binding(_owner())
    assert loaded is not None
    with pytest.raises(GraphBindingUnavailableError):
        registry.resolve(tamper(loaded))


# ---------------------------------------------------------------------------
# P2：组合绑定辅助
# ---------------------------------------------------------------------------


def test_binding_for_combines_graph_and_thread_bindings() -> None:
    thread_binding = ThreadRuntimeBinding(session_id="ses_graph", thread_id="main")
    combined = binding_for(DEEP_AGENT_GRAPH_BINDING, thread_binding)
    assert isinstance(combined, InvocationGraphBinding)
    assert combined.graph is DEEP_AGENT_GRAPH_BINDING
    assert combined.thread is thread_binding
    with pytest.raises(TypeError, match="GraphBinding"):
        binding_for("not-graph", thread_binding)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ThreadRuntimeBinding"):
        binding_for(DEEP_AGENT_GRAPH_BINDING, "not-thread")  # type: ignore[arg-type]
