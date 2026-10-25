"""工具选择 owner 的错误只发布逻辑诊断，原始异常与磁盘事实仍可核验。"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import portalocker
import pytest

from app.services.infrastructure import tool_selection_store as selection_module
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


@pytest.fixture
def selection_case(empty_workspace_scope: tuple[str, Path]):
    _, root = empty_workspace_scope
    store = ToolSelectionStore(boxteam_root=root / ".boxteam")
    store.apply_changes(agent_id="default", changes={"read_file": (True, True)})
    path = root / ".boxteam/settings/tool_selection.json"
    return SimpleNamespace(root=root, store=store, path=path, original=path.read_bytes())


@pytest.mark.parametrize("operation,stage", [
    (operation, stage) for operation in ("snapshot", "apply_changes")
    for stage in ("mkdir", "open", "stat", "read", "lock", "unlock")
] + [("apply_changes", stage) for stage in ("write", "replace")])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_selection_io_errors_preserve_type_cause_and_commit_boundary(
    selection_case, monkeypatch: pytest.MonkeyPatch,
    operation: str, stage: str, error_number: int,
):
    case = selection_case
    fault = OSError(error_number, f"private-input.txt {case.root}", str(case.path), None, str(case.path))

    def fail(*_args, **_kwargs):
        raise fault

    def invoke():
        if operation == "snapshot":
            return case.store.snapshot("default")
        return case.store.apply_changes(agent_id="default", changes={"read_file": (True, False)})

    with monkeypatch.context() as patch:
        if stage in {"lock", "unlock"}:
            patch.setattr(selection_module.portalocker, stage, fail)
        else:
            method = {"mkdir": "mkdir", "open": "open", "stat": "exists", "read": "read_text", "write": "write_text", "replace": "replace"}[stage]
            target = case.path.parent if stage == "mkdir" else (
                case.path.parent / "tool_selection.lock" if stage == "open" else (
                    case.path.with_suffix(".json.tmp") if stage in {"write", "replace"} else case.path
                )
            )
            actual = getattr(Path, method)

            def path_fault(path, *args, **kwargs):
                if path == target:
                    raise fault
                return actual(path, *args, **kwargs)

            patch.setattr(Path, method, path_fault)
        with pytest.raises(type(fault)) as caught:
            invoke()
    error = caught.value
    assert error.__cause__ is fault and error.errno == error_number
    assert error.filename is None and error.filename2 is None
    assert "artifact=tool_selection" in str(error) and f"operation={operation}" in str(error)
    assert str(case.root) not in str(error) and "private-input.txt" not in str(error)
    published = operation == "apply_changes" and stage == "unlock"
    assert case.store.snapshot("default").model_visibility_overrides == {"read_file": not published}
    if not published:
        assert case.path.read_bytes() == case.original
    temporary = case.path.with_suffix(".json.tmp")
    # replace 失败沿既有策略保留未提交正文；它不得被读路径当成成功结果。
    assert temporary.exists() == (stage == "replace")
    if temporary.exists():
        temporary.unlink()
    invoke()
    assert not temporary.exists()


@pytest.mark.parametrize("operation", ["snapshot", "apply_changes"])
@pytest.mark.parametrize("failure", ["shape", "agent", "execution", "visibility", "json", "utf8"])
def test_selection_corruption_is_not_echoed_or_overwritten(selection_case, operation, failure):
    case = selection_case
    private_input = str(case.root / "private-input.txt")
    corrupted = {
        "shape": b"[]",
        "agent": json.dumps({private_input: []}).encode(),
        "execution": json.dumps({private_input: {"execution_overrides": []}}).encode(),
        "visibility": json.dumps({private_input: {"model_visibility_overrides": []}}).encode(),
        "json": ('{"' + private_input + '":').encode(),
        "utf8": b"\xff" + private_input.encode(),
    }[failure]
    case.path.write_bytes(corrupted)
    with pytest.raises(ValueError if failure in {"json", "utf8"} else TypeError) as caught:
        if operation == "snapshot":
            case.store.snapshot("default")
        else:
            case.store.apply_changes(agent_id="default", changes={"read_file": (False, False)})
    message = str(caught.value)
    assert "artifact=tool_selection" in message
    assert str(case.root) not in message and "private-input.txt" not in message
    if failure in {"json", "utf8"}:
        assert isinstance(caught.value.__cause__, json.JSONDecodeError if failure == "json" else UnicodeDecodeError)
        assert ("line=1" if failure == "json" else "start=0, end=1") in message
    assert case.path.read_bytes() == corrupted
    assert not case.path.with_suffix(".json.tmp").exists()
    case.path.write_bytes(case.original)
    assert case.store.snapshot("default").model_visibility_overrides == {"read_file": True}


@pytest.mark.parametrize("operation", ["snapshot", "apply_changes"])
@pytest.mark.parametrize("method", ["lock", "unlock"])
def test_selection_portalocker_errors_preserve_os_cause(selection_case, monkeypatch, operation, method):
    case = selection_case
    os_error = PermissionError(errno.EACCES, "private-input.txt", str(case.path))
    fault = portalocker.LockException(os_error, str(os_error))

    def fail(*_args):
        raise fault

    with monkeypatch.context() as patch:
        patch.setattr(selection_module.portalocker, method, fail)
        with pytest.raises(portalocker.LockException) as caught:
            if operation == "snapshot":
                case.store.snapshot("default")
            else:
                case.store.apply_changes(agent_id="default", changes={"read_file": (True, True)})
    assert caught.value.__cause__ is fault
    assert "errno=13" in str(caught.value) and f"operation={operation}" in str(caught.value)
    assert str(case.root) not in str(caught.value) and "private-input.txt" not in str(caught.value)
    assert case.path.read_bytes() == case.original
    assert case.store.snapshot("default").model_visibility_overrides == {"read_file": True}
