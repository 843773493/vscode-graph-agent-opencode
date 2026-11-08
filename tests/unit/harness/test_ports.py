from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import Path

import pytest

from tests.support.ports import (
    TEST_FILE_PORT_BLOCK_SIZE,
    TEST_FILE_PORT_END,
    TEST_FILE_PORT_MAX_FILES,
    TEST_FILE_PORT_RANGES,
    TEST_FILE_PORT_START,
    TestFilePortBlock,
    e2e_port_block_for_file,
    integration_port_block_for_file,
)

PortTree = Callable[[int, int], tuple[list[Path], list[Path]]]


@pytest.fixture
def port_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PortTree:
    monkeypatch.chdir(tmp_path)

    def prepare(integration_count: int, e2e_count: int) -> tuple[list[Path], list[Path]]:
        files: list[list[Path]] = []
        for layer, count in (("integration", integration_count), ("e2e", e2e_count)):
            root = tmp_path / "tests" / layer
            root.mkdir(parents=True)
            layer_files = [root / f"test_{index:04d}.py" for index in range(count)]
            for test_file in layer_files:
                test_file.touch()
            files.append(layer_files)
        return files[0], files[1]

    return prepare


def test_repository_files_have_disjoint_blocks_with_valid_ports() -> None:
    root = Path.cwd() / "tests"
    blocks = [
        allocate(path)
        for layer, allocate in (
            ("integration", integration_port_block_for_file),
            ("e2e", e2e_port_block_for_file),
        )
        for path in sorted((root / layer).rglob("test_*.py"))
    ]

    assert len(blocks) > 450
    assert blocks[0].start == TEST_FILE_PORT_START
    for index, block in enumerate(blocks):
        assert block.index == index
        assert block.end - block.start + 1 == TEST_FILE_PORT_BLOCK_SIZE
        assert block.backend_port == block.start
        assert any(start <= block.start <= block.end <= end for start, end in TEST_FILE_PORT_RANGES)
        assert block.end < 8000 or block.start >= 8800
        if index and block.start != blocks[index - 1].end + 1:
            assert (blocks[index - 1].end, block.start) == (TEST_FILE_PORT_END, TEST_FILE_PORT_RANGES[1][0])
    ordered = sorted(blocks, key=lambda block: block.start)
    assert all(left.end < right.start for left, right in pairwise(ordered))
    assert blocks[-1].end <= TEST_FILE_PORT_END <= 65535


@pytest.mark.parametrize("integration_count,e2e_count", [(451, 100), (500, 50)])
def test_unused_layer_capacity_is_available_to_the_other_layer(
    port_tree: PortTree, integration_count: int, e2e_count: int,
) -> None:
    integration, e2e = port_tree(integration_count, e2e_count)
    last_integration = integration_port_block_for_file(integration[-1])
    first_e2e = e2e_port_block_for_file(e2e[0])
    last_e2e = e2e_port_block_for_file(e2e[-1])

    assert last_integration.index == integration_count - 1
    assert first_e2e.start == last_integration.end + 1
    assert last_e2e.index == integration_count + e2e_count - 1
    assert last_e2e.end <= 65535


@pytest.mark.parametrize("integration_count", [0, 450, 500, TEST_FILE_PORT_MAX_FILES])
def test_full_shared_pool_ends_at_the_last_complete_block_in_its_range(
    port_tree: PortTree, integration_count: int,
) -> None:
    integration, e2e = port_tree(integration_count, TEST_FILE_PORT_MAX_FILES - integration_count)
    block = e2e_port_block_for_file(e2e[-1]) if e2e else integration_port_block_for_file(integration[-1])

    assert block.index == TEST_FILE_PORT_MAX_FILES - 1
    start, end = TEST_FILE_PORT_RANGES[-1]
    assert block.end == start + (end - start + 1) // TEST_FILE_PORT_BLOCK_SIZE * TEST_FILE_PORT_BLOCK_SIZE - 1
    assert block.end + TEST_FILE_PORT_BLOCK_SIZE > end


def test_exhausted_high_range_uses_disjoint_low_range_without_wrapping(port_tree: PortTree) -> None:
    high_start, high_end = TEST_FILE_PORT_RANGES[0]
    high_capacity = (high_end - high_start + 1) // TEST_FILE_PORT_BLOCK_SIZE
    integration, e2e = port_tree(high_capacity, 1)
    previous = integration_port_block_for_file(integration[-1])
    overflow = e2e_port_block_for_file(e2e[0])
    assert previous.end == TEST_FILE_PORT_END
    assert overflow.start == TEST_FILE_PORT_RANGES[1][0]
    assert overflow.end < 8000
    assert overflow.index == previous.index + 1


def test_pool_overflow_rejects_even_the_first_file_in_either_layer(port_tree: PortTree) -> None:
    integration, e2e = port_tree(TEST_FILE_PORT_MAX_FILES, 1)

    for allocate, test_file in (
        (integration_port_block_for_file, integration[0]),
        (e2e_port_block_for_file, e2e[0]),
    ):
        with pytest.raises(RuntimeError, match=rf"count={TEST_FILE_PORT_MAX_FILES + 1}, max={TEST_FILE_PORT_MAX_FILES}"):
            allocate(test_file)


def test_both_layers_share_one_cached_file_snapshot(
    port_tree: PortTree, monkeypatch: pytest.MonkeyPatch,
) -> None:
    integration, e2e = port_tree(1, 1)
    scans: list[Path] = []
    original_rglob = Path.rglob

    def count_scans(path: Path, pattern: str) -> Iterator[Path]:
        scans.append(path)
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", count_scans)
    first = integration_port_block_for_file(integration[0])
    second = e2e_port_block_for_file(e2e[0])
    assert integration_port_block_for_file(integration[0]) == first

    assert first.index == 0 and second.index == 1
    assert scans == [Path.cwd() / "tests/integration", Path.cwd() / "tests/e2e"]


def test_explicit_tests_root_determines_the_whole_pool(port_tree: PortTree, monkeypatch: pytest.MonkeyPatch) -> None:
    integration, e2e = port_tree(2, 1)
    expected = integration_port_block_for_file(integration[-1])
    tests_root = Path.cwd() / "tests"
    monkeypatch.chdir(tests_root)

    block = e2e_port_block_for_file(e2e[0], tests_root=tests_root)

    assert block.index == 2
    assert block.start == expected.end + 1


def test_wrong_layer_and_missing_files_are_rejected(port_tree: PortTree) -> None:
    integration, e2e = port_tree(1, 1)
    with pytest.raises(ValueError, match="不在指定测试层"):
        integration_port_block_for_file(e2e[0])
    with pytest.raises(ValueError, match="不在指定测试层"):
        e2e_port_block_for_file(integration[0])
    with pytest.raises(ValueError, match="未能.*分配端口块"):
        integration_port_block_for_file(Path.cwd() / "tests/integration/test_missing.py")


def test_port_block_accepts_exactly_its_configured_offsets() -> None:
    block = TestFilePortBlock(
        test_file=Path("test_ports.py"), index=0,
        start=TEST_FILE_PORT_START, end=TEST_FILE_PORT_START + TEST_FILE_PORT_BLOCK_SIZE - 1,
    )

    assert block.port(0) == block.start
    assert block.port(TEST_FILE_PORT_BLOCK_SIZE - 1) == block.end
    with pytest.raises(ValueError, match=f"offset={TEST_FILE_PORT_BLOCK_SIZE}"):
        block.port(TEST_FILE_PORT_BLOCK_SIZE)
    with pytest.raises(ValueError, match="offset=-1"):
        block.port(-1)


def test_shifted_port_block_does_not_extend_its_original_boundary() -> None:
    block = TestFilePortBlock(
        test_file=Path("test_ports.py"), index=0,
        start=TEST_FILE_PORT_START + 20, end=TEST_FILE_PORT_START + TEST_FILE_PORT_BLOCK_SIZE - 1,
    )

    assert block.port(TEST_FILE_PORT_BLOCK_SIZE - 21) == block.end
    with pytest.raises(ValueError, match=f"offset={TEST_FILE_PORT_BLOCK_SIZE - 20}"):
        block.port(TEST_FILE_PORT_BLOCK_SIZE - 20)
