from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.support.ports import (
    E2E_PORT_END,
    E2E_PORT_MAX_FILES,
    E2E_PORT_START,
    INTEGRATION_PORT_END,
    INTEGRATION_PORT_MAX_FILES,
    INTEGRATION_PORT_START,
    TEST_FILE_PORT_BLOCK_SIZE,
    TestFilePortBlock,
    e2e_port_block_for_file,
    integration_port_block_for_file,
    port_block_for_file,
)


def test_integration_port_block_supports_indices_above_99() -> None:
    integration_root = Path.cwd() / "tests" / "integration"
    test_files = sorted(path.resolve() for path in integration_root.rglob("test_*.py"))
    block = integration_port_block_for_file(test_files[-1])

    assert len(test_files) > 100
    assert block.index == len(test_files) - 1
    assert block.index > 99
    assert block.start == INTEGRATION_PORT_START + block.index * TEST_FILE_PORT_BLOCK_SIZE
    assert block.end == block.start + TEST_FILE_PORT_BLOCK_SIZE - 1
    assert block.end <= INTEGRATION_PORT_END


def test_integration_and_e2e_port_ranges_are_adjacent_and_disjoint() -> None:
    assert INTEGRATION_PORT_START <= INTEGRATION_PORT_END
    assert E2E_PORT_START == INTEGRATION_PORT_END + 1
    assert INTEGRATION_PORT_END < E2E_PORT_START
    assert (
        E2E_PORT_END
        == E2E_PORT_START + TEST_FILE_PORT_BLOCK_SIZE * E2E_PORT_MAX_FILES - 1
    )


def test_port_file_tree_is_scanned_once_for_repeated_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    first_file = tests_root / "test_first.py"
    second_file = tests_root / "test_second.py"
    first_file.touch()
    second_file.touch()

    scans = 0
    original_rglob = Path.rglob

    def count_scans(path: Path, pattern: str) -> Iterator[Path]:
        nonlocal scans
        scans += 1
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", count_scans)

    first = port_block_for_file(
        first_file,
        test_layer="integration",
        port_start=1000,
        max_files=10,
        tests_root=tests_root,
    )
    second = port_block_for_file(
        second_file,
        test_layer="integration",
        port_start=1000,
        max_files=10,
        tests_root=tests_root,
    )

    assert first.index == 0
    assert second.index == 1
    assert scans == 1


@pytest.mark.parametrize(
    ("layer", "block_for_file", "max_files", "port_start", "port_end"),
    (
        (
            "integration",
            integration_port_block_for_file,
            INTEGRATION_PORT_MAX_FILES,
            INTEGRATION_PORT_START,
            INTEGRATION_PORT_END,
        ),
        (
            "e2e",
            e2e_port_block_for_file,
            E2E_PORT_MAX_FILES,
            E2E_PORT_START,
            E2E_PORT_END,
        ),
    ),
)
def test_last_file_in_layer_capacity_ends_at_configured_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    block_for_file: Callable[[Path], TestFilePortBlock],
    max_files: int,
    port_start: int,
    port_end: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests_root = tmp_path / "tests" / layer
    tests_root.mkdir(parents=True)
    test_files = [
        tests_root / f"test_{index:03d}.py" for index in range(max_files)
    ]
    for test_file in test_files:
        test_file.touch()

    block = block_for_file(test_files[-1])

    assert block.index == max_files - 1
    assert block.start == port_end - TEST_FILE_PORT_BLOCK_SIZE + 1
    assert block.end == port_end
    assert block.start >= port_start


@pytest.mark.parametrize(
    ("layer", "block_for_file", "max_files"),
    (
        (
            "integration",
            integration_port_block_for_file,
            INTEGRATION_PORT_MAX_FILES,
        ),
        ("e2e", e2e_port_block_for_file, E2E_PORT_MAX_FILES),
    ),
)
def test_file_beyond_layer_capacity_fails_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    block_for_file: Callable[[Path], TestFilePortBlock],
    max_files: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    tests_root = tmp_path / "tests" / layer
    tests_root.mkdir(parents=True)
    test_files = [
        tests_root / f"test_{index:03d}.py" for index in range(max_files + 1)
    ]
    for test_file in test_files:
        test_file.touch()

    with pytest.raises(
        RuntimeError, match=rf"count={max_files + 1}, max={max_files}"
    ):
        block_for_file(test_files[-1])


def test_port_block_keeps_offsets_in_zero_through_99() -> None:
    block = TestFilePortBlock(
        test_file=Path("test_ports.py"),
        index=0,
        start=INTEGRATION_PORT_START,
        end=INTEGRATION_PORT_START + TEST_FILE_PORT_BLOCK_SIZE - 1,
    )

    assert block.port(0) == block.start
    assert block.port(99) == block.end
    with pytest.raises(ValueError, match="offset=100"):
        block.port(100)
    with pytest.raises(ValueError, match="offset=-1"):
        block.port(-1)
