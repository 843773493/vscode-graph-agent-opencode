"""异常公开摘要与诊断的纯提取原语；不改变异常或记录日志。"""

from __future__ import annotations

import json
import re
import traceback
from collections import deque
from collections.abc import Iterable
from itertools import chain, islice
from typing import TypedDict

from jsonschema import ValidationError as JsonSchemaError
from pydantic import ValidationError

PUBLIC_ERROR_BYTES = 16 * 1024
MESSAGE_BYTES = 8 * 1024
MAX_ISSUES = 32
MAX_DESCENDANTS = 8
MAX_DEPTH = 4
DIAGNOSTIC_BYTES = 64 * 1024
MAX_FRAMES = 64
_MARKER = "…[已截断]"
_SECRET = "[redacted]"
_AUTH = re.compile(r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_FIELDS = re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)[\"']?\s*[:=]\s*)([\"'])(.*?)\2|([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)[^&#\s]+")
_BARE = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)\s*[:=]\s*)[^\s,;\"\'&]+")
_USERINFO = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


class ValidationIssue(TypedDict):
    path: list[str | int]
    code: str
    message: str


class PublicError(TypedDict):
    code: str
    message: str
    details: dict[str, object]


def redact_failure_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """只使用当次调用持有的秘密；先脱敏再裁剪，避免泄露凭据残片。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, _SECRET)
    text = _AUTH.sub(lambda m: m[1] + ": " + _SECRET, text)
    text = _BEARER.sub("Bearer " + _SECRET, text)
    text = _FIELDS.sub(lambda m: (m[1] + m[2] + _SECRET + m[2]) if m[1] else m[4] + _SECRET, text)
    text = _BARE.sub(lambda m: m[1] + _SECRET, text)
    return _USERINFO.sub(lambda m: m[1] + _SECRET + "@", text)


def _clip(text: str, limit: int) -> str:
    limit = max(0, limit)
    # 只编码最多 limit 个字符，避免为超长 stderr 再分配完整 UTF-8 副本。
    prefix = text[:limit].encode("utf-8")
    if len(text) <= limit and len(prefix) <= limit:
        return text
    marker = _MARKER.encode("utf-8")[:limit]
    return prefix[:max(0, limit - len(marker))].decode("utf-8", errors="ignore") + marker.decode("utf-8", errors="ignore")


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _json_text(text: str, limit: int) -> str:
    candidate = _clip(text, limit)
    while _size(candidate) > limit:
        candidate = _clip(candidate, len(candidate.encode("utf-8")) // 2)
    return candidate


def validation_issues(error: BaseException, *, secrets: Iterable[str] = ()) -> list[ValidationIssue]:
    """按校验器原路径提取，不携带 input、context 或 schema。"""
    secrets = tuple(secrets)
    if isinstance(error, ValidationError):
        # Pydantic 仅提供列表 API；禁用输入/上下文，且只格式化前32项。
        raw = error.errors(include_input=False, include_context=False, include_url=False)
    elif isinstance(error, JsonSchemaError):
        code = str(error.validator or "unknown_validation")
        path = list(islice(error.absolute_path, 32))
        if len(error.absolute_path) > 32:
            path[-1] = _MARKER
        if code in {"additionalProperties", "unevaluatedProperties"}:
            path.append("<extra>")
        if code == "required" and isinstance(error.instance, dict):
            for field in islice(error.validator_value, 32):
                if field not in error.instance:
                    path.append(field)
                    break
        message = f"未满足 JSON schema {code} 约束"
        # 只取官方规则中的必要标量事实，不渲染输入或复制 enum/pattern/schema。
        constraint = error.validator_value
        if type(constraint) in (int, float, bool) or code == "type" and isinstance(constraint, str):
            message += ": " + _clip(str(constraint), 128)
        raw = ({"loc": path, "type": code, "msg": message},)
    else:
        return []
    result: list[ValidationIssue] = []
    for issue in islice(raw, MAX_ISSUES):
        path: list[str | int] = []
        path_bytes = 0
        location = issue["loc"]
        if issue["type"] == "extra_forbidden":
            location = (*location[:-1], "<extra>")
        for part in islice(location, 32):
            selected = part if isinstance(part, int) else _clip(redact_failure_text(str(part), secrets=secrets), 256)
            path_bytes += _size(selected)
            if path_bytes > 1024:
                path.append(_MARKER)
                break
            path.append(selected)
        if len(location) > 32 and path:
            path[-1] = _MARKER
        entry: ValidationIssue = {"path": path, "code": _clip(redact_failure_text(str(issue["type"]), secrets=secrets), 128),
                       "message": _clip(redact_failure_text(str(issue["msg"]), secrets=secrets), 1024)}
        if _size(result) + _size(entry) > MESSAGE_BYTES:
            break
        result.append(entry)
    return result


def bounded_failure_text(text: str, *, secrets: Iterable[str] = (), max_bytes: int = MESSAGE_BYTES) -> str:
    """已有领域说明沿同一脱敏与 UTF-8 预算处理。"""
    return _clip(redact_failure_text(text, secrets=secrets), min(max_bytes, MESSAGE_BYTES))


def failure_message(error: BaseException, *, secrets: Iterable[str] = (), max_bytes: int = MESSAGE_BYTES) -> str:
    secrets = tuple(secrets)
    if isinstance(error, (ValidationError, JsonSchemaError)):
        issues = validation_issues(error, secrets=secrets)
        message = "; ".join(f"{issue['path']}: {issue['message']} ({issue['code']})" for issue in issues)
        if isinstance(error, ValidationError) and error.error_count() > MAX_ISSUES:
            message += _MARKER
    else:
        message = str(error)
    return bounded_failure_text(message or "未提供错误文本", secrets=secrets, max_bytes=max_bytes)


def public_error(error: BaseException, *, code: str = "unknown_error", stage: str | None = None,
                 secrets: Iterable[str] = (), max_bytes: int = PUBLIC_ERROR_BYTES) -> PublicError:
    """构造既有公共 Error 形状，预算不足立即停止原因遍历。"""
    secrets = tuple(secrets)
    limit = min(max_bytes, PUBLIC_ERROR_BYTES)
    if limit < 1024:
        raise ValueError("公共错误预算至少需要1024字节")
    remaining = MAX_DESCENDANTS
    seen: set[int] = set()

    def build(current: BaseException, depth: int, budget: int, current_code: str) -> PublicError:
        nonlocal remaining
        seen.add(id(current))
        details: dict[str, object] = {"error_type": _json_text(redact_failure_text(type(current).__name__, secrets=secrets), 128)}
        if stage and depth == 0:
            details["stage"] = _json_text(redact_failure_text(stage, secrets=secrets), 256)
        result: PublicError = {"code": _json_text(redact_failure_text(current_code, secrets=secrets), 256),
                               "message": failure_message(current, secrets=secrets, max_bytes=min(MESSAGE_BYTES, budget // 2)),
                               "details": details}
        # 为 JSON 转义与截断事实预留空间；仅序列化已经有界的候选。
        while _size(result) + 64 > budget:
            result["message"] = _clip(result["message"], max(0, len(result["message"].encode("utf-8")) // 2))
            details["truncated"] = True
        if any(_MARKER in value for value in (result["message"], result["code"], str(details.get("stage", "")))):
            details["truncated"] = True
        issues: list[ValidationIssue] = []
        for issue in validation_issues(current, secrets=secrets):
            if _size(result) + _size(issue) + 80 > budget:
                details["truncated"] = True
                break
            if _MARKER in str(issue):
                details["truncated"] = True
            issues.append(issue)
            details["issues"] = issues
        if isinstance(current, ValidationError) and current.error_count() > len(issues):
            details["truncated"] = True
        children: Iterable[tuple[str, BaseException]] = ()
        cause = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if isinstance(current, BaseExceptionGroup):
            children = (("errors", child) for child in current.exceptions)
        if cause is not None:
            children = chain((("cause", cause),), children)
        for key, child in children:
            available = budget - _size(result) - 80
            if remaining == 0 or depth >= MAX_DEPTH or available < 1024 or id(child) in seen:
                details["truncated"] = True
                break
            remaining -= 1
            nested = build(child, depth + 1, available, "unknown_error")
            if nested["details"].get("truncated"):
                details["truncated"] = True
            if key == "cause":
                details[key] = nested
            else:
                existing = details.setdefault("errors", [])
                assert isinstance(existing, list)
                existing.append(nested)
        return result

    return build(error, 0, limit, code)


def format_failure_diagnostic(error: BaseException, *, secrets: Iterable[str] = (),
                              max_bytes: int = DIAGNOSTIC_BYTES, max_frames: int = MAX_FRAMES) -> str:
    """只格式化已选节点与帧，不采集 locals，不递归渲染完整异常链。"""
    secrets = tuple(secrets)
    budget = min(max_bytes, DIAGNOSTIC_BYTES)
    frames = min(max_frames, MAX_FRAMES)
    parts: list[str] = []
    seen: set[int] = set()
    pending = [(error, 0, "")]
    truncated = False
    while pending and budget > len(_MARKER.encode("utf-8")):
        current, depth, label = pending.pop()
        if id(current) in seen:
            truncated = True
            continue
        seen.add(id(current))
        text = label + type(current).__name__ + ": " + failure_message(current, secrets=secrets) + "\n"
        selected = _clip(text, max(0, budget - len(_MARKER.encode("utf-8"))))
        parts.append(selected)
        budget -= len(selected.encode("utf-8"))
        if selected != text:
            truncated = True
            break
        notes = getattr(current, "__notes__", ())
        for note in islice(notes, MAX_DESCENDANTS):
            text = redact_failure_text("说明: " + note + "\n", secrets=secrets)
            selected = _clip(text, max(0, budget - len(_MARKER.encode("utf-8"))))
            parts.append(selected)
            budget -= len(selected.encode("utf-8"))
            if selected != text:
                truncated = True
                break
        truncated |= len(notes) > MAX_DESCENDANTS
        tb = current.__traceback__
        selected_frames = deque(maxlen=max(0, frames))
        frame_count = 0
        while tb is not None:
            selected_frames.append(tb)
            frame_count += 1
            tb = tb.tb_next
        truncated |= frame_count > len(selected_frames)
        for tb in selected_frames:
            if budget <= 128:
                truncated = True
                break
            # 仅保留靠近抛错点的有界帧引用；不读取可能包含凭据的源代码行。
            frame = traceback.FrameSummary(tb.tb_frame.f_code.co_filename, tb.tb_lineno,
                                           tb.tb_frame.f_code.co_name, lookup_line=False, line="")
            rendered = redact_failure_text("".join(traceback.StackSummary.from_list([frame]).format()), secrets=secrets)
            selected = _clip(rendered, max(0, budget - len(_MARKER.encode("utf-8"))))
            parts.append(selected)
            budget -= len(selected.encode("utf-8"))
            frames -= 1
            if selected != rendered:
                truncated = True
                break
        children: list[tuple[BaseException, int, str]] = []
        cause = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if cause is not None:
            children.append((cause, depth + 1, "原因: "))
        if isinstance(current, BaseExceptionGroup):
            children.extend((child, depth + 1, "子错误: ") for child in islice(current.exceptions, MAX_DESCENDANTS))
            truncated |= len(current.exceptions) > MAX_DESCENDANTS
        capacity = MAX_DESCENDANTS + 1 - len(seen) - len(pending)
        if depth >= MAX_DEPTH:
            truncated |= bool(children)
        else:
            truncated |= len(children) > capacity
            pending.extend(reversed(children[:capacity]))
    if pending:
        truncated = True
    if truncated:
        parts.append(_clip(_MARKER, budget))
    return "".join(parts)
