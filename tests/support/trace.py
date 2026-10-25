from __future__ import annotations


def get_trace_payload(trace_event: dict) -> dict:
    """从 TraceEventDTO 的 raw 字段中提取 payload。"""

    raw = trace_event.get("raw") or {}
    payload = raw.get("payload") or {}
    return payload if isinstance(payload, dict) else {}
