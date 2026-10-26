"""真实封存历史的24→25维护升级与旧选择基准。"""

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage.schema import (
    SKILL_SELECTION_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema24,
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,  # noqa: PLC0414 - 复用真实准入与封存 fixture
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as epoch_db,  # noqa: PLC0414 - 按当前文件隔离工作区
)


def test_schema24_upgrade_preserves_sealed_bytes_and_current_skill(epoch_case):
    case = epoch_case
    case.load_snapshot()
    original = case.snapshot(case.prepare("before-selection-migration"))
    case.checkpoint("selection-migration-checkpoint")
    old_bytes = canonical_json_bytes(original.to_dict())
    storage = case.saver._storage.storage
    path = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    old_jsonl = path.read_bytes()
    restore_schema24(case.connection)
    storage.migrate_schema(
        case.owner.thread_id, session_id=case.owner.session_id, to_version=25,
        migration_name="skill_selection_024_025", migration_sql=SKILL_SELECTION_MIGRATION_SQL,
    )
    assert path.read_bytes() == old_jsonl
    assert canonical_json_bytes(case.snapshot({"assembly_id": original.assembly_id}).to_dict()) == old_bytes
    assert all(state.selection_sequence == state.latest_visible_selection_sequence == 0
               for state in case.saver.load_context_source_control_states(case.owner))
    case.restart()
    case.load_snapshot()
    assert case.manager.load_skill("one-shot").append_status == "already_active"
    assert case.manager.prepare_pending() is None
    assert case.snapshot(case.prepare("after-selection-migration")).seal_binding.prefix.state.frames == original.seal_binding.prefix.state.frames
