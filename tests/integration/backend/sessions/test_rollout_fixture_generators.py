"""生成器使用受控 Provider 替身，但真实写入 canonical SessionThread 与 admission。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.core.path_utils import get_session_path_resolver
from scripts import generate_real_model_rollout_fixture as generator
from tests.harness.python.run_context import TestRunContext
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
def real_generator_case(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    bundle = seed_catalog_session_bundle(
        workspace / ".boxteam" / "sessions", generator.SESSION_ID
    )
    providers = [
        {
            "id": identity, "model": "fixture-model",
            "endpoint": "http://provider-stub.invalid/v1",
            "api_key": "fixture-key", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions"},
        }
        for identity in ("provider-a", "provider-b")
    ]

    class FixtureConfig:
        def __init__(self, **_kwargs):
            pass

        def validate_workspace_config(self):
            pass

        def get_agent_runtime_config(self, _agent_id, **_kwargs):
            return {"providers": providers}

    def build_model(provider, _runtime):
        return FakeMessagesListChatModel(responses=[
            AIMessage(id=f"final-{provider['id']}", content="受控模型正文")
        ])

    monkeypatch.setattr(generator, "ConfigService", FixtureConfig)
    monkeypatch.setattr(generator, "_load_running_provider_keys", lambda _names: None)
    monkeypatch.setattr(generator, "build_model_from_provider", build_model)
    return workspace, bundle


def test_provider_stub_generator_writes_real_canonical_owner_and_admissions(
    real_generator_case,
) -> None:
    workspace, bundle = real_generator_case
    generator.generate(workspace, end_turn=2, tool_every=0)
    manifest = json.loads((bundle.directory / "session.json").read_text(encoding="utf-8"))
    assert manifest["workspace_id"] == bundle.node.workspace_id
    assert set(manifest) == {"session_id", "workspace_id", "created_at"}
    resolver = get_session_path_resolver(workspace / ".boxteam" / "sessions")
    assert resolver.catalog_store.get_node(bundle.session_id).current_provider_id == "provider-b"
    rollout = resolver.resolve_thread_node(bundle.session_id, bundle.main_thread_id) / "rollout"
    assert rollout.is_dir()
    with sqlite3.connect(rollout / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT DISTINCT session_id, thread_id FROM executions"
        ).fetchall() == [(bundle.session_id, bundle.main_thread_id)]
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_records WHERE status = 'completed'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_acceptances"
        ).fetchone() == (2,)

    backup = rollout.parent / ".rollout-backup-fixture"
    shutil.copytree(rollout, backup)
    (rollout / "failed-generation").write_text("失败产物", encoding="utf-8")
    generator._restore_generation_backup(workspace)
    assert not backup.exists()
    assert not (rollout / "failed-generation").exists()
    assert (rollout / "index.sqlite").is_file()
