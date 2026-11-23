"""单父候选解析：真实 JSONC 入口、定义来源及不可变路径共享。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.core.config_sources import read_stable_config_file, verify_stable_config_file
from app.services.infrastructure.config.source_uri import config_file_uri
from configs.inheritance import ConfigGraph, NodeLocation
from configs.inheritance.values import thaw
from configs.runtime import merge_json_objects


@pytest.fixture
def settings_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "$defs": {
            "proxy": {"type": "object", "x-merge": "replace", "required": ["mode"], "properties": {
                "mode": {"enum": ["direct", "environment", "explicit"]}, "url": {"type": "string"},
            }},
        },
        "properties": {
            "workspace": {"type": "object"},
            "network": {"type": "object", "properties": {"proxy": {"$ref": "#/$defs/proxy"}}},
            "model_options": {"type": "object", "properties": {
                "required_option": {"type": "string"},
                "request_options": {"type": "object", "properties": {
                    "parameters": {"type": "object", "x-key-aliases": {"max_tokens": "max_output_tokens"},
                                   "additionalProperties": {"x-merge": "replace"}},
                    "default_headers": {"type": "object", "x-key-casefold": True},
                }},
            }},
        },
    }


def test_source_projection_and_nodes_use_the_same_schema_merge(
    settings_schema: dict, graph_factory, write_config,
) -> None:
    base = {"network": {"proxy": {"mode": "explicit", "url": "http://proxy.invalid:3128"}},
            "model_options": {"request_options": {"parameters": {
                "max_tokens": 50, "extra_body": {"parent_only": True, "keep": True},
            }, "default_headers": {"X-Route": "parent"}}}}
    local = {"network": {"proxy": {"mode": "direct"}}, "model_options": {"request_options": {
        "parameters": {"max_output_tokens": None, "extra_body": {"keep": False}},
        "default_headers": {"x-ROUTE": "local"},
    }}}
    write_config("parent.jsonc", base)
    leaf = write_config("leaf.jsonc", local, parent="parent.jsonc")
    node = graph_factory(leaf).resolve_scope()
    projected = merge_json_objects(base, local, schema=settings_schema)
    assert projected["network"] == {"proxy": {"mode": "direct"}}
    assert projected["model_options"]["request_options"] == {
        "parameters": {"max_output_tokens": None, "extra_body": {"keep": False}},
        "default_headers": {"x-ROUTE": "local"},
    }
    assert thaw(node.settings) == {**projected, "model_options": {"request_options": {
        **projected["model_options"]["request_options"], "default_headers": {"x-route": "local"},
    }}}
    assert node.origins.locate(("model_options", "request_options", "default_headers", "x-route")).pointer == (
        "/settings/model_options/request_options/default_headers/x-ROUTE"
    )
    assert base["network"]["proxy"]["url"] == "http://proxy.invalid:3128"
    assert base["model_options"]["request_options"]["parameters"]["extra_body"]["keep"] is True


@pytest.fixture
def graph_factory(tmp_path: Path, settings_schema: dict):
    def create(entry: Path, *, owner: str = "gateway-a/workspace-a", domain: str = "workspace",
               schema: dict | None = None, aliases: dict | None = None, reads: Counter | None = None,
               authorize=None, registered_uris: dict | None = None, options_schema: dict | None = None,
               source_documents=None):
        def read(path: Path):
            if reads is not None:
                reads[path] += 1
            return read_stable_config_file(path)

        scope_schema = schema or settings_schema
        option_schema = options_schema if options_schema is not None else {
            **scope_schema, "required": [],
            "properties": {key: value for key, value in scope_schema["properties"].items()
                           if key in {"network", "model_options"}},
        }
        return ConfigGraph(
            domain=domain, version=4, owner_id=owner, entry=entry, aliases=aliases or {},
            settings_schema=scope_schema, options_schema=option_schema,
            source_uri=lambda path: config_file_uri(scope="user", scope_id="local",
                logical_name=path.relative_to(tmp_path).as_posix().replace("/", "_").replace(".", "_")),
            authorize_source=authorize or (lambda path: path.is_relative_to(tmp_path)),
            registered_uris=registered_uris, read_source=read, source_documents=source_documents,
        )
    return create


@pytest.fixture
def write_config(tmp_path: Path):
    def write(name: str, settings: dict, *, parent: object = None, profiles: dict | None = None,
              version: int = 4) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {"config_version": version, "settings": settings}
        if parent is not None:
            document["extends"] = parent
        if profiles is not None:
            document["profiles"] = profiles
        path.write_text("// 真实 JSONC 注释\n" + json.dumps(document), encoding="utf-8")
        return path
    return write


def test_only_explicit_chain_participates_and_metadata_is_not_merged(write_config, graph_factory):
    write_config("ignored.jsonc", {"workspace": {"ignored": True}})
    parent = write_config("parent.jsonc", {"workspace": {"n": 9, "enabled": True, "array": [1], "map": {"x": 1}}},
                          profiles={"unused": {"settings": {}, "extends": "missing.jsonc"}})
    entry = write_config("entry.jsonc", {"workspace": {"n": 0, "enabled": False, "array": [], "map": {}}}, parent="parent.jsonc")
    graph = graph_factory(entry)
    result = graph.resolve_scope()
    assert thaw(result.settings) == {"workspace": {"n": 0, "enabled": False, "array": [], "map": {"x": 1}}}
    assert [node.location.path for node in result.chain] == [parent, entry]
    assert set(graph.dependencies) == {parent, entry}
    assert result.origins.locate(("workspace", "n")).pointer == "/settings/workspace/n"
    assert "entry_jsonc" in result.origins.locate(("workspace", "n")).source_uri
    assert "parent_jsonc" in result.origins.locate(("workspace", "map", "x")).source_uri


def test_atomic_objects_and_parameter_values_drop_parent_fields(write_config, graph_factory):
    write_config("parent.jsonc", {"network": {"proxy": {"mode": "explicit", "url": "http://proxy.invalid"}},
        "model_options": {"request_options": {"parameters": {"max_tokens": 3, "extra_body": {"one": 1, "two": 2}},
                                                "default_headers": {"X-Test": "parent"}}}})
    entry = write_config("entry.jsonc", {"network": {"proxy": {"mode": "direct"}},
        "model_options": {"request_options": {"parameters": {"max_output_tokens": None, "extra_body": {"one": 9}},
                                                "default_headers": {"x-test": "child"}}}}, parent="parent.jsonc")
    result = graph_factory(entry).resolve_scope()
    assert thaw(result.settings["network"]) == {"proxy": {"mode": "direct"}}
    assert thaw(result.settings["model_options"]) == {"request_options": {
        "parameters": {"max_output_tokens": None, "extra_body": {"one": 9}}, "default_headers": {"x-test": "child"}}}
    assert result.origins.locate(("model_options", "request_options", "default_headers", "x-test")).pointer.endswith("/x-test")
    with pytest.raises(KeyError, match="/network/proxy/url"):
        result.origins.locate(("network", "proxy", "url"))


def test_same_node_alias_conflict_and_header_case_conflict_rejected(write_config, graph_factory):
    for key, value in (("parameters", {"max_tokens": 1, "max_output_tokens": 2}),
                       ("default_headers", {"x-test": "one", "X-Test": "two"})):
        entry = write_config("entry.jsonc", {"model_options": {"request_options": {key: value}}})
        with pytest.raises(ValueError, match="重复逻辑键"):
            graph_factory(entry).resolve_scope()


def test_inherited_model_reference_keeps_declaration_file_and_reads_once(write_config, graph_factory):
    reads = Counter()
    parent = write_config("shared/user.jsonc", {"workspace": {"llm": {"providers": {
        "lab": {"config_ref": "#/profiles/service", "models": {
            "fast": {"model": "remote-fast", "config_ref": "#/profiles/fast"},
            "also-fast": {"model": "remote-other", "config_ref": "#/profiles/fast"},
        }},
    }}}}, profiles={
        "service": {"extends": "context:workspace", "settings": {"model_options": {"required_option": "user"}}},
        "fast": {"extends": "context:provider/lab", "settings": {"network": {"proxy": {"mode": "direct"}}}},
    })
    entry = write_config("workspace/entry.jsonc", {"workspace": {"name": "current"}}, parent="../shared/user.jsonc",
                         profiles={"fast": {"settings": {"model_options": {"required_option": "wrong"}}}})
    graph = graph_factory(entry, reads=reads)
    result = graph.resolve_model("lab", "fast")
    assert result.location.path == parent
    assert result.settings["model_options"]["required_option"] == "user"
    assert result.settings["workspace"]["name"] == "current"
    assert result is graph.resolve_model("lab", "also-fast")
    assert reads == {parent: 1, entry: 1}
    assert graph.resolve_scope().settings["workspace"] is result.settings["workspace"]
    for _ in range(20):
        assert graph.resolve_model("lab", "fast") is result
    assert reads == {parent: 1, entry: 1}
    with pytest.raises(TypeError):
        result.settings["workspace"]["name"] = "mutated"


def test_shared_context_is_bound_to_each_workspace(write_config, graph_factory):
    write_config("shared.jsonc", {}, profiles={"shared": {"extends": "context:workspace", "settings": {}}})
    a = write_config("a.jsonc", {"workspace": {"name": "A"}})
    b = write_config("b.jsonc", {"workspace": {"name": "B"}})
    first = graph_factory(a, owner="owner/A").resolve_reference("shared.jsonc#/profiles/shared", declared_at=NodeLocation(a))
    second = graph_factory(b, owner="owner/B").resolve_reference("shared.jsonc#/profiles/shared", declared_at=NodeLocation(b))
    assert first.node_id != second.node_id
    assert first.settings["workspace"]["name"] == "A"
    assert second.settings["workspace"]["name"] == "B"


@pytest.mark.parametrize("parent", [["other.jsonc"], "https://invalid.example/config.jsonc", "context:gateway", "user:unknown"])
def test_invalid_parent_never_falls_back(write_config, graph_factory, parent):
    entry = write_config("entry.jsonc", {"workspace": {}}, parent=parent)
    with pytest.raises((ValueError, PermissionError)):
        graph_factory(entry).resolve_scope()


def test_missing_parent_dependency_survives_for_owner_observation(write_config, graph_factory):
    entry = write_config("entry.jsonc", {}, parent="missing.jsonc")
    graph = graph_factory(entry)
    with pytest.raises(FileNotFoundError):
        graph.resolve_scope()
    assert graph.dependencies[entry.parent / "missing.jsonc"].presence == "absent"
    write_config("missing.jsonc", {"workspace": {"name": "repaired"}})
    assert graph_factory(entry).resolve_scope().settings["workspace"]["name"] == "repaired"


def test_symlink_and_provider_cycles_fail(write_config, graph_factory):
    entry = write_config("entry.jsonc", {}, parent="alias.jsonc")
    alias = entry.with_name("alias.jsonc")
    alias.symlink_to(entry)
    with pytest.raises(ValueError, match="循环"):
        graph_factory(entry).resolve_scope()
    entry = write_config("entry.jsonc", {"workspace": {"llm": {"providers": {
        "lab": {"config_ref": "#/profiles/self"}}}}},
        profiles={"self": {"extends": "context:provider/lab", "settings": {}}})
    with pytest.raises(ValueError, match="循环"):
        graph_factory(entry).resolve_provider("lab")


def test_root_and_alias_and_registered_uri_share_one_file_identity(write_config, graph_factory):
    parent = write_config("builtin.jsonc", {"workspace": {"name": "builtin"}},
                          profiles={"empty": {"settings": {}}})
    entry = write_config("entry.jsonc", {}, parent="builtin:workspace")
    uri = config_file_uri(scope="user", scope_id="local", logical_name="builtin")
    graph = graph_factory(entry, aliases={"builtin:workspace": parent}, registered_uris={uri: parent})
    assert graph.resolve_scope().parent is graph.resolve_reference(uri, declared_at=NodeLocation(entry))
    with pytest.raises(ValueError):
        graph_factory(entry, aliases={"builtin:gateway": parent})


def test_cross_domain_and_unauthorized_sources_rejected_before_read(write_config, graph_factory):
    entry = write_config(".boxteam/entry.jsonc", {})
    reads = Counter()
    with pytest.raises(PermissionError):
        graph_factory(entry, domain="gateway", reads=reads).resolve_scope()
    assert not reads
    with pytest.raises(PermissionError):
        graph_factory(entry, authorize=lambda _path: False, reads=reads).resolve_scope()
    assert not reads


def test_schema_partial_nodes_validate_only_the_final_value(write_config, graph_factory, settings_schema):
    settings_schema["properties"]["model_options"]["required"] = ["required_option"]
    entry = write_config("entry.jsonc", {}, profiles={
        "partial": {"settings": {"model_options": {}}},
        "final": {"extends": "#/profiles/partial", "settings": {"model_options": {"required_option": "complete"}}},
    })
    graph = graph_factory(entry)
    assert graph.resolve_reference("#/profiles/final", declared_at=NodeLocation(entry)).settings["model_options"]["required_option"] == "complete"
    with pytest.raises(ValueError, match="校验失败"):
        graph.resolve_reference("#/profiles/partial", declared_at=NodeLocation(entry))


def test_wrong_version_namespace_and_null_are_not_silently_repaired(write_config, graph_factory, settings_schema):
    for settings, version in (({"gateway": {}}, 4), ({}, 3), ({"model_options": {"required_option": None}}, 4)):
        entry = write_config("entry.jsonc", settings, version=version)
        with pytest.raises(ValueError, match="校验失败"):
            graph_factory(entry).resolve_scope()


def test_snapshot_detects_parent_change_without_warm_read(write_config, graph_factory):
    parent = write_config("parent.jsonc", {"workspace": {"value": 1}})
    entry = write_config("entry.jsonc", {}, parent="parent.jsonc")
    graph = graph_factory(entry)
    result = graph.resolve_scope()
    write_config("parent.jsonc", {"workspace": {"value": 2}})
    assert graph.resolve_scope() is result
    assert result.settings["workspace"]["value"] == 1
    with pytest.raises(RuntimeError, match="变化"):
        verify_stable_config_file(graph.dependencies[parent])
    assert graph_factory(entry).resolve_scope().settings["workspace"]["value"] == 2


def test_equal_override_is_local_and_array_item_keeps_original_location(write_config, graph_factory):
    write_config("parent.jsonc", {"workspace": {"value": 1, "items": [{"name": "parent"}]}})
    entry = write_config("entry.jsonc", {"workspace": {"value": 1, "items": [{"name": "child"}]}}, parent="parent.jsonc")
    result = graph_factory(entry).resolve_scope()
    assert result.origins.locate(("workspace", "value")).source_uri.endswith("entry_jsonc")
    assert result.origins.locate(("workspace", "items", "0", "name")).pointer == "/settings/workspace/items/0/name"
    restored = write_config("entry.jsonc", {"workspace": {}}, parent="parent.jsonc")
    assert graph_factory(restored).resolve_scope().origins.locate(("workspace", "value")).source_uri.endswith("parent_jsonc")


def test_schema_reference_chain_preserves_replace_semantics(write_config, graph_factory, settings_schema):
    settings_schema["$defs"]["proxy-alias"] = {"$ref": "#/$defs/proxy"}
    settings_schema["properties"]["network"]["properties"]["proxy"] = {"$ref": "#/$defs/proxy-alias"}
    write_config("parent.jsonc", {"network": {"proxy": {"mode": "explicit", "url": "http://proxy.invalid"}}})
    entry = write_config("entry.jsonc", {"network": {"proxy": {"mode": "direct"}}}, parent="parent.jsonc")
    assert thaw(graph_factory(entry).resolve_scope().settings) == {"network": {"proxy": {"mode": "direct"}}}


def test_external_schema_is_never_downloaded(write_config, graph_factory, settings_schema):
    settings_schema["properties"]["workspace"] = {"$ref": "https://schema.invalid/private-config"}
    entry = write_config("entry.jsonc", {"workspace": {}})
    with pytest.raises(ValueError, match="本地 JSON Pointer|禁止自动下载"):
        graph_factory(entry).resolve_scope()


def test_scope_shared_options_can_be_partial_but_selected_model_must_be_complete(write_config, graph_factory):
    entry = write_config("entry.jsonc", {"model_options": {"common": True}}, profiles={
        "incomplete": {"extends": "context:workspace", "settings": {}},
        "complete": {"extends": "context:workspace", "settings": {"model_options": {"endpoint": "https://model.invalid"}}},
    })
    graph = graph_factory(entry, options_schema={
        "type": "object", "required": ["model_options"], "properties": {
            "model_options": {"type": "object", "required": ["endpoint"], "properties": {"endpoint": {"type": "string"}}},
        },
    })
    assert graph.resolve_scope().settings["model_options"]["common"] is True
    with pytest.raises(ValueError, match="校验失败"):
        graph.resolve_reference("#/profiles/incomplete", declared_at=NodeLocation(entry))
    assert graph.resolve_reference("#/profiles/complete", declared_at=NodeLocation(entry)).settings["model_options"]["common"] is True


def test_missing_profile_does_not_echo_the_config_document(write_config, graph_factory):
    entry = write_config("entry.jsonc", {"model_options": {"api_key": "synthetic-secret-value"}})
    graph = graph_factory(entry)
    with pytest.raises(ValueError, match="未指向节点") as failure:
        graph.resolve_reference("#/profiles/missing", declared_at=NodeLocation(entry))
    assert "synthetic-secret-value" not in str(failure.value)
    assert str(entry.parent) not in str(failure.value)


def test_fixed_sources_restore_the_active_chain_without_reading_changed_files(write_config, graph_factory):
    parent = write_config("parent.jsonc", {"network": {"proxy": {"mode": "explicit", "url": "http://proxy.invalid"}}})
    entry = write_config("entry.jsonc", {}, parent="parent.jsonc")
    original = graph_factory(entry)
    scope = original.resolve_scope()
    documents = original.source_documents
    parent.write_text("not valid JSONC", encoding="utf-8")
    write_config("other.jsonc", {"network": {"proxy": {"mode": "direct"}}})
    write_config("entry.jsonc", {}, parent="other.jsonc")
    reads = Counter()
    restored = graph_factory(entry, source_documents=documents, reads=reads)
    actual = restored.resolve_scope()
    assert thaw(actual.settings) == thaw(scope.settings)
    assert [node.node_id for node in actual.chain] == [node.node_id for node in scope.chain]
    assert actual.origins.locate(("network", "proxy", "mode")) == scope.origins.locate(("network", "proxy", "mode"))
    assert not reads and not restored.dependencies
    assert set(restored.source_locations.values()) == {entry, parent}
    assert restored.source_documents == documents


def test_fixed_sources_never_fill_a_missing_definition_from_current_files(write_config, graph_factory):
    parent = write_config("parent.jsonc", {"network": {"proxy": {"mode": "direct"}}})
    entry = write_config("entry.jsonc", {}, parent="parent.jsonc")
    original = graph_factory(entry)
    original.resolve_scope()
    documents = {uri: document for uri, document in original.source_documents.items()
                 if original.source_locations[uri] != parent}
    reads = Counter()
    with pytest.raises(ValueError, match="固定配置快照缺少来源定义"):
        graph_factory(entry, source_documents=documents, reads=reads).resolve_scope()
    assert not reads


def test_different_files_cannot_share_one_source_uri(write_config, graph_factory):
    write_config("a.b.jsonc", {})
    # 此装具 URI 生成器会将点与下划线映射为相同字符，必须在源身份边界发现冲突。
    entry = write_config("a_b.jsonc", {}, parent="a.b.jsonc")
    with pytest.raises(ValueError, match="同一来源 URI"):
        graph_factory(entry).resolve_scope()
