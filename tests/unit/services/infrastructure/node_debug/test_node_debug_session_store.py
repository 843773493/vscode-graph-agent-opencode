"""调试存储的失败诊断保留逻辑归属，不暴露原生位置或损坏正文。"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from app.core.atomic_fs import native_io_path
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

_CONFIGURATION_ID = "dbgcfg_" + "1" * 32
_OTHER_CONFIGURATION_ID = "dbgcfg_" + "2" * 32
_READERS = ("manifest", "claim", "configuration", "registered", "listed")
ReaderKind = Literal["manifest", "claim", "configuration", "registered", "listed"]


@dataclass(frozen=True)
class DebugStore:
    store: NodeDebugSessionStore
    session_id: str
    thread_id: str
    directory: Path

    def reader(self, kind: ReaderKind) -> tuple[Path, Callable[[], object]]:
        owner = (self.session_id, self.thread_id)
        if kind == "manifest":
            return self.directory / "manifest.json", lambda: self.store.read_manifest_payload(*owner)
        if kind == "claim":
            return self.directory / "launch-claim.json", lambda: self.store.read_launch_claim(*owner)
        path = self.directory / "configurations" / f"{_CONFIGURATION_ID}.json"
        if kind == "configuration":
            return path, lambda: self.store.read_configuration(self.session_id, _CONFIGURATION_ID, self.thread_id)
        if kind == "registered":
            return path, lambda: self.store.read_registered_configuration_payloads(*owner, (_CONFIGURATION_ID,))
        return path, lambda: self.store.list_configurations(*owner)


@pytest.fixture
async def debug_store() -> AsyncIterator[DebugStore]:
    run_context = TestRunContext.from_test_file(Path(__file__))
    prepare_test_workspace(
        workspace_root=run_context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    with build_catalog_workspace(run_context.output_root) as workspace:
        session_id = await workspace.create_session("调试存储诊断")
        thread_id = workspace.resolver.main_thread_id(session_id)
        store = NodeDebugSessionStore(workspace.resolver)
        now = datetime.now(UTC)
        store.write_configuration(session_id, NodeDebugConfigurationDTO(
            configuration_id=_CONFIGURATION_ID, name="主方案", created_at=now, updated_at=now,
        ), thread_id)
        store.write_manifest(NodeDebugSessionManifestDTO(
            session_id=session_id, thread_id=thread_id, updated_at=now,
        ))
        store.write_launch_claim(NodeDebugLaunchClaimDTO(
            session_id=session_id, thread_id=thread_id, process_instance_id="process-1",
            nonce="nonce-1", phase="settled", configuration_id=_CONFIGURATION_ID,
            created_at=now, updated_at=now,
        ))
        directory = native_io_path(workspace.resolver.resolve_thread_node(session_id, thread_id) / "debug/node")
        yield DebugStore(store, session_id, thread_id, directory)


def test_cold_store_reads_updates_and_deletes_with_long_configuration_path(debug_store: DebugStore) -> None:
    owner = (debug_store.session_id, debug_store.thread_id)
    configuration_path = debug_store.directory / "configurations" / f"{_CONFIGURATION_ID}.json"
    assert len(str(configuration_path)) > 260
    cold = NodeDebugSessionStore(debug_store.store.path_resolver)
    manifest_bytes, manifest = cold.read_manifest_payload(*owner)
    assert manifest is not None and manifest_bytes is not None
    assert manifest.configuration_ids == (_CONFIGURATION_ID,)
    assert cold.read_launch_claim(*owner) == debug_store.store.read_launch_claim(*owner)
    registered = cold.read_registered_configuration_payloads(*owner, manifest.configuration_ids)
    configuration, payload = registered[_CONFIGURATION_ID]
    assert cold.list_configurations(*owner) == [configuration]
    assert cold.read_configuration(owner[0], _CONFIGURATION_ID, owner[1]) == configuration
    assert payload == configuration_path.read_bytes()

    updated = cold.write_configuration(owner[0], configuration.model_copy(update={"name": "更新方案"}), owner[1])
    reopened = NodeDebugSessionStore(cold.path_resolver)
    assert reopened.read_configuration(owner[0], _CONFIGURATION_ID, owner[1]) == updated
    reopened.delete_configuration(owner[0], _CONFIGURATION_ID, owner[1])
    assert reopened.read_configuration(owner[0], _CONFIGURATION_ID, owner[1]) is None
    assert reopened.list_configurations(*owner) == []
    reopened.write_manifest(manifest)
    empty = NodeDebugSessionStore(reopened.path_resolver).read_manifest(*owner)
    assert empty is not None and empty.configuration_ids == ()
    assert not list((debug_store.directory / "configurations").iterdir())


def _assert_private_failure(error: Exception, fixture: DebugStore, original: bytes, path: Path) -> None:
    message = str(error)
    assert fixture.session_id in message
    assert fixture.thread_id in message
    assert str(fixture.directory) not in message
    assert str(path) not in message
    assert path.read_bytes() == original, "诊断不能覆写或修复损坏原件"


@pytest.mark.parametrize("kind", _READERS)
@pytest.mark.parametrize("corruption", ["json", "schema", "extra"])
def test_read_failure_preserves_owner_and_cause_without_path_or_input(
    debug_store: DebugStore, kind: ReaderKind, corruption: str,
) -> None:
    path, read = debug_store.reader(kind)
    if corruption == "json":
        original = b"{"
    else:
        payload = json.loads(path.read_bytes())
        if corruption == "schema":
            payload["updated_at"] = [str(debug_store.directory / "private-input")]
        else:
            payload[str(debug_store.directory / "private-input")] = "invalid"
        original = json.dumps(payload).encode()
    path.write_bytes(original)
    with pytest.raises(RuntimeError) as caught:
        read()
    _assert_private_failure(caught.value, debug_store, original, path)
    assert "private-input" not in str(caught.value)
    assert caught.value.__cause__ is not None
    assert {
        "json": "json_invalid", "schema": "updated_at", "extra": "extra_forbidden",
    }[corruption] in str(caught.value)


@pytest.mark.parametrize("kind", _READERS)
def test_read_error_exposes_stage_errno_and_owner_without_native_location(
    debug_store: DebugStore, kind: ReaderKind, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, read = debug_store.reader(kind)
    original = path.read_bytes()
    read_bytes = Path.read_bytes

    def denied(candidate: Path) -> bytes:
        if candidate == path:
            raise PermissionError(errno.EACCES, "denied", str(candidate))
        return read_bytes(candidate)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_bytes", denied)
        with pytest.raises(PermissionError) as caught:
            read()
    _assert_private_failure(caught.value, debug_store, original, path)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert "read" in str(caught.value) and "errno=13" in str(caught.value)


@pytest.mark.parametrize("kind", _READERS)
@pytest.mark.parametrize("dangling", [False, True])
def test_symbolic_file_is_rejected_without_reading_or_publishing_native_target(
    debug_store: DebugStore, kind: ReaderKind, dangling: bool,
) -> None:
    path, read = debug_store.reader(kind)
    original = path.read_bytes()
    target = debug_store.directory / "private-target"
    if not dangling:
        target.write_bytes(original)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(RuntimeError) as caught:
        read()
    assert debug_store.session_id in str(caught.value) and debug_store.thread_id in str(caught.value)
    assert str(debug_store.directory) not in str(caught.value)
    assert path.is_symlink()
    assert target.exists() is not dangling
    if not dangling:
        assert target.read_bytes() == original


@pytest.mark.parametrize("kind", ["manifest", "claim", "configuration", "registered", "listed"])
def test_owner_or_configuration_mismatch_keeps_identity_without_file_location(
    debug_store: DebugStore, kind: ReaderKind,
) -> None:
    path, read = debug_store.reader(kind)
    payload = json.loads(path.read_bytes())
    payload["session_id" if kind in {"manifest", "claim"} else "configuration_id"] = (
        "ses_00000000400070008000000000000002" if kind in {"manifest", "claim"} else _OTHER_CONFIGURATION_ID
    )
    original = json.dumps(payload).encode()
    path.write_bytes(original)
    with pytest.raises(RuntimeError) as caught:
        read()
    _assert_private_failure(caught.value, debug_store, original, path)


def test_registered_missing_configuration_keeps_owner_without_recreating_file(debug_store: DebugStore) -> None:
    path, read = debug_store.reader("registered")
    path.unlink()
    with pytest.raises(RuntimeError) as caught:
        read()
    assert debug_store.session_id in str(caught.value) and debug_store.thread_id in str(caught.value)
    assert _CONFIGURATION_ID in str(caught.value)
    assert str(debug_store.directory) not in str(caught.value)
    assert not path.exists()


@pytest.mark.parametrize("kind", _READERS)
def test_directory_in_place_of_file_fails_without_native_location(
    debug_store: DebugStore, kind: ReaderKind,
) -> None:
    path, read = debug_store.reader(kind)
    path.unlink()
    path.mkdir()
    with pytest.raises(RuntimeError) as caught:
        read()
    assert debug_store.session_id in str(caught.value)
    assert debug_store.thread_id in str(caught.value)
    assert str(debug_store.directory) not in str(caught.value)
    assert path.is_dir()


@pytest.mark.parametrize("kind", ["manifest", "claim", "configuration", "listed"])
def test_missing_optional_data_keeps_existing_empty_semantics(
    debug_store: DebugStore, kind: ReaderKind,
) -> None:
    path, read = debug_store.reader(kind)
    path.unlink()
    assert read() == {"manifest": (None, None), "claim": None, "configuration": None, "listed": []}[kind]
    assert not path.exists()


@pytest.mark.parametrize("error_type, error_number", [
    (PermissionError, errno.EACCES), (FileNotFoundError, errno.ENOENT),
])
def test_directory_enumeration_error_is_explicit_and_preserves_cause(
    debug_store: DebugStore, monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError], error_number: int,
) -> None:
    directory = debug_store.directory / "configurations"
    iterdir = Path.iterdir

    def denied(candidate: Path):
        if candidate == directory:
            raise error_type(error_number, "enumeration failed", str(candidate))
        return iterdir(candidate)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(error_type) as caught:
        debug_store.store.list_configurations(debug_store.session_id, debug_store.thread_id)
    assert debug_store.session_id in str(caught.value)
    assert debug_store.thread_id in str(caught.value)
    assert "list" in str(caught.value) and f"errno={error_number}" in str(caught.value)
    assert str(directory) not in str(caught.value)
    assert isinstance(caught.value.__cause__, error_type)


@pytest.mark.parametrize("kind", _READERS)
def test_file_disappearing_during_read_preserves_missing_error_without_native_location(
    debug_store: DebugStore, kind: ReaderKind, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, read = debug_store.reader(kind)
    original = path.read_bytes()
    read_bytes = Path.read_bytes

    def disappeared(candidate: Path) -> bytes:
        if candidate == path:
            raise FileNotFoundError(errno.ENOENT, "missing", str(candidate))
        return read_bytes(candidate)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_bytes", disappeared)
        with pytest.raises(FileNotFoundError) as caught:
            read()
    assert debug_store.session_id in str(caught.value)
    assert debug_store.thread_id in str(caught.value)
    assert str(debug_store.directory) not in str(caught.value)
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert path.read_bytes() == original


def test_invalid_registered_id_is_not_echoed_as_a_storage_location(debug_store: DebugStore) -> None:
    invalid_id = str(debug_store.directory / "private-input")
    with pytest.raises(ValueError) as caught:
        debug_store.store.read_registered_configuration_payloads(
            debug_store.session_id, debug_store.thread_id, (invalid_id,),
        )
    assert debug_store.session_id in str(caught.value)
    assert debug_store.thread_id in str(caught.value)
    assert invalid_id not in str(caught.value)


@pytest.mark.parametrize("kind", ["manifest", "claim", "configuration"])
@pytest.mark.parametrize("stage", ["mkdir", "write", "replace"])
def test_write_io_failure_preserves_error_type_and_original_without_native_location(
    debug_store: DebugStore, kind: ReaderKind, stage: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _read = debug_store.reader(kind)
    original = path.read_bytes()
    owner = (debug_store.session_id, debug_store.thread_id)
    if kind == "manifest":
        manifest = debug_store.store.read_manifest(*owner)
        assert manifest is not None
        write = lambda: debug_store.store.write_manifest(manifest)
    elif kind == "claim":
        claim = debug_store.store.read_launch_claim(*owner)
        assert claim is not None
        write = lambda: debug_store.store.write_launch_claim(claim)
    else:
        configuration = debug_store.store.read_configuration(owner[0], _CONFIGURATION_ID, owner[1])
        assert configuration is not None
        write = lambda: debug_store.store.write_configuration(owner[0], configuration, owner[1])

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "denied", str(path))

    with monkeypatch.context() as context:
        if stage == "replace":
            context.setattr(os, "replace", denied)
        else:
            context.setattr(Path, "mkdir" if stage == "mkdir" else "write_text", denied)
        with pytest.raises(PermissionError) as caught:
            write()
    _assert_private_failure(caught.value, debug_store, original, path)
    assert "write" in str(caught.value) and "errno=13" in str(caught.value)
    assert isinstance(caught.value.__cause__, PermissionError)


def test_delete_io_failure_preserves_error_type_and_original_without_native_location(
    debug_store: DebugStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _read = debug_store.reader("configuration")
    original = path.read_bytes()

    def denied(_path: Path) -> None:
        raise PermissionError(errno.EACCES, "denied", str(path))

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", denied)
        with pytest.raises(PermissionError) as caught:
            debug_store.store.delete_configuration(
                debug_store.session_id, _CONFIGURATION_ID, debug_store.thread_id,
            )
    _assert_private_failure(caught.value, debug_store, original, path)
    assert "delete" in str(caught.value) and "errno=13" in str(caught.value)
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize("kind", _READERS)
def test_metadata_failure_is_not_reported_as_missing_or_empty(
    debug_store: DebugStore, kind: ReaderKind, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, read = debug_store.reader(kind)
    original = path.read_bytes()
    metadata_path = path.parent if kind == "listed" else path
    lstat = Path.lstat

    def denied(candidate: Path):
        if candidate == metadata_path:
            raise PermissionError(errno.EACCES, "denied", str(candidate))
        return lstat(candidate)

    with monkeypatch.context() as context:
        context.setattr(Path, "lstat", denied)
        with pytest.raises(PermissionError) as caught:
            read()
    _assert_private_failure(caught.value, debug_store, original, path)
    assert caught.value.errno == errno.EACCES
    assert isinstance(caught.value.__cause__, PermissionError)


def test_missing_configuration_directory_is_empty_without_recreation(debug_store: DebugStore) -> None:
    path, read = debug_store.reader("listed")
    path.unlink()
    path.parent.rmdir()
    assert read() == []
    assert not path.parent.exists()


def test_delete_missing_configuration_preserves_missing_type_and_owner(debug_store: DebugStore) -> None:
    path, _read = debug_store.reader("configuration")
    path.unlink()
    with pytest.raises(FileNotFoundError) as caught:
        debug_store.store.delete_configuration(
            debug_store.session_id, _CONFIGURATION_ID, debug_store.thread_id,
        )
    assert caught.value.errno == errno.ENOENT
    assert debug_store.session_id in str(caught.value) and debug_store.thread_id in str(caught.value)
    assert str(path) not in str(caught.value)
    assert not path.exists()
