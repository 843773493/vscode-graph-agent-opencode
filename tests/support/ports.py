from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

TEST_FILE_PORT_BLOCK_SIZE = 100
E2E_PORT_MAX_FILES = 100
INTEGRATION_PORT_START = 8800
INTEGRATION_PORT_MAX_FILES = 160
INTEGRATION_PORT_END = (
    INTEGRATION_PORT_START
    + TEST_FILE_PORT_BLOCK_SIZE * INTEGRATION_PORT_MAX_FILES
    - 1
)
E2E_PORT_START = INTEGRATION_PORT_END + 1
E2E_PORT_END = E2E_PORT_START + TEST_FILE_PORT_BLOCK_SIZE * E2E_PORT_MAX_FILES - 1


@lru_cache(maxsize=16)
def _test_files_for_root(root: Path) -> tuple[Path, ...]:
    """pytest 单次运行内测试树固定，复用按测试文件排序的目录扫描。"""
    return tuple(sorted(path.resolve() for path in root.rglob("test_*.py")))


@dataclass(frozen=True, slots=True)
class TestFilePortBlock:
    """一个集成测试或 E2E 测试文件独占的连续端口块。"""

    __test__: ClassVar[bool] = False
    test_file: Path
    # index 是所属测试层全部文件按解析后绝对路径排序的零基序号。
    index: int
    start: int
    # start 和 end 均包含在端口块内；offset 从 start 起算。
    end: int

    @property
    def backend_port(self) -> int:
        return self.start

    def port(self, offset: int) -> int:
        if offset < 0 or offset >= TEST_FILE_PORT_BLOCK_SIZE:
            raise ValueError(
                f"测试文件端口偏移超出端口块: offset={offset}, block_size={TEST_FILE_PORT_BLOCK_SIZE}"
            )
        return self.start + offset


def port_block_for_file(
    test_file_path: Path,
    *,
    test_layer: str,
    port_start: int,
    max_files: int,
    tests_root: Path,
) -> TestFilePortBlock:
    root = tests_root.resolve()
    resolved_file = test_file_path.resolve()
    test_files = _test_files_for_root(root)
    if resolved_file not in test_files:
        raise ValueError(f"未能为 {test_layer} 测试文件分配端口块: {resolved_file}")

    index = test_files.index(resolved_file)
    if index >= max_files:
        raise RuntimeError(
            f"{test_layer} 测试文件数量超过端口块容量: "
            f"count={len(test_files)}, max={max_files}"
        )

    start = port_start + index * TEST_FILE_PORT_BLOCK_SIZE
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
        port_start=E2E_PORT_START,
        max_files=E2E_PORT_MAX_FILES,
        tests_root=(
            tests_root.resolve()
            if tests_root is not None
            else Path.cwd().resolve() / "tests" / "e2e"
        ),
    )


def integration_port_block_for_file(test_file_path: Path) -> TestFilePortBlock:
    return port_block_for_file(
        test_file_path,
        test_layer="integration",
        port_start=INTEGRATION_PORT_START,
        max_files=INTEGRATION_PORT_MAX_FILES,
        tests_root=Path.cwd().resolve() / "tests" / "integration",
    )
