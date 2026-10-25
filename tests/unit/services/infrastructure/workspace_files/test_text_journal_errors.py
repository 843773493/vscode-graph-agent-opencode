"""journal 的局部存储错误合同，不代替完整文本工具执行验收。"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.hashing import sha256_hex
from app.schemas.internal_v2.text_mutation import EditText
from app.services.infrastructure.workspace_files import text_journal


@pytest.fixture
def journal_case(empty_workspace_scope: tuple[str, Path]):
    workspace_root = empty_workspace_scope[1]
    snapshots = [{"uri": "bt://workspace/ws_test/files/root/notes.txt", "before_content": "before"}]
    journal_id = text_journal.write_text_mutation_journal(snapshots, workspace_root=workspace_root)
    path = workspace_root / ".boxteam/cache/text_mutations" / f"{journal_id}.json"
    return SimpleNamespace(root=workspace_root, path=path, journal_id=journal_id,
                           original=path.read_bytes(), snapshots=snapshots)


def _load(case):
    return text_journal.load_text_mutation_snapshots(
        json.dumps({"journal_id": case.journal_id}), workspace_root=case.root,
    )


@pytest.mark.parametrize("stage", ["create_mkdir", "create_write", "read_stat", "read_text", "update_read", "update_write"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO, errno.ELOOP])
def test_journal_io_errors_keep_cause_errno_original_and_logical_identity(
    journal_case, monkeypatch: pytest.MonkeyPatch, stage: str, error_number: int,
):
    case = journal_case
    method = {"create_mkdir": "mkdir", "create_write": "write_text", "read_stat": "stat",
              "read_text": "read_text", "update_read": "read_text", "update_write": "write_text"}[stage]
    target = case.path.parent if stage == "create_mkdir" else case.path
    original = getattr(Path, method)
    fault = OSError(error_number, f"private-input {case.root}", str(target))

    def fail(path, *args, **kwargs):
        if path == target:
            raise fault
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        patch.setattr(text_journal, "create_prefixed_id", lambda _prefix: case.journal_id)
        with pytest.raises(type(fault)) as caught:
            if stage.startswith("create"):
                text_journal.write_text_mutation_journal([], workspace_root=case.root)
            elif stage.startswith("update"):
                text_journal.update_text_mutation_journal(case.journal_id, [], observations={}, workspace_root=case.root)
            else:
                _load(case)
    assert type(caught.value) is type(fault)
    assert caught.value.__cause__ is fault and caught.value.errno == error_number
    assert caught.value.filename is None and caught.value.filename2 is None
    assert "artifact=text_mutation_journal" in str(caught.value)
    assert f"stage={stage.split('_')[0]}" in str(caught.value)
    assert str(case.root) not in str(caught.value) and "private-input" not in str(caught.value)
    assert case.path.read_bytes() == case.original
    assert _load(case) == case.snapshots
    text_journal.update_text_mutation_journal(case.journal_id, [], observations={}, workspace_root=case.root)
    assert json.loads(case.path.read_bytes())["files"] == []


@pytest.mark.parametrize("operation", ["read", "update"])
@pytest.mark.parametrize("failure", ["missing", "directory", "shape", "json", "utf8"])
def test_journal_invalid_storage_keeps_error_type_and_never_rewrites(journal_case, operation, failure):
    case = journal_case
    private_input = str(case.root / "private-input")
    corrupted = None
    if failure in {"missing", "directory"}:
        case.path.unlink()
        if failure == "directory":
            case.path.mkdir()
    else:
        corrupted = {"shape": json.dumps([private_input]).encode(),
                     "json": ('{\n"' + private_input + '":').encode(),
                     "utf8": b"\xff" + private_input.encode()}[failure]
        case.path.write_bytes(corrupted)
    expected = {"shape": TypeError, "utf8": UnicodeDecodeError}.get(failure, RuntimeError)
    with pytest.raises(expected) as caught:
        if operation == "read":
            _load(case)
        else:
            text_journal.update_text_mutation_journal(case.journal_id, [], observations={}, workspace_root=case.root)
    assert type(caught.value) is expected
    assert "artifact=text_mutation_journal" in str(caught.value)
    assert str(case.root) not in str(caught.value) and "private-input" not in str(caught.value)
    if failure == "json":
        cause = caught.value.__cause__
        assert isinstance(cause, json.JSONDecodeError)
        assert f"stage={operation}" in str(caught.value)
        for key, value in (("line", cause.lineno), ("column", cause.colno), ("position", cause.pos)):
            assert f"{key}={value}" in str(caught.value)
    elif failure == "utf8":
        cause = caught.value.__cause__
        assert isinstance(cause, UnicodeDecodeError)
        assert (caught.value.start, caught.value.end) == (cause.start, cause.end) == (0, 1)
        assert caught.value.object == cause.object and caught.value.encoding == cause.encoding
        assert f"stage={operation}" in str(caught.value)
    if corrupted is not None:
        assert case.path.read_bytes() == corrupted
    elif failure == "directory":
        assert list(case.path.iterdir()) == []
        case.path.rmdir()
    else:
        assert not case.path.exists()
    case.path.write_bytes(case.original)
    assert _load(case) == case.snapshots


def test_update_journal_failure_preserves_already_published_file(journal_case, monkeypatch, text_mutations_factory):
    case = journal_case
    target = case.root / "notes.txt"
    target.write_bytes(b"before\n")
    owner = text_mutations_factory(case.root)
    actual = Path.write_text
    writes = 0

    def fail_update(path, *args, **kwargs):
        nonlocal writes
        if path.parent == case.path.parent:
            writes += 1
            if writes == 2:
                raise OSError(errno.EIO, f"private-input {case.root}", str(path))
        return actual(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail_update)
        receipt = owner.execute(EditText(target="notes.txt", old_string="before", new_string="after",
                                         expected_version="sha256:" + sha256_hex(b"before\n")))
    assert writes == 2
    assert receipt["status"] == "error" and receipt["effect"] == "completed" and receipt["retryable"] is False
    assert receipt["files"][0]["state"] == "published" and receipt["error_type"] == "OSError"
    assert "artifact=text_mutation_journal" in receipt["error"] and "errno=5" in receipt["error"]
    assert str(case.root) not in json.dumps(receipt) and "private-input" not in json.dumps(receipt)
    assert target.read_bytes() == b"after\n"
