from __future__ import annotations

import json
from pathlib import Path

from app.core.config_sources import read_stable_config_file
from app.services.infrastructure.config import WorkspaceSourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def _write_config(path: Path, *, logger_level: str) -> None:
    path.write_text(
        json.dumps(
            {
                "config_version": 1,
                "llm": {
                    "providers": [
                        {
                            "id": "primary",
                            "endpoint": "https://example.com/v1",
                            "model": "model-a",
                            "api_key": "${TEST_API_KEY}",
                            "custom_llm_provider": "openai",
                            "api_mode": {
                                "protocol": "chat_completions",
                                "model_info": {
                                    "supports_function_calling": True,
                                    "supports_reasoning": True,
                                },
                                "supports_reasoning": {"reasoning_content": True},
                            },
                        }
                    ]
                },
                "logger": {"level": logger_level, "pretty": True},
                "default_agent": "default",
                "agents": {
                    "default": {
                        "name": "Default Agent",
                        "instructions": {"system_prompt": "hello"},
                        "model": {
                            "primary_provider": "primary",
                            "fallback_providers": [],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_shared_source_owner_distinguishes_a_b_a_and_deduplicates_adjacent_observation(
    tmp_path: Path,
) -> None:
    first_owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    second_owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    source_path = tmp_path / "workspace.jsonc"

    first = first_owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    duplicate = second_owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    second = first_owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-b",
        origin="file-watcher",
    )
    third = second_owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )

    assert duplicate == first
    assert (first.source_generation, second.source_generation, third.source_generation) == (
        1,
        2,
        3,
    )
    assert first.fanout_id != third.fanout_id
    assert first.source_event_id != third.source_event_id
    assert first_owner.high_water_mark() == 3


def test_shared_source_owner_prepares_stopped_workspace_and_preserves_failed_result(
    tmp_path: Path,
) -> None:
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    source_path = tmp_path / "workspace.jsonc"
    first = owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-a",
        origin="file-watcher",
    )
    second = owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-b",
        origin="file-watcher",
    )

    replay = owner.prepare_fanout(workspace_id="workspace-b")
    assert [record.source_generation for record in replay] == [1, 2]
    owner.record_fanout(
        source_generation=first.source_generation,
        workspace_id="workspace-b",
        status="superseded",
        result="superseded",
    )
    owner.record_fanout(
        source_generation=second.source_generation,
        workspace_id="workspace-b",
        status="conflict",
        result="conflict",
        error="本地 Workspace layer 已变化",
    )
    third = owner.observe(
        source_path=source_path,
        presence="present",
        layer_digest="digest-c",
        origin="file-watcher",
    )
    owner.prepare_fanout(workspace_id="workspace-b")
    summary = owner.summary(
        workspace_ids=("workspace-b",),
        source_generation=third.source_generation,
    )

    assert summary["result"] == "fanout_partial"
    assert summary["pending_workspace_ids"] == ("workspace-b",)
    assert owner.get_fanout(
        source_key="workspace:user",
        source_generation=second.source_generation,
        workspace_id="workspace-b",
    ).status == "conflict"


async def test_config_service_materializes_shared_user_source_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, logger_level="info")
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    first_store = WorkspaceStateStore(workspace_root=tmp_path / "workspace-a")
    second_store = WorkspaceStateStore(workspace_root=tmp_path / "workspace-b")
    first_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=tmp_path / "workspace-a",
        workspace_state_store=first_store,
        source_owner=owner,
        source_owner_workspace_id="workspace-a",
    )
    second_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=tmp_path / "workspace-b",
        workspace_state_store=second_store,
        source_owner=owner,
        source_owner_workspace_id="workspace-b",
    )
    try:
        first_service.get_revision()
        second_service.get_revision()
        assert owner.high_water_mark() == 1
        assert first_store.get_source_layer("workspace_mutable_override").source_generation == 1
        assert second_store.get_source_layer("workspace_mutable_override").source_generation == 1

        _write_config(config_path, logger_level="warning")
        assert await first_service.reload() is True
        assert await second_service.reload() is True
        assert owner.high_water_mark() == 2

        _write_config(config_path, logger_level="info")
        assert await first_service.reload() is True
        assert await second_service.reload() is True
        assert owner.high_water_mark() == 3
        assert first_store.get_source_layer("workspace_mutable_override").source_generation == 3
        assert second_store.get_source_layer("workspace_mutable_override").source_generation == 3
        assert owner.summary(
            workspace_ids=("workspace-a", "workspace-b"),
            source_generation=3,
        )["result"] == "applied"
    finally:
        first_store.close()
        second_store.close()
        owner.close()


def test_config_service_catches_up_stopped_workspace_from_source_high_water(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = tmp_path / "workspace.jsonc"
    _write_config(config_path, logger_level="info")
    owner = WorkspaceSourceOwner(path=tmp_path / "source-owner.sqlite")
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=tmp_path / "workspace",
            workspace_state_store=store,
            source_owner=owner,
            source_owner_workspace_id="stopped-workspace",
        )
        service.get_revision()
        assert store.get_source_layer("workspace_mutable_override").source_generation == 1

        _write_config(config_path, logger_level="warning")
        warning_digest = read_stable_config_file(config_path).digest
        assert warning_digest is not None
        owner.observe(
            source_path=config_path,
            presence="present",
            layer_digest=warning_digest,
            origin="file-watcher",
        )
        _write_config(config_path, logger_level="info")
        restored_digest = read_stable_config_file(config_path).digest
        assert restored_digest is not None
        owner.observe(
            source_path=config_path,
            presence="present",
            layer_digest=restored_digest,
            origin="file-watcher",
        )
        assert owner.high_water_mark() == 3

        restarted = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=tmp_path / "workspace",
            workspace_state_store=store,
            source_owner=owner,
            source_owner_workspace_id="stopped-workspace",
        )
        restarted.get_revision()

        assert store.get_source_layer("workspace_mutable_override").source_generation == 3
        assert owner.get_fanout(
            source_key="workspace:user",
            source_generation=2,
            workspace_id="stopped-workspace",
        ).status == "superseded"
        assert owner.get_fanout(
            source_key="workspace:user",
            source_generation=3,
            workspace_id="stopped-workspace",
        ).status == "applied"
    finally:
        store.close()
        owner.close()
