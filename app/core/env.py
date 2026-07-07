from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def get_project_root(project_root: Path | str | None = None) -> Path:
    """解析项目根目录。

    默认使用当前运行目录，也可以通过显式路径或 BOXTEAM_PROJECT_ROOT 指定。
    这里不再根据当前文件位置向上推导仓库根目录，避免框架层隐式依赖源码布局。
    """
    configured_root = project_root or os.environ.get("BOXTEAM_PROJECT_ROOT") or Path.cwd()
    resolved_root = Path(configured_root).expanduser().resolve()
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"项目根目录必须是目录: {resolved_root}")
    if not (resolved_root / "pyproject.toml").exists():
        raise FileNotFoundError(
            "无法定位项目根目录，请从项目根目录启动后端，"
            f"或通过 BOXTEAM_PROJECT_ROOT 显式指定。当前路径: {resolved_root}"
        )
    return resolved_root


def load_project_env(
    project_root: Path | str | None = None,
    *,
    override: bool = False,
    required: bool = False,
) -> Path | None:
    """加载项目根目录下的 .env 文件。"""
    resolved_project_root = get_project_root(project_root)
    env_file = resolved_project_root / ".env"

    print(f"[Env] 加载 .env 文件: {env_file.resolve()}")

    if not env_file.exists():
        if required:
            raise FileNotFoundError(f"未找到项目环境文件: {env_file}")

        return None

    load_dotenv(env_file, override=override)
    return env_file
