from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

E2E_PORT_START = 18800
E2E_PORT_BLOCK_SIZE = 100
E2E_PORT_MAX_FILES = 100
E2E_PORT_END = E2E_PORT_START + E2E_PORT_BLOCK_SIZE * E2E_PORT_MAX_FILES - 1
INTEGRATION_PORT_START = 28800
INTEGRATION_PORT_END = (
    INTEGRATION_PORT_START + E2E_PORT_BLOCK_SIZE * E2E_PORT_MAX_FILES - 1
)


@dataclass(frozen=True, slots=True)
class E2EPortBlock:
    test_file: Path
    index: int
    start: int
    end: int

    @property
    def backend_port(self) -> int:
        return self.start

    def port(self, offset: int) -> int:
        if offset < 0 or offset >= E2E_PORT_BLOCK_SIZE:
            raise ValueError(
                f"e2e 端口偏移超出文件端口块: offset={offset}, block_size={E2E_PORT_BLOCK_SIZE}"
            )
        return self.start + offset


def port_block_for_file(
    test_file_path: Path,
    *,
    test_layer: str,
    port_start: int,
    tests_root: Path,
) -> E2EPortBlock:
    root = tests_root.resolve()
    resolved_file = test_file_path.resolve()
    test_files = sorted(path.resolve() for path in root.rglob("test_*.py"))
    if resolved_file not in test_files:
        raise ValueError(f"未能为 {test_layer} 测试文件分配端口块: {resolved_file}")

    index = test_files.index(resolved_file)
    if index >= E2E_PORT_MAX_FILES:
        raise RuntimeError(
            f"{test_layer} 测试文件数量超过端口块容量: "
            f"count={len(test_files)}, max={E2E_PORT_MAX_FILES}"
        )

    start = port_start + index * E2E_PORT_BLOCK_SIZE
    return E2EPortBlock(
        test_file=resolved_file,
        index=index,
        start=start,
        end=start + E2E_PORT_BLOCK_SIZE - 1,
    )


def e2e_port_block_for_file(
    test_file_path: Path,
    *,
    tests_root: Path | None = None,
) -> E2EPortBlock:
    return port_block_for_file(
        test_file_path,
        test_layer="e2e",
        port_start=E2E_PORT_START,
        tests_root=(
            tests_root.resolve()
            if tests_root is not None
            else Path.cwd().resolve() / "tests" / "e2e"
        ),
    )


def integration_port_block_for_file(test_file_path: Path) -> E2EPortBlock:
    return port_block_for_file(
        test_file_path,
        test_layer="integration",
        port_start=INTEGRATION_PORT_START,
        tests_root=Path.cwd().resolve() / "tests" / "integration",
    )
