"""写锁原生错误的公开事实、诊断原因和真实文件效果。"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import portalocker
import pytest

from app.agents.tools.apply_patch.tool import create_apply_patch_tool
from app.services.infrastructure.workspace_files import mutation_lock


@pytest.fixture
def lock_failure_case(empty_workspace_scope, monkeypatch):
    root = empty_workspace_scope[1]
    lock_path = root / mutation_lock._LOCK_PATH
    opened = []

    def inject(stage, error):
        mkdir, open_file = Path.mkdir, Path.open
        unlock = mutation_lock.portalocker.unlock

        def fail_mkdir(path, *args, **kwargs):
            if path == lock_path.parent and stage == "prepare":
                raise error
            return mkdir(path, *args, **kwargs)

        class LockFile:
            def __init__(self, file):
                self.file = file

            def __getattr__(self, name):
                return getattr(self.file, name)

            def close(self):
                self.file.close()
                if stage == "close":
                    raise error

        def open_lock(path, *args, **kwargs):
            if path != lock_path:
                return open_file(path, *args, **kwargs)
            if stage == "open":
                raise error
            file = open_file(path, *args, **kwargs)
            opened.append(file)
            return LockFile(file)

        def fail_acquire(*_args):
            raise error

        def fail_release(file):
            unlock(file)
            raise error

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        monkeypatch.setattr(Path, "open", open_lock)
        if stage == "acquire":
            monkeypatch.setattr(mutation_lock.portalocker, "lock", fail_acquire)
        elif stage == "release":
            monkeypatch.setattr(mutation_lock.portalocker, "unlock", fail_release)

    return SimpleNamespace(root=root, path=lock_path, inject=inject, opened=opened)


@pytest.mark.parametrize("stage", ["prepare", "open", "acquire", "release", "close"])
@pytest.mark.parametrize("native_path", ["/private/lock-sentinel", r"C:\private\lock-sentinel", r"\\server\share\lock-sentinel"])
@pytest.mark.parametrize("has_filename", [False, True])
def test_lock_native_failure_keeps_type_cause_and_releases_local_resources(
    lock_failure_case, caplog, stage, native_path, has_filename,
):
    case = lock_failure_case
    original = OSError(errno.EIO, f"原生错误包含 {native_path}", native_path if has_filename else None)
    case.inject(stage, original)
    body_ran = False
    with (caplog.at_level(logging.ERROR, logger=mutation_lock.__name__),
          pytest.raises(OSError) as caught,
          mutation_lock.workspace_file_mutation_lock(case.root)):
        body_ran = True
    error = caught.value
    assert type(error) is type(original) and error.errno == original.errno
    assert error.__cause__ is original
    assert error.filename is None and error.filename2 is None
    assert native_path not in str(error) and str(case.root) not in str(error)
    assert f"stage={stage}" in str(error) and "artifact=workspace_file_lock" in str(error)
    assert body_ran == (stage in {"release", "close"})
    assert all(file.closed for file in case.opened)
    lock = mutation_lock._get_workspace_lock(case.root)
    assert lock.acquire(blocking=False)
    lock.release()
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)


@pytest.mark.parametrize("stage", ["acquire", "release"])
def test_portalocker_failure_has_explicit_cause_without_raw_file_handle(
    lock_failure_case, stage,
):
    case = lock_failure_case
    original = portalocker.LockException(f"底层锁失败 {case.path}")
    case.inject(stage, original)
    with pytest.raises(RuntimeError) as caught, mutation_lock.workspace_file_mutation_lock(case.root):
        pass
    assert caught.value.__cause__ is original
    assert str(case.root) not in str(caught.value)
    assert "error=LockException" in str(caught.value)
    assert all(file.closed for file in case.opened)


@pytest.mark.parametrize("stage", ["open", "release", "close"])
@pytest.mark.parametrize("error_kind", ["os", "portalocker"])
def test_model_patch_lock_failure_preserves_actual_publication(
    lock_failure_case, text_mutations_factory, stage, error_kind,
):
    case = lock_failure_case
    original = (OSError(errno.EIO, f"系统原始路径 {case.path}", str(case.path))
                if error_kind == "os" else portalocker.LockException(f"锁原始路径 {case.path}"))
    case.inject(stage, original)
    tool = create_apply_patch_tool(text_mutations=text_mutations_factory(case.root))
    result = json.loads(tool.invoke({
        "input": "*** Begin Patch\n*** Add File: written.txt\n+written\n+\n*** End Patch",
        "target": ".", "expected_versions": {"written.txt": "absent"}, "explanation": "检查真实效果",
    }))
    assert result["status"] == "error"
    assert str(case.root) not in json.dumps(result)
    assert str(case.path) not in json.dumps(result)
    if stage == "open":
        assert result["effect"] == "not_executed" and not (case.root / "written.txt").exists()
    else:
        assert result["effect"] == "completed"
        assert result["files"][0]["state"] == "published"
        assert (case.root / "written.txt").read_bytes() == b"written\n"
        assert f"stage={stage}" in result["lock_release_error"]
    assert all(file.closed for file in case.opened)


def test_business_error_in_critical_section_keeps_its_original_owner(lock_failure_case, caplog):
    original = OSError(errno.EIO, "文件正文访问失败", "business-target")
    with pytest.raises(OSError) as caught, mutation_lock.workspace_file_mutation_lock(lock_failure_case.root):
        raise original
    assert caught.value is original
    assert not any(record.name == mutation_lock.__name__ for record in caplog.records)
