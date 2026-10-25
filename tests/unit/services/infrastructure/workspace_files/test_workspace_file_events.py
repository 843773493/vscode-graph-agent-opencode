from __future__ import annotations

import asyncio
from unittest.mock import Mock
from uuid import uuid4

from app.core.resource_uri import file_uri_child
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
)
from app.services.infrastructure.workspace_files.events import file_events
from app.services.infrastructure.workspace_service import WorkspaceService


async def test_operation_staging_watch_events_are_hidden_after_cleanup(
    empty_workspace_scope, monkeypatch
):
    _, workspace = empty_workspace_scope
    target = workspace / "target"
    target.mkdir(parents=True)
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    service = WorkspaceService(config_service=config, workspace_root=workspace)
    await service.start()
    owner = service.file_operations
    try:
        listing = await service.list_files(path="target")
        intent = WorkspaceFileMutationIntentDTO(
            client_operation_id="op_" + uuid4().hex,
            client_sequence=1,
            directory_key=listing.directory_key,
            expected_directory_revision=listing.directory_revision,
        )
        accepted = await owner.enqueue(
            kind="create",
            directory_path="target",
            scope="workspace",
            intent=intent,
            actor="local",
            data={"name": "created.txt", "kind": "file"},
        )
        for _ in range(300):
            receipt = owner.status([accepted.operation_id], actor="local").operations[0]
            if receipt.state == "committed" and not owner.staging_roots():
                break
            if owner.worker_state == "failed":
                raise RuntimeError(owner.worker_error)
            await asyncio.sleep(0.01)
        assert receipt.state == "committed"
        assert not owner.staging_roots()

        operation = owner.store.get(accepted.operation_id, "local")
        staging_root = owner._stage_root(operation, 0)
        changed_path = target / "external.txt"
        changed_path.write_text("external change", encoding="utf-8")
        observe_directories = Mock(wraps=owner.observe_directories)
        monkeypatch.setattr(owner, "observe_directories", observe_directories)
        watcher_batches = [
            WorkspaceFileChangeBatch(
                changes=(WorkspaceFileChange(kind=kind, path=str(path)),)
            )
            for kind, path in (
                ("create", staging_root),
                ("create", staging_root / "owner.json"),
                ("create", staging_root / "entry"),
                ("delete", staging_root),
            )
        ]
        watcher_batches.append(
            WorkspaceFileChangeBatch(
                changes=(WorkspaceFileChange(kind="create", path=str(changed_path)),)
            )
        )

        class OneBatchWatcher:
            async def subscribe_roots(self, _roots):
                for batch in watcher_batches:
                    yield batch

        stream = file_events(
            owner,
            OneBatchWatcher(),
            (workspace,),
            after_event_id=0,
            references={workspace: owner.paths.workspace_uri},
        )
        second_stream = file_events(
            owner,
            OneBatchWatcher(),
            (workspace,),
            after_event_id=0,
            references={workspace: owner.paths.workspace_uri},
        )
        try:
            terminal_events = await asyncio.gather(anext(stream), anext(second_stream))
            assert [event.event_id for event in terminal_events] == [1, 1]
            watcher_events = await asyncio.gather(anext(stream), anext(second_stream))
        finally:
            await asyncio.gather(stream.aclose(), second_stream.aclose())

        expected_path = file_uri_child(
            owner.paths.workspace_uri, "target", "external.txt"
        )
        assert all(
            [change.path for change in event.changes] == [expected_path]
            for event in watcher_events
        )
        observe_directories.assert_called_once()
    finally:
        await service.stop()
        service.close()
