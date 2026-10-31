from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.gateway.main import app as gateway_app
from app.main import app
from app.protocol.openapi import assert_gateway_http_schemas, assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set


def main() -> None:
    """核对公开结构后，把当前 FastAPI 路由导出为静态 OpenAPI 快照。"""

    parser = argparse.ArgumentParser(description="核对 proto 并导出当前 HTTP OpenAPI")
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    parser.add_argument("--descriptor-set", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").is_file():
        raise RuntimeError(f"必须从项目根目录运行: cwd={project_root}")

    document = app.openapi()
    pool = descriptor_pool_from_set(args.descriptor_set.read_bytes()) if args.descriptor_set else None
    assert_public_http_schemas(document, pool)
    assert_gateway_http_schemas(gateway_app.openapi(), pool)
    if args.validate_only:
        return

    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    targets = (
        args.output_root / "src" / "clients" / "web" / "openapi.json",
        args.output_root / "src" / "clients" / "web" / "src" / "types" / "openapi" / "index.json",
    )
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
