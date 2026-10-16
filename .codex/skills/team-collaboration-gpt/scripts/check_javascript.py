"""使用固定 Bun Transpiler 入口解析 JS/TS 源码，不执行被检文件或导入依赖。"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

LOADERS = {".js": "js", ".mjs": "js", ".jsx": "jsx", ".ts": "ts", ".tsx": "tsx"}
PARSE_PROGRAM = """
import { readFileSync } from "node:fs";
const files = JSON.parse(await Bun.stdin.text());
let failed = false;
for (const file of files) {
  try {
    new Bun.Transpiler({ loader: file.loader }).transformSync(
      readFileSync(file.path, "utf8"),
    );
    console.log(`PARSE ${file.relative}`);
  } catch (error) {
    failed = true;
    console.error(`解析失败: ${file.relative}`);
    const diagnostics = Array.isArray(error.errors) ? error.errors : [error];
    for (const diagnostic of diagnostics) {
      const position = diagnostic.position;
      const location = position ? ` (${position.line}:${position.column})` : "";
      console.error(`${diagnostic.message}${location}`);
    }
  }
}
if (failed) process.exit(1);
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    if not (root / "pyproject.toml").is_file():
        raise ValueError("必须从项目物理根目录调用 JS/TS 解析入口")
    files = []
    for supplied in args.paths:
        path = supplied.resolve(strict=True)
        relative = path.relative_to(root)
        if not path.is_file() or path.suffix not in LOADERS:
            raise ValueError(f"只接受 JS/MJS/JSX/TS/TSX 文件: {relative}")
        files.append({
            "path": str(path), "relative": str(relative),
            "loader": LOADERS[path.suffix],
        })
    result = subprocess.run(
        ["bun", "--eval", PARSE_PROGRAM],
        input=json.dumps(files), text=True, check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
