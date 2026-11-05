from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal

TEST_FILE_PORT_BLOCK_SIZE = 100
TEST_FILE_PORT_START = 8800
# 先保留既有高端口块，耗尽后使用低段；跳过特权端口及 8000–8799 开发服务段。
TEST_FILE_PORT_RANGES = ((TEST_FILE_PORT_START, 65535), (1024, 7999))
TEST_FILE_PORT_MAX_FILES = sum((end - start + 1) // TEST_FILE_PORT_BLOCK_SIZE for start, end in TEST_FILE_PORT_RANGES)
TEST_FILE_PORT_END = max(start + (end - start + 1) // TEST_FILE_PORT_BLOCK_SIZE * TEST_FILE_PORT_BLOCK_SIZE - 1 for start, end in TEST_FILE_PORT_RANGES)


@lru_cache(maxsize=16)
def _test_file_indexes_for_tests_root(
    tests_root: Path,
) -> tuple[int, Mapping[Path, int]]:
    """两层共享一次文件快照，集成在前、E2E在后，不预留固定层级配额。"""
    test_files = [
        path.resolve()
        for layer in ("integration", "e2e")
        for path in sorted((tests_root / layer).rglob("test_*.py"))
    ]
    first_index_by_path: dict[Path, int] = {}
    for index, test_file in enumerate(test_files):
        first_index_by_path.setdefault(test_file, index)
    return len(test_files), MappingProxyType(first_index_by_path)


@dataclass(frozen=True, slots=True)
class TestFilePortBlock:
    """一个集成测试或 E2E 测试文件独占的连续端口块。"""

    __test__: ClassVar[bool] = False
    test_file: Path
    # index 是共享文件快照中的零基序号，先integration、后e2e，各层按路径排序。
    index: int
    start: int
    # start 和 end 均包含在端口块内；offset 从 start 起算。
    end: int

    @property
    def backend_port(self) -> int:
        return self.start

    def port(self, offset: int) -> int:
        if offset < 0 or offset >= TEST_FILE_PORT_BLOCK_SIZE or self.start + offset > self.end:
            raise ValueError(
                f"测试文件端口偏移超出端口块: offset={offset}, block_size={TEST_FILE_PORT_BLOCK_SIZE}, "
                f"start={self.start}, end={self.end}"
            )
        return self.start + offset


def port_block_for_file(
    test_file_path: Path,
    *,
    test_layer: Literal["integration", "e2e"],
    tests_root: Path,
) -> TestFilePortBlock:
    resolved_tests_root = tests_root.resolve()
    resolved_file = test_file_path.resolve()
    if not resolved_file.is_relative_to(resolved_tests_root / test_layer):
        raise ValueError(f"{test_layer} 测试文件不在指定测试层: {resolved_file}")
    test_file_count, first_index_by_path = _test_file_indexes_for_tests_root(
        resolved_tests_root
    )
    index = first_index_by_path.get(resolved_file)
    if index is None:
        raise ValueError(f"未能为 {test_layer} 测试文件分配端口块: {resolved_file}")

    if test_file_count > TEST_FILE_PORT_MAX_FILES:
        raise RuntimeError(
            "integration/e2e 测试文件总数超过端口块容量: "
            f"count={test_file_count}, max={TEST_FILE_PORT_MAX_FILES}"
        )

    remaining_index = index
    for range_start, range_end in TEST_FILE_PORT_RANGES:
        capacity = (range_end - range_start + 1) // TEST_FILE_PORT_BLOCK_SIZE
        if remaining_index < capacity:
            start = range_start + remaining_index * TEST_FILE_PORT_BLOCK_SIZE
            break
        remaining_index -= capacity
    else:
        raise RuntimeError(f"测试端口块索引超出容量: index={index}")
    return TestFilePortBlock(
        test_file=resolved_file,
        index=index,
        start=start,
        end=start + TEST_FILE_PORT_BLOCK_SIZE - 1,
    )


def e2e_port_block_for_file(
    test_file_path: Path,
    *,
    tests_root: Path | None = None,
) -> TestFilePortBlock:
    return port_block_for_file(
        test_file_path,
        test_layer="e2e",
        tests_root=(
            tests_root.resolve()
            if tests_root is not None
            else Path.cwd().resolve() / "tests"
        ),
    )


def integration_port_block_for_file(test_file_path: Path) -> TestFilePortBlock:
    return port_block_for_file(
        test_file_path,
        test_layer="integration",
        tests_root=Path.cwd().resolve() / "tests",
    )
