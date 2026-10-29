from pathlib import Path
from subprocess import CompletedProcess

import portalocker
import pytest

from tests.harness.python import integration_gate as gate_module


def test_source_archive_uses_its_explicit_root(tmp_path: Path) -> None:
    lock_path = gate_module.integration_gate_path(tmp_path)
    assert lock_path == tmp_path / "out/tests/harness/python/integration_gate/runtime/session.lock"


def test_worktree_git_failure_is_not_silently_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").write_text("gitdir: missing", encoding="utf-8")
    error = OSError("git 无法启动")

    def fail(*args: object, **kwargs: object) -> CompletedProcess[str]:
        raise error

    monkeypatch.setattr(gate_module.subprocess, "run", fail)
    with pytest.raises(OSError) as caught:
        gate_module.integration_gate_path(tmp_path)
    assert caught.value is error


@pytest.mark.parametrize("output", ["", "bare\0", "HEAD deadbeef\0"])
def test_invalid_git_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        gate_module.subprocess, "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, output, ""),
    )
    with pytest.raises(ValueError, match="Git 未返回主工作树位置"):
        gate_module.integration_gate_path(tmp_path)


@pytest.mark.parametrize("output", ["worktree \0", "worktree relative\0"])
def test_git_owner_must_be_an_absolute_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        gate_module.subprocess, "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, output, ""),
    )
    with pytest.raises(ValueError, match="不是绝对路径"):
        gate_module.integration_gate_path(tmp_path)


def test_timeout_and_failed_test_release_the_same_lock(tmp_path: Path) -> None:
    lock_path = gate_module.integration_gate_path(tmp_path)
    marker = RuntimeError("测试本身失败")
    with pytest.raises(RuntimeError) as caught, gate_module.integration_gate(lock_path):
        with pytest.raises(portalocker.AlreadyLocked), gate_module.integration_gate(lock_path, timeout=0.01):
            pytest.fail("另一个运行组不应进入")
        raise marker
    assert caught.value is marker
    assert lock_path.is_file()
    with gate_module.integration_gate(lock_path, timeout=0):
        assert lock_path.is_file()
