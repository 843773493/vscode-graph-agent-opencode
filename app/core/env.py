from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def get_project_root(start_path: Path | str | None = None) -> Path:
    """从给定起点向上查找项目根目录。优先使用 pyproject.toml，未找到时再回退到 AGENTS.md。"""
    current_path = Path(start_path or __file__).resolve()
    search_start = current_path.parent if current_path.is_file() else current_path

    # 优先查找 pyproject.toml
    for candidate in (search_start, *search_start.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate

    # 回退：查找 AGENTS.md
    for candidate in (search_start, *search_start.parents):
        if (candidate / "AGENTS.md").exists():
            return candidate

    raise FileNotFoundError(f"无法定位项目根目录，请从项目内路径调用: {current_path}")


def load_project_env(
    start_path: Path | str | None = None,
    *,
    override: bool = False,
    required: bool = False,
) -> Path | None:
    """加载项目根目录下的 .env 文件。"""
    project_root = get_project_root(start_path)
    env_file = project_root / ".env"

    print(f"[Env] 加载 .env 文件: {env_file.resolve()}")

    if not env_file.exists():
        if required:
            raise FileNotFoundError(f"未找到项目环境文件: {env_file}")

        return None

    load_dotenv(env_file, override=override)
    return env_file