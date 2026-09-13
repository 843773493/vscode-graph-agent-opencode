from __future__ import annotations

from pathlib import Path

import pytest

from app.services.infrastructure.config.policy import (
    ConfigPolicyRegistry,
    ConfigPolicyRule,
    gateway_config_policy,
    validate_policy_manifest,
    workspace_config_policy,
)
from app.services.infrastructure.config.state import changed_json_paths
from configs.runtime import read_jsonc_object


def test_config_policy_prefers_longest_explicit_path_and_marks_missing_defaults():
    registry = ConfigPolicyRegistry(
        domain="workspace",
        default_policy="restart_workspace",
        rules=(
            ConfigPolicyRule("/runtime/*", "next_job", "next_job"),
            ConfigPolicyRule(
                "/runtime/agent/run/timeout_seconds",
                "next_session",
                "next_session",
            ),
        ),
    )
    decisions = registry.classify(
        (
            "/runtime/agent/run/timeout_seconds",
            "/unknown/value",
        )
    )
    assert decisions[0].policy == "next_session"
    assert decisions[0].policy_missing is False
    assert decisions[1].policy == "restart_workspace"
    assert decisions[1].policy_missing is True


def test_builtin_policies_cover_workspace_and_gateway_restart_boundaries():
    assert workspace_config_policy().restart_paths(("/mcp/tools",)) == ("/mcp/tools",)
    assert workspace_config_policy().restart_paths(("/llm/providers",)) == ()
    assert workspace_config_policy().activation_scope_for(
        ("/llm/providers",)
    ) == "next_job"
    assert workspace_config_policy().activation_scope_for(
        ("/runtime/gateway/connection/url",)
    ) == "next_job"
    assert gateway_config_policy().restart_paths(("/workspaces/0/host",)) == (
        "/workspaces/0/host",
    )
    assert gateway_config_policy().restart_paths(("/ui/theme/default_theme_id",)) == ()


def test_activation_scope_exposes_mixed_and_restart_candidate_boundaries():
    assert workspace_config_policy().activation_scope_for(
        ("/runtime/agent/run/timeout_seconds",)
    ) == "next_job"
    assert gateway_config_policy().activation_scope_for(
        ("/ui/theme/default_theme_id", "/features/session_catalog/enabled")
    ) == "mixed"
    assert workspace_config_policy().activation_scope_for(
        ("/ui/theme", "/logger")
    ) == "restart_workspace"


def test_changed_json_paths_uses_declared_array_identity_or_array_root():
    previous = {"workspaces": [{"connection_id": "a", "host": "old"}]}
    current = {"workspaces": [{"connection_id": "a", "host": "new"}]}
    assert changed_json_paths(previous, current) == ("/workspaces",)
    assert changed_json_paths(
        previous,
        current,
        array_identity_keys=gateway_config_policy().array_identity_keys(),
    ) == ("/workspaces/a/host",)
    assert changed_json_paths(
        {"workspaces": [{"connection_id": "a"}, {"connection_id": "b"}]},
        {"workspaces": [{"connection_id": "b"}, {"connection_id": "a"}]},
        array_identity_keys=gateway_config_policy().array_identity_keys(),
    ) == ("/workspaces",)
    assert gateway_config_policy().restart_paths(("/workspaces",)) == ("/workspaces",)
    assert gateway_config_policy().array_identity_keys() == {
        "/workspaces": "connection_id"
    }


def test_config_policy_rejects_duplicate_patterns():
    with pytest.raises(ValueError, match="重复登记"):
        ConfigPolicyRegistry(
            domain="gateway",
            default_policy="restart_gateway",
            rules=(
                ConfigPolicyRule("/ui/*", "immediate_read", "current_view"),
                ConfigPolicyRule("/ui/*", "next_job", "next_operation"),
            ),
        )


def test_schema_policy_manifests_match_runtime_policy_registries():
    for schema_path, registry in (
        ("configs/workspace_schema.jsonc", workspace_config_policy()),
        ("configs/gateway_schema.jsonc", gateway_config_policy()),
    ):
        schema = read_jsonc_object(Path(schema_path))
        validate_policy_manifest(schema, registry)


def test_schema_policy_manifest_rejects_drift():
    schema = read_jsonc_object(Path("configs/gateway_schema.jsonc"))
    manifest = schema["x-boxteam-policy-manifest"]
    assert isinstance(manifest, list)
    manifest[0] = {**manifest[0], "policy": "next_job"}
    with pytest.raises(ValueError, match="策略表不一致"):
        validate_policy_manifest(schema, gateway_config_policy())
