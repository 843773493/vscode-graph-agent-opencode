"""验证 Windows Chromium 短目录别名的原生归属与清理。"""

import os
from pathlib import Path

import pytest

from tests.support.paths import chromium_temporary_environment


@pytest.mark.skipif(os.name != "nt", reason="验证原生 Windows junction 与长路径")
@pytest.mark.parametrize("interrupted", [False, True])
def test_chromium_temporary_alias_uses_windows_junction(
    tmp_path: Path, interrupted: bool,
) -> None:
    temporary = tmp_path / ("long-directory-" * 12) / "chromium-tmp"
    alias: Path | None = None

    def use_alias() -> None:
        nonlocal alias
        with chromium_temporary_environment(temporary) as environment:
            alias = Path(environment["TEMP"])
            assert set(environment.values()) == {str(alias)}
            assert len(str(alias)) < len(str(temporary))
            assert alias.is_junction()
            assert alias.resolve() == temporary.resolve()
            (alias / "profile.txt").write_text("通过短别名访问", encoding="utf-8")
            if interrupted:
                raise KeyboardInterrupt("模拟浏览器准备中断")

    if interrupted:
        with pytest.raises(KeyboardInterrupt, match="浏览器准备中断"):
            use_alias()
    else:
        use_alias()

    assert (temporary / "profile.txt").read_text(encoding="utf-8") == "通过短别名访问"
    assert alias is not None
    assert not alias.parent.exists()
