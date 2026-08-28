from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from app.protocol.codecs.json import struct_from_mapping, struct_to_mapping
from app.protocol.generated.boxteam.browser.v1 import browser_pb2


def _status(value: object) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Browser 状态必须是非空字符串: {value!r}")
    enum_value = getattr(browser_pb2, f"BROWSER_STATUS_{value.upper()}", None)
    if not isinstance(enum_value, int):
        raise TypeError(f"Browser 状态不在协议范围内: {value}")
    return enum_value


def browser_page_to_proto(value: Mapping[str, object]) -> browser_pb2.BrowserPage:
    required = ("browser_id", "page_id", "session_id", "status")
    for field_name in required:
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ValueError(f"Browser 页面缺少字段: {field_name}")
    page = browser_pb2.BrowserPage(
        browser_id=cast(str, value["browser_id"]),
        page_id=cast(str, value["page_id"]),
        session_id=cast(str, value["session_id"]),
        status=_status(value["status"]),
    )
    for field_name in ("title", "url"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, str):
                raise ValueError(f"Browser 页面字段类型错误: {field_name}")
            setattr(page, field_name, field_value)
    viewport = value.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, Mapping):
            raise ValueError("Browser 页面 viewport 必须是对象")
        width = viewport.get("width")
        height = viewport.get("height")
        if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
            raise ValueError("Browser 页面 viewport width/height 必须是正整数")
        page.viewport.width = width
        page.viewport.height = height
    sequence = value.get("sequence")
    if sequence is not None:
        if not isinstance(sequence, int) or sequence < 0:
            raise ValueError("Browser 页面 sequence 必须是非负整数")
        page.sequence = sequence
    page.metadata.CopyFrom(struct_from_mapping({"raw": dict(value)}))
    return page


def browser_page_to_json(value: browser_pb2.BrowserPage) -> dict[str, object]:
    if not value.HasField("metadata"):
        raise ValueError(f"Browser Protobuf 页面缺少 raw metadata: browser_id={value.browser_id}")
    raw = struct_to_mapping(value.metadata).get("raw")
    if not isinstance(raw, dict):
        raise TypeError(f"Browser Protobuf 页面 raw metadata 格式错误: browser_id={value.browser_id}")
    return cast(dict[str, object], raw)
