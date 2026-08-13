from __future__ import annotations

import json

_LOGPOINT_OUTPUT_PREFIX = "__BOXTEAM_NODE_LOGPOINT__"
_HIT_COUNT_SYMBOL = "boxteam.nodeDebug.hitCounts"


def inspector_breakpoint_condition(
    *,
    breakpoint_id: str,
    condition: str | None,
    hit_condition: int | None,
    log_message: str | None,
) -> str | None:
    """将特殊断点语义编译为 Node Inspector condition。"""

    normalized_condition = condition.strip() if condition is not None else ""
    if hit_condition is None and log_message is None:
        return normalized_condition or None

    statements: list[str] = []
    predicates: list[str] = []
    if hit_condition is not None:
        key = json.dumps(breakpoint_id, ensure_ascii=False)
        symbol = json.dumps(_HIT_COUNT_SYMBOL)
        statements.extend(
            [
                f"const store = globalThis[Symbol.for({symbol})] ??= Object.create(null);",
                f"const hit = store[{key}] = (store[{key}] ?? 0) + 1;",
            ]
        )
        predicates.append(f"hit === {hit_condition}")
    if normalized_condition:
        predicates.append(f"Boolean(({normalized_condition}))")
    predicate = " && ".join(predicates) or "true"

    if log_message is None:
        statements.append(f"return {predicate};")
    else:
        message_expression = _compile_log_message(log_message)
        prefix = json.dumps(_LOGPOINT_OUTPUT_PREFIX)
        statements.extend(
            [
                f"if ({predicate}) {{",
                f"process.stdout.write({prefix} + ({message_expression}) + '\\n');",
                "}",
                "return false;",
            ]
        )
    return "(() => { " + " ".join(statements) + " })()"


def parse_logpoint_output(text: str) -> str | None:
    if not text.startswith(_LOGPOINT_OUTPUT_PREFIX):
        return None
    return text.removeprefix(_LOGPOINT_OUTPUT_PREFIX)


def _compile_log_message(message: str) -> str:
    if not message.strip():
        raise ValueError("日志点 log_message 不能为空")
    parts: list[str] = []
    literal: list[str] = []
    index = 0
    while index < len(message):
        character = message[index]
        following = message[index + 1] if index + 1 < len(message) else ""
        if character == "{" and following == "{":
            literal.append("{")
            index += 2
            continue
        if character == "}" and following == "}":
            literal.append("}")
            index += 2
            continue
        if character == "}":
            raise ValueError("日志点消息包含未匹配的右花括号；字面量请写成 }}")
        if character != "{":
            literal.append(character)
            index += 1
            continue
        if literal:
            parts.append(json.dumps("".join(literal), ensure_ascii=False))
            literal.clear()
        closing = _find_expression_end(message, index + 1)
        expression = message[index + 1 : closing].strip()
        if not expression:
            raise ValueError("日志点插值表达式不能为空")
        parts.append(f"String(({expression}))")
        index = closing + 1
    if literal:
        parts.append(json.dumps("".join(literal), ensure_ascii=False))
    return " + ".join(parts) if parts else '""'


def _find_expression_end(message: str, start: int) -> int:
    quote: str | None = None
    escaped = False
    nested_braces = 0
    for index in range(start, len(message)):
        character = message[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"', "`"}:
            quote = character
            continue
        if character == "{":
            nested_braces += 1
            continue
        if character == "}" and nested_braces > 0:
            nested_braces -= 1
            continue
        if character == "}":
            return index
    raise ValueError("日志点消息包含未闭合的插值表达式")


__all__ = ["inspector_breakpoint_condition", "parse_logpoint_output"]
