"""JSONC 来源的脱敏往返；只替换秘密字符串 token，保留其余原始字节。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from commentjson.commentjson import parser
from lark import Token, Tree
from pydantic import ValidationError

from app.core.config_sources import StableConfigFile, parse_stable_config_file
from app.schemas.internal_v2.mcp import McpPrivateEnvRefDTO, McpPrivateHeaderRefDTO
from app.services.infrastructure.config.state import redact_config_payload

_MASK_PREFIX = "boxteam-redacted:"


def parse_source_text(text: str) -> dict[str, object]:
    # 解析异常的正文隔离与现有文件入口共用；虚拟位置不进入公开结果。
    payload = parse_stable_config_file(StableConfigFile(
        path=Path.cwd(), presence="present", raw_bytes=text.encode("utf-8"), digest=None,
    ))
    if payload is None:
        raise RuntimeError("来源正文解析未返回对象")
    return payload


def set_source_value(text: str, path: tuple[str, ...], value: object) -> str:
    """用原 JSONC 语法树和 lexer 定位单个成员，保留其余注释、格式与秘密占位。"""
    parse_source_text(text)
    if not path:
        raise ValueError("来源编辑必须指定成员路径")
    tree = parser.parse(text)
    tokens = list(parser.lex(text))
    indices = {token.pos_in_stream: index for index, token in enumerate(tokens)}
    closing: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.type in {"LBRACE", "LSQB"}:
            stack.append(index)
        elif token.type in {"RBRACE", "RSQB"}:
            closing[stack.pop()] = index
    opening = 0
    newline = "\r\n" if "\r\n" in text else "\n"
    for depth, part in enumerate(path):
        if tree.data != "object":
            raise ValueError("来源编辑的祖先成员必须是对象")
        pairs = [child for child in tree.children if isinstance(child, Tree)]
        matches = [pair for pair in pairs if json.loads(str(pair.children[0].children[0])) == part]
        if len(matches) > 1:
            raise ValueError("来源编辑路径存在重复成员，不能猜测目标")
        position = tokens[opening].pos_in_stream
        prefix = text[text.rfind("\n", 0, position) + 1:position]
        indent = prefix[:len(prefix) - len(prefix.lstrip(" \t"))]
        if not matches:
            nested = value
            for key in reversed(path[depth + 1:]):
                nested = {key: nested}
            fragment = json.dumps({part: nested}, ensure_ascii=False, indent=2)[1:-1]
            fragment = fragment.replace("\n", newline + indent)
            start = tokens[opening].pos_in_stream + 1
            return text[:start] + fragment.rstrip() + ("," if pairs else "") + newline + indent + text[start:]
        pair, = matches
        key_token = pair.children[0].children[0]
        opening = indices[key_token.pos_in_stream] + 2  # key、冒号、value 均由同一官方 lexer 提供。
        if depth == len(path) - 1:
            end = tokens[closing.get(opening, opening)]
            start = tokens[opening].pos_in_stream
            position = key_token.pos_in_stream
            prefix = text[text.rfind("\n", 0, position) + 1:position]
            indent = prefix[:len(prefix) - len(prefix.lstrip(" \t"))]
            replacement = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", newline + indent)
            return text[:start] + replacement + text[end.pos_in_stream + len(end):]
        tree = pair.children[1]
    raise RuntimeError("来源成员编辑未取得目标")


def _strings(node: Tree, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Token]]:
    if node.data == "string":
        yield path, node.children[0]
    elif node.data == "object":
        for pair in node.children:
            if isinstance(pair, Tree):
                key, value = pair.children
                yield from _strings(value, (*path, json.loads(str(key.children[0]))))
    elif node.data == "array":
        for index, item in enumerate(child for child in node.children if isinstance(child, Tree)):
            yield from _strings(item, (*path, str(index)))


def _secret(path: tuple[str, ...], value: str) -> bool:
    if not path:
        return False
    if redact_config_payload({path[-1]: value}) != {path[-1]: value}:
        return True
    # 原 MCP env/headers 和 Provider headers 是任意键的秘密输入，不能只识别 password 等键名。
    return ((len(path) >= 5 and path[:2] == ("mcp", "servers") and path[3] in {"env", "headers"})
            or (len(path) >= 5 and path[:2] == ("llm", "providers") and path[3] == "default_headers"))


def redact_source_payload(value: object, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        if len(path) == 5 and path[:2] == ("mcp", "servers") and path[3] in {"env", "headers"}:
            try:
                model = McpPrivateEnvRefDTO if path[3] == "env" else McpPrivateHeaderRefDTO
                reference = model.model_validate(value)
            except ValidationError:
                pass  # 非引用对象继续逐叶脱敏，不能借对象外形公开任意秘密。
            else:
                return reference.model_dump(mode="json")
        return {str(key): redact_source_payload(item, (*path, str(key))) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_source_payload(item, (*path, str(index))) for index, item in enumerate(value)]
    if isinstance(value, str) and _secret(path, value):
        return _MASK_PREFIX + "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)
    return value


def _replace_tokens(text: str, replacements: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _at_path(payload: object, path: tuple[str, ...]) -> object:
    for part in path:
        if isinstance(payload, list):
            payload = payload[int(part)]
        else:
            payload = cast(dict[str, object], payload)[part]
    return payload


def redact_source_text(text: str) -> str:
    redacted = redact_source_payload(parse_source_text(text))
    replacements = []
    for path, token in _strings(parser.parse(text)):
        value = json.loads(str(token))
        replacement_value = _at_path(redacted, path)
        if replacement_value != value:
            replacement = json.dumps(replacement_value, ensure_ascii=False)
            replacements.append((token.pos_in_stream, token.pos_in_stream + len(token), replacement))
    return _replace_tokens(text, replacements)


def restore_source_text(text: str, original: str) -> str:
    """占位只能在相同路径、相同对象绑定中还原，绝不把遮罩当密码保存。"""
    incoming, previous = parse_source_text(text), parse_source_text(original)
    redacted_previous = redact_source_payload(previous)
    originals = {path: (json.loads(str(token)), str(token)) for path, token in _strings(parser.parse(original))}
    replacements = []
    for path, token in _strings(parser.parse(text)):
        value = json.loads(str(token))
        if not _secret(path, value) or not value.startswith(_MASK_PREFIX):
            continue
        old = originals.get(path)
        if old is None or _at_path(redacted_previous, path) != value:
            raise ValueError("秘密脱敏占位不属于当前来源路径，必须重新读取原目标")
        # 数组元素重排、Server/Provider 换身份后不能沿旧位置借用秘密。
        left: object = previous
        right: object = incoming
        for part in (*path[:-1], None):
            if isinstance(left, dict) and isinstance(right, dict):
                for identity in ("id", "url", "endpoint", "command", "transport"):
                    if left.get(identity) != right.get(identity):
                        raise ValueError("秘密所属对象或地址已改变，不能沿用原脱敏占位")
                if part is not None:
                    left, right = left[part], right[part]
            elif isinstance(left, list) and isinstance(right, list) and part is not None:
                left, right = cast(list[object], left)[int(part)], cast(list[object], right)[int(part)]
            else:
                raise ValueError("秘密所属对象结构已改变，不能沿用原脱敏占位")
        replacements.append((token.pos_in_stream, token.pos_in_stream + len(token), old[1]))
    return _replace_tokens(text, replacements)


def source_origins(layers: dict[str, dict[str, object]], effective: dict[str, object]) -> list[dict[str, object]]:
    """只标注原 merge 结果的叶值来源；数组沿原合同作为一个替换单元。"""
    result: list[dict[str, object]] = []

    def visit(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, dict) and value:
            for key, child in value.items():
                visit(child, (*path, str(key)))
            return
        sources = []
        for key, payload in layers.items():
            item: object = payload
            for part in path:
                if not isinstance(item, dict) or part not in item:
                    break
                item = item[part]
            else:
                sources.append(key)
        if sources:
            result.append({"path": "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path),
                           "source_key": sources[-1], "shadowed_sources": sources[:-1]})

    visit(effective, ())
    return result
