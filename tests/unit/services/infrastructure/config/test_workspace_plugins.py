from __future__ import annotations

import pytest

from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings,
)

WORKSPACE_ID = "12345678-1234-5678-1234-567812345678"


def test_workspace_plugin_bindings_inject_owner_identity() -> None:
    bindings = workspace_plugin_bindings(
        {"plugins": {"runtime": {"entries": [
            {
                "package_id": "sample-tools",
                "plugin_id": "browser",
                "package_version": "1.2.0",
                "enabled": True,
            },
        ]}}},
        workspace_id=WORKSPACE_ID,
    )

    assert len(bindings) == 1
    assert bindings[0].workspace_id == WORKSPACE_ID
    assert bindings[0].enabled is True


def test_workspace_plugin_bindings_reject_configured_owner_and_extra_fields() -> None:
    base = {
        "package_id": "sample-tools",
        "plugin_id": "browser",
        "package_version": "1.2.0",
        "enabled": True,
    }

    with pytest.raises(ValueError, match="不得自行填写 workspace_id"):
        workspace_plugin_bindings(
            {"plugins": {"runtime": {"entries": [{**base, "workspace_id": WORKSPACE_ID}]}}},
            workspace_id=WORKSPACE_ID,
        )

    with pytest.raises(ValueError):
        workspace_plugin_bindings(
            {"plugins": {"runtime": {"entries": [{**base, "unknown": True}]}}},
            workspace_id=WORKSPACE_ID,
        )


def test_workspace_plugin_bindings_default_to_empty() -> None:
    assert workspace_plugin_bindings({}, workspace_id=None) == []
