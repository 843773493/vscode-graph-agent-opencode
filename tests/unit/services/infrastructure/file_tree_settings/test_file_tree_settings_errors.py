"""快捷方式 owner 的读写失败保留错误类别、原件和原因，诊断不含原生路径。"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.path_utils import get_session_path_resolver
from app.services.infrastructure import file_tree_settings_service as settings_module
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


@pytest.fixture
def file_tree_shortcut_case(empty_workspace_scope: tuple[str, Path]):
    workspace_id, root = empty_workspace_scope
    sessions = root / ".boxteam/sessions"
    resolver = get_session_path_resolver(sessions)
    resolver.initialize()
    session = "ses_00000000400070008000000000000031"
    seed_catalog_session_bundle(sessions, session, title="快捷方式错误")
    paths = WorkspaceFilePaths(
        workspace=root, workspace_id=workspace_id, bindings=None,
    )
    service = FileTreeSettingsService(workspace_root=root, path_resolver=resolver, file_paths=paths)
    directory = root / "documents"
    directory.mkdir()
    service.add_session_shortcut(session, path=str(directory), label="原始标签")
    service.apply_to_workspace(session, path=str(directory))
    controls = {
        "session": resolver.resolve_session_node(session) / "ui/file-tree-shortcuts.json",
        "workspace": root / ".boxteam/ui/file-tree-shortcuts.json",
    }
    return SimpleNamespace(service=service, session=session, directory=directory,
                           controls=controls, root=root, uri=paths.reference(directory))


@pytest.mark.parametrize("scope", ["session", "workspace"])
@pytest.mark.parametrize("stage", ["stat", "read", "mkdir", "temporary", "dump", "fsync", "replace"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_shortcut_io_errors_preserve_original_type_cause_and_atomic_file(
    file_tree_shortcut_case, monkeypatch: pytest.MonkeyPatch, scope: str, stage: str, error_number: int,
):
    case = file_tree_shortcut_case
    path = case.controls[scope]
    original = path.read_bytes()
    fault = OSError(error_number, f"private-input.txt {case.root}", str(path), None, str(path))

    def fail(*_args, **_kwargs):
        raise fault

    with monkeypatch.context() as patch:
        if stage in {"stat", "read", "mkdir"}:
            method = {"stat": "exists", "read": "read_text", "mkdir": "mkdir"}[stage]
            actual = getattr(Path, method)

            def path_fault(target, *args, **kwargs):
                if target == (path.parent if stage == "mkdir" else path):
                    raise fault
                return actual(target, *args, **kwargs)

            patch.setattr(Path, method, path_fault)
        elif stage == "temporary":
            patch.setattr(settings_module.tempfile, "NamedTemporaryFile", fail)
        elif stage == "dump":
            patch.setattr(settings_module.json, "dump", fail)
        else:
            patch.setattr(settings_module.os, stage, fail)

        def invoke():
            if stage in {"stat", "read"}:
                return case.service.get(case.session)
            operation = case.service.add_session_shortcut if scope == "session" else case.service.apply_to_workspace
            return operation(case.session, path=str(case.directory), label="新标签")

        with pytest.raises(type(fault)) as caught:
            invoke()
    error = caught.value
    assert error.__cause__ is fault
    assert error.errno == error_number
    assert error.filename is None and error.filename2 is None
    assert str(case.root) not in str(error) and "private-input.txt" not in str(error)
    assert "artifact=file_tree_shortcuts" in str(error)
    assert f"errno={error_number}" in str(error)
    assert path.read_bytes() == original
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    retried = invoke()
    assert retried.session_shortcuts[0].path == case.uri


@pytest.mark.parametrize("scope", ["session", "workspace"])
@pytest.mark.parametrize("failure", ["object", "list", "entry", "validation", "json", "utf8"])
def test_shortcut_read_diagnostics_do_not_echo_invalid_input(file_tree_shortcut_case, scope: str, failure: str):
    case = file_tree_shortcut_case
    path = case.controls[scope]
    original = path.read_bytes()
    payload = json.loads(original)
    if failure == "object":
        corrupted = b"[]"
    elif failure == "json":
        corrupted = ('{"' + str(case.root / "private-input.txt") + '":').encode()
    elif failure == "utf8":
        corrupted = b"\xff" + str(case.root / "private-input.txt").encode()
    else:
        payload["shortcuts"] = {
            "list": str(case.root / "private-input.txt"), "entry": [str(case.root)],
            "validation": [{"path": str(case.root / "private-input.txt"), "label": {str(case.root): "secret"}}],
        }[failure]
        corrupted = json.dumps(payload).encode()
    path.write_bytes(corrupted)
    with pytest.raises((TypeError, ValueError)) as caught:
        case.service.get(case.session)
    message = str(caught.value)
    assert "artifact=file_tree_shortcuts" in message
    assert str(case.root) not in message and "private-input.txt" not in message
    if failure == "validation":
        assert "error_count=2" in message and "rules=" in message
    if failure == "json":
        assert "line=1" in message and "column=" in message
    if failure == "utf8":
        assert "start=0, end=1" in message
    assert path.read_bytes() == corrupted
    path.write_bytes(original)
    assert case.service.get(case.session).session_shortcuts[0].path == case.uri


def test_shortcut_snapshot_missing_version_and_duplicate_errors_keep_logical_identity(file_tree_shortcut_case):
    case = file_tree_shortcut_case
    path = case.controls["session"]
    original = path.read_bytes()
    for failure in ("version", "missing", "duplicate"):
        if failure == "version":
            payload = json.loads(original)
            payload["version"] = str(case.root)
            path.write_text(json.dumps(payload), encoding="utf-8")
        elif failure == "missing":
            path.unlink()
        else:
            path.write_bytes(original)
        expected = {"version": ValueError, "missing": FileNotFoundError, "duplicate": FileExistsError}[failure]
        with pytest.raises(expected) as caught:
            if failure == "duplicate":
                case.service.initialize_session(case.session)
            else:
                case.service.get(case.session)
        assert "artifact=file_tree_shortcuts" in str(caught.value)
        assert str(case.root) not in str(caught.value)
        if failure == "duplicate":
            assert case.session in str(caught.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_shortcut_directory_stat_error_uses_target_uri(file_tree_shortcut_case, monkeypatch, error_number):
    case = file_tree_shortcut_case
    actual = Path.exists
    fault = OSError(error_number, "private-input.txt", str(case.directory))

    def exists(target):
        if target == case.directory:
            raise fault
        return actual(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", exists)
        with pytest.raises(type(fault)) as caught:
            case.service.add_session_shortcut(case.session, path=str(case.directory), label=None)
    assert caught.value.__cause__ is fault
    assert caught.value.errno == error_number
    assert "stage=directory_stat" in str(caught.value) and case.uri in str(caught.value)
    assert str(case.root) not in str(caught.value) and "private-input.txt" not in str(caught.value)
    assert case.service.add_session_shortcut(case.session, path=case.uri, label=None).session_shortcuts[0].path == case.uri


@pytest.mark.parametrize("form", ["relative", "absolute", "uri"])
def test_shortcut_inputs_still_select_the_same_directory(file_tree_shortcut_case, form):
    case = file_tree_shortcut_case
    value = {"relative": "documents", "absolute": str(case.directory), "uri": case.uri}[form]
    result = case.service.add_session_shortcut(case.session, path=value, label="同一目录")
    assert [(item.path, item.label) for item in result.session_shortcuts] == [(case.uri, "同一目录")]
    assert result.workspace_shortcuts == []
    assert [item.path for item in result.default_shortcuts] == [case.uri]
    file = case.root / "file.txt"
    file.write_text("file", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="快捷路径不是目录"):
        case.service.add_session_shortcut(case.session, path=str(file), label=None)


def test_shortcut_cleanup_failure_keeps_both_causes_and_original(file_tree_shortcut_case, monkeypatch):
    case = file_tree_shortcut_case
    path = case.controls["session"]
    original = path.read_bytes()
    publish_fault = OSError(errno.EIO, "publish private-input.txt", str(path))
    cleanup_fault = PermissionError(errno.EACCES, "cleanup private-input.txt", str(path))

    def refuse_publish(*_args):
        raise publish_fault

    def refuse_cleanup(*_args):
        raise cleanup_fault

    with monkeypatch.context() as patch:
        patch.setattr(settings_module.os, "replace", refuse_publish)
        patch.setattr(settings_module.os, "unlink", refuse_cleanup)
        with pytest.raises(PermissionError) as caught:
            case.service.add_session_shortcut(case.session, path=case.uri, label="未发布")
    assert caught.value.__cause__ is cleanup_fault
    assert cleanup_fault.__context__ is publish_fault
    assert str(case.root) not in str(caught.value) and "private-input.txt" not in str(caught.value)
    assert path.read_bytes() == original
    (temporary,) = path.parent.glob(f".{path.name}.*.tmp")
    temporary.unlink()
