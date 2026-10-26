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
            ConfigPolicyRule("/runtime/**", "next_job", "next_job"),
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
                ConfigPolicyRule("/ui/*", "immediate_read", "current"),
                ConfigPolicyRule("/ui/*", "next_job", "next_job"),
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


def test_single_level_policy_does_not_admit_unknown_server_fields():
    single = ConfigPolicyRule("/mcp/servers/*", "immediate_read", "current")
    assert single.matches("/mcp/servers/alpha")
    assert not single.matches("/mcp/servers/alpha/unknown")
    recursive = ConfigPolicyRule("/agents/*/instructions/**", "next_job", "next_job")
    assert recursive.matches("/agents/coder/instructions/system/path")
    assert not recursive.matches("/agents/coder/tools/system/path")


@pytest.mark.parametrize("field", [
    "enabled", "transport", "command", "args", "env", "cwd", "url", "headers",
    "env/ROOT~1~0", "headers/Authorization",
])
def test_supported_mcp_connection_changes_use_declared_hot_policy(field):
    path = f"/mcp/servers/alpha/{field}"
    decision, = workspace_config_policy().classify((path,))
    assert decision.policy == "immediate_read" and decision.activation_scope == "current"
    assert not decision.policy_missing


@pytest.mark.parametrize("path", [
    "/mcp/servers/alpha/unknown", "/mcp/servers/alpha/auth",
    "/mcp/servers/alpha/url/nested", "/mcp/servers/alpha/env/ROOT/nested",
    "/mcp/supervision/sealed_call_timeout_seconds",
])
def test_unverified_mcp_paths_keep_restart_classification(path):
    registry = workspace_config_policy()
    decision, = registry.classify((path,))
    assert decision.policy == "restart_workspace" and decision.policy_missing
    assert registry.activation_scope_for(("/mcp/servers/alpha/url", path)) == "restart_workspace"


def test_mcp_server_add_remove_uses_the_real_diff_without_recursive_admission():
    before = {"mcp": {"servers": {"alpha": {"transport": "stdio", "command": "old"}}}}
    after = {"mcp": {"servers": {"beta": {"transport": "stdio", "command": "new"}}}}
    paths = changed_json_paths(before, after)
    assert paths == ("/mcp/servers/alpha", "/mcp/servers/beta")
    assert workspace_config_policy().restart_paths(paths) == ()


def test_recursive_policy_cannot_hide_unmatched_levels():
    with pytest.raises(ValueError, match="只能位于末尾"):
        ConfigPolicyRegistry(domain="workspace", default_policy="restart_workspace", rules=(
            ConfigPolicyRule("/mcp/**/url", "immediate_read", "current"),
        ))
