"""真实目录别名和 fd 生命周期；不启动 Chromium 或证明页面行为。"""

import errno
import os
from pathlib import Path

import pytest

from tests.support.paths import chromium_temporary_environment


@pytest.mark.parametrize("interrupted", [False, True])
def test_chromium_temporary_alias_releases_descriptor(
    tmp_path: Path, interrupted: bool,
) -> None:
    directory = tmp_path / "chromium-tmp"
    descriptor = None

    def consume() -> None:
        nonlocal descriptor
        with chromium_temporary_environment(directory) as environment:
            aliases = set(environment.values())
            assert len(aliases) == 1
            alias = Path(aliases.pop())
            assert alias.resolve() == directory.resolve()
            if Path("/proc/self/fd").is_dir():
                descriptor = int(alias.name)
                assert str(alias).startswith(f"/proc/{os.getpid()}/fd/")
            (alias / "profile.txt").write_text("本测试临时正文", encoding="utf-8")
            if interrupted:
                raise KeyboardInterrupt("模拟浏览器准备中断")

    if interrupted:
        with pytest.raises(KeyboardInterrupt, match="浏览器准备中断"):
            consume()
    else:
        consume()
    assert (directory / "profile.txt").read_text(encoding="utf-8") == "本测试临时正文"
    if descriptor is not None:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
