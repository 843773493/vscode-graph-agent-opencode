from __future__ import annotations

import json
import re
from typing import Any

from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.tool_result_text import serialize_tool_value


def looks_like_unexecuted_tool_call_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if "<TOOLCALL>" in stripped:
        return True
    if "arguments" not in stripped:
        return False
    if re.match(r'^(?:[\[{]\s*)?"?name"?\s*:', stripped) is not None:
        return True
    return (
        stripped[0] in "[{"
        and '"name"' in stripped
        and ('"arguments"' in stripped or '"args"' in stripped)
    )


def normalize_pseudo_tool_call_payload(value: Any) -> tuple[str, dict[str, Any]] | None:
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if not isinstance(value, dict):
        return None

    function_payload = value.get("function")
    if isinstance(function_payload, dict):
        name = function_payload.get("name")
        args = function_payload.get("arguments", {})
    else:
        name = value.get("name")
        args = value.get("arguments", value.get("args", {}))

    if not isinstance(name, str) or not name:
        return None
    if isinstance(args, str):
        args = json.loads(args)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise TypeError(f"伪工具调用 arguments 应为 object，实际类型: {type(args).__name__}")
    return name, args


def extract_json_object_after(source: str, start_index: int) -> dict[str, Any] | None:
    depth = 0
    in_string = False
    escaped = False
    object_start = -1
    for index in range(start_index, len(source)):
        char = source[index]
        if object_start < 0:
            if char == "{":
                object_start = index
                depth = 1
            continue

        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(source[object_start : index + 1])
                if not isinstance(parsed, dict):
                    raise TypeError("伪工具调用 arguments JSON 解析后不是 object")
                return parsed
    return None


def extract_pseudo_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    if not looks_like_unexecuted_tool_call_text(text):
        return None

    stripped = text.strip()
    if "<TOOLCALL>" in stripped:
        stripped = stripped.split("<TOOLCALL>", 1)[1].strip()

    decoder = json.JSONDecoder()
    candidates = [stripped]
    if stripped.startswith('name":'):
        candidates.append('{"' + stripped)
    if stripped.startswith('"name"') or stripped.startswith('"function"'):
        candidates.append("{" + stripped)

    for candidate in candidates:
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        normalized = normalize_pseudo_tool_call_payload(parsed)
        if normalized is not None:
            return normalized

    name_match = re.search(r'"?name"?\s*:\s*"([^"]+)"', stripped)
    if name_match is None:
        return None
    args_match = re.search(r'"?(?:arguments|args)"?\s*:', stripped)
    if args_match is None:
        return name_match.group(1), {}
    args = extract_json_object_after(stripped, args_match.end())
    return name_match.group(1), args or {}


def find_agent_tool(agent: Any, tool_name: str) -> Any | None:
    return extract_agent_tools_by_name(agent).get(tool_name)


def format_recovered_tool_result(tool_name: str, result: Any) -> str:
    if tool_name != "persistent_terminal" or not isinstance(result, dict):
        return serialize_tool_value(result)

    status = result.get("status")
    command = result.get("command")
    terminal_id = result.get("terminal_id")
    attach_url = result.get("attach_url")
    exit_code = result.get("exit_code")
    output = result.get("output") or result.get("recent_output")

    if status == "completed":
        lines = ["命令已在持久终端中执行完成。"]
    elif status == "background":
        lines = ["命令仍在运行，已转入可 attach 的后台终端。"]
    else:
        lines = [f"持久终端工具已执行，状态：{status or '未知'}。"]

    if command:
        lines.append(f"命令：`{command}`")
    if terminal_id:
        lines.append(f"终端 UUID：`{terminal_id}`")
    if exit_code is not None:
        lines.append(f"退出码：{exit_code}")
    if output:
        lines.append("输出：")
        lines.append(f"```text\n{output}\n```")
    if attach_url:
        lines.append(f"可从资源视图打开终端，也可以访问：{attach_url}")
    return "\n\n".join(lines)


def safe_final_text(text: str) -> str:
    if not looks_like_unexecuted_tool_call_text(text):
        return text
    return (
        "系统已拦截一段未执行的工具调用文本。"
        "请重新发送请求，或更明确地要求 agent 使用工具调用通道。"
    )
