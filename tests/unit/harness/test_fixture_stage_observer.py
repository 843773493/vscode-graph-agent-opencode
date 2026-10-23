from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import pytest

from tests.support.custom_tool_test_workspace import FixtureStageObserver


@dataclass
class ProfileFiles:
    root: Path
    opened: list[TextIO] = field(default_factory=list)
    on_open: Callable[[TextIO], None] = lambda _stream: None


@pytest.fixture
def profile_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProfileFiles]:
    state = ProfileFiles(tmp_path)
    original_open = Path.open

    def track_profile(path: Path, mode: str = "r", *args: object, **kwargs: object):
        stream = original_open(path, mode, *args, **kwargs)
        if path.parent == state.root and path.suffix == ".jsonl" and mode == "x":
            state.opened.append(stream)
            state.on_open(stream)
        return stream

    monkeypatch.setattr(Path, "open", track_profile)
    try:
        yield state
    finally:
        for stream in state.opened:
            stream.close()


def fail_once(
    monkeypatch: pytest.MonkeyPatch, stream: TextIO, operation: str, error: BaseException,
) -> None:
    original = getattr(stream, operation)
    failed = False

    def failing_io(*args: object, **kwargs: object):
        nonlocal failed
        if not failed:
            failed = True
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(stream, operation, failing_io)


@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_initial_profile_record_failure_closes_file_and_keeps_original_error(
    profile_files: ProfileFiles, monkeypatch: pytest.MonkeyPatch, operation: str, interrupted: bool,
) -> None:
    error = KeyboardInterrupt("计时开始中断") if interrupted else OSError("计时开始写入失败")
    profile_files.on_open = lambda stream: fail_once(monkeypatch, stream, operation, error)

    with pytest.raises(type(error)) as failure:
        FixtureStageObserver(profile_files.root)

    assert failure.value is error
    assert len(profile_files.opened) == 1
    assert profile_files.opened[0].closed, "构造失败也必须释放已经打开的计时文件"


@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_summary_failure_still_closes_file_without_repeating_summary(
    profile_files: ProfileFiles, monkeypatch: pytest.MonkeyPatch, operation: str, interrupted: bool,
) -> None:
    observer = FixtureStageObserver(profile_files.root)
    stream = profile_files.opened[0]
    error = KeyboardInterrupt("计时摘要中断") if interrupted else OSError("计时摘要写入失败")
    fail_once(monkeypatch, stream, operation, error)

    with pytest.raises(type(error)) as failure:
        observer.close()

    assert failure.value is error
    assert stream.closed, "摘要失败不能跳过文件关闭"
    before = observer.path.read_bytes()
    observer.close()
    assert observer.path.read_bytes() == before


def test_close_failure_preserves_summary_error_as_original_context(
    profile_files: ProfileFiles, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = FixtureStageObserver(profile_files.root)
    stream = profile_files.opened[0]
    summary_error = OSError("计时摘要写入失败")
    close_error = OSError("计时文件关闭失败")
    fail_once(monkeypatch, stream, "write", summary_error)
    original_close = stream.close

    def close_then_fail() -> None:
        original_close()
        raise close_error

    with monkeypatch.context() as patch:
        patch.setattr(stream, "close", close_then_fail)
        with pytest.raises(OSError) as failure:
            observer.close()

    assert failure.value is close_error
    assert close_error.__context__ is summary_error
    assert stream.closed
    observer.close()


def test_normal_profile_keeps_utf8_record_order_and_completion_counts(profile_files: ProfileFiles) -> None:
    observer = FixtureStageObserver(profile_files.root)
    observer.record("start", "generator.prepare_execution")
    observer.record("done", "generator.normal_turn", outcome="ok", text="会话🙂")
    observer.record("done", "generator.normal_turn", outcome="error")
    observer.record("done", "generator.fork_copy", outcome="ok", turn_count=3)
    observer.close()
    observer.close()

    rows = [json.loads(line) for line in observer.path.read_bytes().decode("utf-8").splitlines()]
    assert [row["sequence"] for row in rows] == list(range(1, 7))
    assert [row["event"] for row in rows] == ["start", "start", "done", "done", "done", "summary"]
    assert rows[2]["text"] == "会话🙂"
    assert rows[-1] | {"monotonic_ns": 0} == {
        "sequence": 6, "event": "summary", "stage": "fixture.profile", "monotonic_ns": 0,
        "total_turns": 4, "normal_turns": 1, "normal_prepare_calls": 1, "fork_turns": 3,
    }
    assert all(row["monotonic_ns"] > 0 for row in rows)
    assert profile_files.opened[0].closed
