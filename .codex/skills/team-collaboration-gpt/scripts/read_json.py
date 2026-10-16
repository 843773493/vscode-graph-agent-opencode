"""按 JSON Pointer 读取源码或合同节点，避免整行配置淹没实际调用方。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def resolve_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError(f"非法 JSON Pointer：{pointer!r}")
    node = document
    for encoded in pointer[1:].split("/"):
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node[key]
        elif isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            node = node[int(key)]
        else:
            raise TypeError(f"JSON Pointer 无法继续定位：{pointer!r}，段={key!r}")
    return node


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--pointer", action="append", required=True)
    parser.add_argument("--keys", action="store_true", help="只输出对象键或数组长度")
    parser.add_argument("--max-output-bytes", type=int, default=16384)
    args = parser.parse_args()
    path = args.path.resolve(strict=True)
    if not path.is_relative_to(Path.cwd().resolve()) or ".boxteam" in path.parts:
        parser.error("输入必须是当前仓库内的源码或合同 JSON，不得读取业务数据")
    if path.suffix.lower() != ".json" or args.max_output_bytes <= 0:
        parser.error("输入必须为 JSON 文件，输出预算必须为正数")
    with path.open("rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        parser.error("该入口只读取不超过 4 MiB 的源码或合同 JSON")
    document = json.loads(data)
    nodes = []
    profile_fields = {
        "workdir", "project_root", "artifacts", "max_seconds", "task_input",
        "source_paths", "allowed_options", "pytest_node_ids",
    }
    for pointer in args.pointer:
        if path.name.startswith("run-profile") and not args.keys:
            first_key = pointer.split("/")[1] if pointer.startswith("/") else ""
            if first_key not in profile_fields:
                parser.error("运行配置只允许读取必要坐标；其他字段请用 --keys 查看元数据")
        node = resolve_pointer(document, pointer)
        if args.keys:
            if isinstance(node, dict):
                node = {"type": "object", "length": len(node), "keys": sorted(node)}
            elif isinstance(node, list):
                node = {"type": "array", "length": len(node)}
            else:
                raise TypeError(f"--keys 要求对象或数组：{pointer!r}")
        nodes.append({"pointer": pointer, "value": node})
    output = json.dumps(
        {"source": str(path), "sha256": hashlib.sha256(data).hexdigest(), "nodes": nodes},
        ensure_ascii=False, indent=2,
    )
    size = len(output.encode("utf-8"))
    if size > args.max_output_bytes:
        parser.error(f"选中节点为 {size} 字节，超出输出预算；请细化 --pointer 或使用 --keys")
    print(output)


if __name__ == "__main__":
    main()
