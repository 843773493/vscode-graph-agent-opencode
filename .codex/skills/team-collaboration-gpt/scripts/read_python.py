"""按 Python 符号或解码字符串片段读取源码，保留缩进与可核验来源。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


def symbols(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    result: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = f"{prefix}.{node.name}" if prefix else node.name
            result.append((prefix, node))
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(tree, "")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--symbol", action="append", help="仅传一次：完整限定名，或在本文件中唯一的符号短名")
    selection.add_argument("--symbols", action="store_true", help="只列符号名和源码行范围")
    selection.add_argument("--string-match", help="在唯一字符串常量中定位一行；不执行 Python 或 SQL")
    parser.add_argument("--before", type=int, default=2)
    parser.add_argument("--after", type=int, default=24)
    parser.add_argument("--max-output-bytes", type=int, default=16384)
    args = parser.parse_args()
    symbol_names = args.symbol or []
    if len(symbol_names) > 1:
        parser.error("每次读取只能传一个 --symbol；重复参数不能代表读齐多个符号")
    symbol_name = symbol_names[0] if symbol_names else None
    path = args.path.resolve(strict=True)
    if not path.is_relative_to(Path.cwd().resolve()) or ".boxteam" in path.parts:
        parser.error("输入必须是当前仓库内的 Python 源码，不得读取业务数据")
    if path.suffix != ".py" or args.max_output_bytes <= 0:
        parser.error("输入必须为 Python 文件，输出预算必须为正数")
    if not 0 <= args.before <= 120 or not 0 <= args.after <= 120:
        parser.error("字符串上下文行数必须在0至120之间")
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        parser.error("该入口只读取不超过4 MiB的源码")
    text = raw.decode("utf-8")
    tree = ast.parse(text, filename=str(path))
    metadata: dict[str, object] = {"source": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    display: list[str] = []
    if args.symbols:
        metadata["symbols"] = [
            {"name": name, "line": node.lineno, "end_line": node.end_lineno}
            for name, node in symbols(tree)
        ]
    elif symbol_name:
        candidates = [(name, node) for name, node in symbols(tree) if name == symbol_name]
        if not candidates:
            candidates = [(name, node) for name, node in symbols(tree) if name.rsplit(".", 1)[-1] == symbol_name]
        if len(candidates) != 1:
            raise LookupError(f"符号必须唯一：{symbol_name!r}，匹配数={len(candidates)}")
        name, node = candidates[0]
        start = min([node.lineno] + [item.lineno for item in node.decorator_list])
        metadata["symbol"] = name
        metadata["line"] = start
        metadata["end_line"] = node.end_lineno
        display = [f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1) if start <= i <= node.end_lineno]
    else:
        if not args.string_match or "\n" in args.string_match:
            parser.error("字符串定位必须为非空的单行文本")
        constants = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and args.string_match in node.value
        ]
        if len(constants) != 1:
            raise LookupError(f"字符串常量必须唯一：{args.string_match!r}，匹配数={len(constants)}")
        constant = constants[0]
        lines = constant.value.splitlines()
        matches = [i for i, line in enumerate(lines) if args.string_match in line]
        if len(matches) != 1:
            raise LookupError(f"解码字符串中的定位行必须唯一，匹配数={len(matches)}")
        position = matches[0]
        start, end = max(0, position - args.before), min(len(lines), position + args.after + 1)
        metadata.update({"constant_source_line": constant.lineno, "decoded_line_count": len(lines),
                         "selected_decoded_lines": [start + 1, end], "string_match": args.string_match})
        display = [f"字符串第{i + 1}行: {lines[i]}" for i in range(start, end)]
    output = "\n".join([json.dumps(metadata, ensure_ascii=False), *display])
    size = len(output.encode("utf-8"))
    if size > args.max_output_bytes:
        parser.error(f"选中源码为{size}字节，超出输出预算；请细化符号或用--string-match定位解码字符串")
    print(output)


if __name__ == "__main__":
    main()
