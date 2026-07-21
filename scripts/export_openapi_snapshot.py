from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    """把当前 FastAPI 路由导出为 Web 侧的静态 OpenAPI 快照。"""

    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").is_file():
        raise RuntimeError(f"必须从项目根目录运行: cwd={project_root}")

    payload = json.dumps(
        app.openapi(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    targets = (
        project_root / "src" / "web" / "openapi.json",
        project_root / "src" / "web" / "src" / "types" / "gen" / "openapi" / "index.json",
    )
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
