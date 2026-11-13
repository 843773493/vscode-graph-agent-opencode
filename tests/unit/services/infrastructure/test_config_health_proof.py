from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import load_or_create_workspace_id
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config.state import SecretResolutionError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def test_active_health_proof_does_not_resolve_unused_provider_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "selected-provider-secret")
    monkeypatch.delenv("KILO_API_KEY", raising=False)
    workspace_root = tmp_path / "workspace"
    config_path = tmp_path / "workspace.jsonc"
    config_dir = Path.cwd() / "configs"

    with WorkspaceStateStore(workspace_root=workspace_root) as store:
        initial = ConfigService(
            config_dir=config_dir,
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        initial.validate_workspace_config()

    with WorkspaceStateStore(workspace_root=workspace_root) as store:
        service = ConfigService(
            config_dir=config_dir,
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )

        proof = service.get_loaded_config_proof()

        assert proof["loaded_source"] == "active"
        assert proof["secret_binding_digest"]
        assert service.get_llm_provider("primary")["api_key"] == "selected-provider-secret"
        with pytest.raises(ValueError, match="KILO_API_KEY 未设置"):
            service.get_llm_provider("backup_1")


@pytest.mark.asyncio
async def test_pending_health_proof_still_resolves_current_secret_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_names = (
        "OPENCODE_ZEN_API_KEY",
        "KILO_API_KEY",
        "OPENROUTER_API_KEY",
        "CCTQ_API_KEY",
        "DEEPSEEK_V41_FLASH_API_KEY",
    )
    for secret_name in secret_names:
        monkeypatch.setenv(secret_name, f"test-value-{secret_name}")

    workspace_root = tmp_path / "workspace"
    config_path = tmp_path / "workspace.jsonc"
    config_dir = Path.cwd() / "configs"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=config_dir,
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config_path.write_text(
            json.dumps(
                {"mcp": {"supervision": {"sealed_call_timeout_seconds": 2000}}}
            ),
            encoding="utf-8",
        )

        async def require_restart(*_args: object) -> None:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)

        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.candidate_ref is not None
        assert pending.target_generation is not None
        assert pending.fencing_token is not None
        candidate_ref = pending.candidate_ref
        generation = pending.target_generation
        fencing_token = pending.fencing_token
    finally:
        store.close()

    startup_mount = MountedWorkspace(
        load_or_create_workspace_id(workspace_root),
        workspace_root.resolve(),
        config_candidate_ref=candidate_ref,
        config_generation=generation,
        config_fencing_token=fencing_token,
    )
    with WorkspaceStateStore(workspace_root=workspace_root) as restored_store:
        restored = ConfigService(
            config_dir=config_dir,
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=restored_store,
            startup_mount=startup_mount,
        )
        restored.get_snapshot()
        monkeypatch.delenv("KILO_API_KEY", raising=False)

        with pytest.raises(SecretResolutionError, match="KILO_API_KEY"):
            restored.get_loaded_config_proof()
