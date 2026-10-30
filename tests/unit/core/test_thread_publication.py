"""发布后失败与同 key 恢复不能重新进入发布前清理。"""

from __future__ import annotations

from collections.abc import Callable

import portalocker
import pytest

from app.core import thread_creation
from app.core.session_control_store import ThreadCreationRecord
from app.core.thread_creation import ThreadCreationService
from tests.unit.core.test_thread_creation import (
    OwnerSession,
    child_node_inventory,
    count_rows,
    make_artifacts,
    materialize_node,
    prepare_manual_record,
)
from tests.unit.core.test_thread_creation import (
    owner as owner,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import (
    service as service,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import (
    sessions_root as sessions_root,  # noqa: PLC0414
)
from tests.unit.core.test_thread_creation import (
    store as store,  # noqa: PLC0414
)


@pytest.mark.parametrize("phase_failure", [False, True])
@pytest.mark.parametrize("committed_error", [False, True])
async def test_post_publication_error_propagates_and_same_key_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
    monkeypatch: pytest.MonkeyPatch,
    phase_failure: bool,
    committed_error: bool,
) -> None:
    failure = RuntimeError("发布后确认失败")
    confirmed: list[str] = []
    prepared = prepare_manual_record(owner, key="publication-failure")
    final = owner.session_dir / prepared.final_relative_locator
    materialize_node(final, prepared, owner, make_artifacts())
    inventory = child_node_inventory(prepared, owner, make_artifacts())

    def assert_gates_held() -> None:
        paths = (service._gate.lock_path, service._session_gate.exclusive(owner.session_id).path)
        for path in paths:
            with path.open("rb") as file, pytest.raises(portalocker.AlreadyLocked):
                portalocker.lock(file, portalocker.LOCK_EX | portalocker.LOCK_NB)

    def confirm(record: ThreadCreationRecord) -> None:
        assert_gates_held()
        assert record.state == "published"
        confirmed.append(record.child_thread_id)
        if not phase_failure:
            raise failure

    async def observe(operation_id: str, phase: str) -> None:
        if phase == "thread.creation.published":
            assert operation_id == "publication-failure"
            raise failure

    async def create(callback: Callable[[ThreadCreationRecord], None]):
        return await service._publish_and_finalize(
            owner.control.get_thread_creation_record("publication-failure"),
            session_id=owner.session_id, session_dir=owner.session_dir,
            final_dir=final, inventory=inventory,
            on_published=callback,
        )

    with monkeypatch.context() as patch:
        if committed_error:
            original_publish = owner.control.publish_thread_creation_record

            def publish_without_ack(key: str, *, session_id: str) -> ThreadCreationRecord:
                original_publish(key, session_id=session_id)
                raise RuntimeError("提交已完成，但原确认丢失")

            patch.setattr(owner.control, "publish_thread_creation_record", publish_without_ack)
        if phase_failure:
            patch.setattr(thread_creation, "observe_phase", observe)
        with pytest.raises(RuntimeError) as caught:
            await create(confirm)
        assert caught.value is failure
    record = owner.control.get_thread_creation_record("publication-failure")
    assert record.state == "published" and record.abort_reason is None
    assert final.is_dir()
    assert not (owner.session_dir / record.staging_locator).exists()
    assert confirmed == [record.child_thread_id]
    # 失败已释放原 gate；同一操作的恢复仍可重新取得两锁。
    async with service._gate.shared(), service._session_gate.exclusive(owner.session_id):
        pass

    def recovered(current: ThreadCreationRecord) -> None:
        assert_gates_held()
        assert current == record
        confirmed.append(current.child_thread_id)

    result = await create(recovered)
    assert result.child_thread_id == record.child_thread_id
    assert confirmed == [record.child_thread_id] * 2
    assert owner.control.get_thread_creation_record("publication-failure") == record
    assert count_rows(owner.control, "thread_catalog") == 2
    assert count_rows(owner.control, "thread_creation_records") == 1
    assert count_rows(owner.control, "thread_execution_intents") == 1
