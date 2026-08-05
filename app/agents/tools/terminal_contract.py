from __future__ import annotations

import re
from typing import Any

from app.core.identifier import create_uuid_hex

DEFAULT_EXEC_YIELD_TIME_MS = 10_000
DEFAULT_WRITE_STDIN_YIELD_TIME_MS = 250
MIN_YIELD_TIME_MS = 250
MAX_YIELD_TIME_MS = 30_000
MIN_EMPTY_POLL_YIELD_TIME_MS = 5_000
MAX_EMPTY_POLL_YIELD_TIME_MS = 300_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
APPROX_BYTES_PER_TOKEN = 4


def extract_command_output(
    *,
    buffer: str,
    start_marker: str,
    done_marker: str,
) -> tuple[bool, str, int | None]:
    normalized_output = buffer.replace("\r\n", "\n").replace("\r", "\n")
    done_matches = list(
        re.finditer(
            rf"{re.escape(done_marker)}:(\d+)",
            normalized_output,
        )
    )
    if not done_matches:
        start_matches = list(
            re.finditer(
                re.escape(start_marker),
                normalized_output,
            )
        )
        if start_matches:
            return False, normalized_output[start_matches[-1].end() :].strip(), None
        return False, buffer, None

    done_match = done_matches[-1]
    start_matches = list(
        re.finditer(
            re.escape(start_marker),
            normalized_output[: done_match.start()],
        )
    )
    if start_matches:
        output = normalized_output[start_matches[-1].end() : done_match.start()]
    else:
        output = normalized_output[: done_match.start()]
    return True, output.strip(), int(done_match.group(1))


def clean_terminal_delta(output: str, terminal: dict[str, Any]) -> str:
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    for marker_field in ("last_command_start_marker", "last_command_done_marker"):
        marker = terminal.get(marker_field)
        if not isinstance(marker, str) or not marker:
            continue
        normalized = re.sub(
            rf"^{re.escape(marker)}(?::\d+)?\s*$",
            "",
            normalized,
            flags=re.MULTILINE,
        )
    return normalized.strip()


def truncate_output(
    output: str,
    max_output_tokens: int | None,
) -> tuple[str, int]:
    encoded = output.encode("utf-8")
    original_token_count = (
        len(encoded) + APPROX_BYTES_PER_TOKEN - 1
    ) // APPROX_BYTES_PER_TOKEN
    resolved_limit = max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
    if original_token_count <= resolved_limit:
        return output, original_token_count

    marker = (
        f"\n... 已截断 {original_token_count - resolved_limit} 个输出 token ...\n"
    )
    max_bytes = resolved_limit * APPROX_BYTES_PER_TOKEN
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore"), original_token_count
    content_bytes = max_bytes - len(marker_bytes)
    head_bytes = content_bytes // 2
    tail_bytes = content_bytes - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore") if head_bytes else ""
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return f"{head}{marker}{tail}", original_token_count


def effective_yield_time_ms(yield_time_ms: int, *, empty_poll: bool = False) -> int:
    if yield_time_ms < 0:
        raise ValueError("yield_time_ms 不能为负数")
    if empty_poll:
        return min(
            max(yield_time_ms, MIN_EMPTY_POLL_YIELD_TIME_MS),
            MAX_EMPTY_POLL_YIELD_TIME_MS,
        )
    return min(max(yield_time_ms, MIN_YIELD_TIME_MS), MAX_YIELD_TIME_MS)


def validate_max_output_tokens(max_output_tokens: int | None) -> None:
    if max_output_tokens is not None and max_output_tokens <= 0:
        raise ValueError("max_output_tokens 必须大于 0")


def tool_output(
    *,
    terminal_id: str,
    wall_time_seconds: float,
    output: str,
    max_output_tokens: int | None,
    exit_code: int | None = None,
    running: bool,
) -> dict[str, Any]:
    truncated_output, original_token_count = truncate_output(
        output,
        max_output_tokens,
    )
    result: dict[str, Any] = {
        "chunk_id": create_uuid_hex()[:6],
        "terminal_id": terminal_id,
        "wall_time_seconds": wall_time_seconds,
        "original_token_count": original_token_count,
        "output": truncated_output,
    }
    if running:
        result["session_id"] = terminal_id
    elif exit_code is not None:
        result["exit_code"] = exit_code
    return result


__all__ = [
    "DEFAULT_EXEC_YIELD_TIME_MS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_WRITE_STDIN_YIELD_TIME_MS",
    "clean_terminal_delta",
    "effective_yield_time_ms",
    "extract_command_output",
    "tool_output",
    "truncate_output",
    "validate_max_output_tokens",
]
