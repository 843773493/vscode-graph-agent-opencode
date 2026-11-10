from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from time import time

import pytest

from app.gateway.runtime.log_query import (
    MAX_LOG_QUERY_BYTES,
    MAX_LOG_QUERY_LINE_BYTES,
    LogQueryBudget,
    RuntimeLogQueryError,
    query_runtime_log,
    read_runtime_log_record,
)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    path = tmp_path / "backend.log"
    path.write_bytes("未命中\n😀中文 needle 中 needle\r\n最后 needle".encode())
    return path


def test_unicode_matches_continue_inside_original_line(log_path: Path) -> None:
    first = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    hit = first.items[0]
    assert (hit.reference.start, hit.reference.end) == (4, 10)
    assert hit.preview[hit.reference.start - hit.preview_start:hit.reference.end - hit.preview_start] == "needle"
    assert read_runtime_log_record(log_path, hit.reference) == "😀中文 needle 中 needle"
    second = query_runtime_log(
        log_path, "needle", limit=1, budget=LogQueryBudget.create(),
        offset=first.offset, match_offset=first.match_offset, observation=first.observation,
    )
    assert second.items[0].reference.offset == hit.reference.offset
    assert second.items[0].reference.start == 13
    final = query_runtime_log(
        log_path, "needle", limit=1, budget=LogQueryBudget.create(),
        offset=second.offset, match_offset=second.match_offset, observation=second.observation,
    )
    assert read_runtime_log_record(log_path, final.items[0].reference) == "最后 needle"
    assert final.limited_reason is None and final.offset == log_path.stat().st_size


def test_append_rejects_cursor_but_preserves_complete_record(log_path: Path) -> None:
    first = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(" appended\n")
    with pytest.raises(RuntimeLogQueryError) as caught:
        query_runtime_log(
            log_path, "needle", limit=1, budget=LogQueryBudget.create(),
            offset=first.offset, match_offset=first.match_offset, observation=first.observation,
        )
    assert (caught.value.code, caught.value.reason) == ("stale_runtime_log_cursor", "file_changed")
    assert read_runtime_log_record(log_path, first.items[0].reference) == "😀中文 needle 中 needle"


def test_extending_unterminated_record_invalidates_its_reference(log_path: Path) -> None:
    page = query_runtime_log(log_path, "最后", limit=1, budget=LogQueryBudget.create())
    with log_path.open("ab") as stream:
        stream.write(b" changed")
    with pytest.raises(RuntimeLogQueryError) as caught:
        read_runtime_log_record(log_path, page.items[0].reference)
    assert (caught.value.code, caught.value.reason) == ("stale_runtime_log_reference", "record_changed")


def test_reference_detects_full_original_line_change_with_restored_mtime(log_path: Path) -> None:
    page = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    original = log_path.stat()
    log_path.write_bytes(log_path.read_bytes().replace(b"needle", b"NEEDLE", 1))
    os.utime(log_path, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(RuntimeLogQueryError) as caught:
        read_runtime_log_record(log_path, page.items[0].reference)
    assert caught.value.reason == "record_changed"


def test_rotation_and_deletion_do_not_redirect_reference(log_path: Path) -> None:
    page = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    log_path.rename(log_path.with_suffix(".log.1"))
    log_path.write_text("😀中文 needle 中 needle\n", encoding="utf-8")
    with pytest.raises(RuntimeLogQueryError) as caught:
        read_runtime_log_record(log_path, page.items[0].reference)
    assert caught.value.reason == "file_replaced"
    log_path.unlink()
    with pytest.raises(RuntimeLogQueryError) as caught:
        read_runtime_log_record(log_path, page.items[0].reference)
    assert caught.value.reason == "file_deleted"


def test_line_budget_provides_progress_without_false_complete(tmp_path: Path) -> None:
    path = tmp_path / "many.log"
    path.write_bytes(("ordinary\n" * 512 + "needle\n").encode())
    budget = LogQueryBudget.create()
    first = query_runtime_log(path, "needle", limit=5, budget=budget)
    assert first.items == [] and first.limited_reason == "scan_limit"
    assert budget.lines == 512 and first.offset == len(b"ordinary\n") * 512
    second = query_runtime_log(
        path, "needle", limit=5, budget=LogQueryBudget.create(),
        offset=first.offset, observation=first.observation,
    )
    assert len(second.items) == 1 and second.limited_reason is None


def test_byte_budget_does_not_return_partial_line_matches(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_bytes((b"needle" + b"x" * 50000 + b"\n") * 200)
    budget = LogQueryBudget.create()
    first = query_runtime_log(path, "not-present", limit=5, budget=budget)
    assert first.limited_reason == "byte_limit"
    assert budget.bytes_read == MAX_LOG_QUERY_BYTES
    assert first.offset == (50007 * 167)
    second = query_runtime_log(
        path, "needle", limit=1, budget=LogQueryBudget.create(),
        offset=first.offset, observation=first.observation,
    )
    assert second.items[0].reference.offset == first.offset


def test_oversized_line_and_elapsed_deadline_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "oversized.log"
    path.write_bytes(b"x" * (MAX_LOG_QUERY_LINE_BYTES + 1) + b"\n")
    page = query_runtime_log(path, "x", limit=1, budget=LogQueryBudget.create())
    assert page.items == [] and page.limited_reason == "item_size_limit" and page.offset == 0
    budget = LogQueryBudget.create(time() - 1)
    page = query_runtime_log(path, "x", limit=1, budget=budget)
    assert page.limited_reason == "deadline" and budget.bytes_read == 0


@pytest.mark.parametrize("offset,match_offset", [(-1, 0), (1, 0), (0, 1000), (1000, 0)])
def test_invalid_cursor_positions_are_rejected(log_path: Path, offset: int, match_offset: int) -> None:
    with pytest.raises(RuntimeLogQueryError) as caught:
        query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create(), offset=offset, match_offset=match_offset)
    assert (caught.value.code, caught.value.reason) == ("invalid_runtime_log_cursor", "position_out_of_bounds")


def test_exact_read_rejects_out_of_bounds_position(log_path: Path) -> None:
    page = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    with pytest.raises(RuntimeLogQueryError) as caught:
        read_runtime_log_record(log_path, replace(page.items[0].reference, end=1000))
    assert caught.value.reason == "position_out_of_bounds"


def test_symbolic_link_is_not_a_diagnostic_log(log_path: Path) -> None:
    linked = log_path.with_name("linked.log")
    linked.symlink_to(log_path)
    with pytest.raises(RuntimeError, match="普通文件"):
        query_runtime_log(linked, "needle", limit=1, budget=LogQueryBudget.create())


def test_cursor_and_exact_read_share_native_windows_file_identity(
    log_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ("volume-serial", "windows-file-index")
    monkeypatch.setattr(
        "app.gateway.runtime.log_query.runtime_log_path_identity",
        lambda _path: identity,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.log_query.runtime_log_file_identity",
        lambda _descriptor: identity,
    )

    page = query_runtime_log(log_path, "needle", limit=1, budget=LogQueryBudget.create())
    reference = page.items[0].reference

    assert page.observation.identity == (reference.device, reference.inode)
    assert reference.device == identity[0] and reference.inode == identity[1]
    assert read_runtime_log_record(log_path, reference) == "😀中文 needle 中 needle"
