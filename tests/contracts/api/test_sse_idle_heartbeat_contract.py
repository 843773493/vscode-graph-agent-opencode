"""冻结「服务端 SSE 心跳间隔必须严格小于浏览器空闲阈值」这条跨层契约。

浏览器各长连 SSE 流只有一份空闲阈值常量
（src/clients/web/src/sse/sseIdleTimeout.ts 的 SSE_IDLE_TIMEOUT_MS）。服务端任何
可配置的心跳间隔一旦接近或超过该阈值，前端就会把健康长连误判为断线。此前只有
「前端阈值 > 15000」这类静态断言，捕获不到有人把 trace 心跳配大。本测试直接读取
两份权威来源并断言 schema 上界，配置被调大即变红。
"""

from __future__ import annotations

import re
from pathlib import Path

import jsonschema
import pytest

from configs.runtime import read_jsonc_object

_IDLE_TIMEOUT_SOURCE = Path("src/clients/web/src/sse/sseIdleTimeout.ts")
_SCHEMA_PATH = Path("configs/workspace_schema.jsonc")
_INLINE_PATH = Path("configs/workspace_inline.jsonc")
_HEARTBEAT_SCHEMA_POINTER = (
    "eventsRuntimeConfig",
    "trace_stream",
    "heartbeat_interval_seconds",
)


def _frontend_idle_timeout_ms() -> int:
    """读取前端唯一空闲阈值，绝不在这里复制第二份数值。"""
    source = _IDLE_TIMEOUT_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r"export const SSE_IDLE_TIMEOUT_MS\s*=\s*([0-9_]+)\s*;",
        source,
    )
    assert match is not None, f"未找到前端空闲阈值常量: {_IDLE_TIMEOUT_SOURCE}"
    return int(match.group(1).replace("_", ""))


def _heartbeat_schema() -> dict[str, object]:
    definitions = read_jsonc_object(_SCHEMA_PATH)["$defs"]
    root, *nested = _HEARTBEAT_SCHEMA_POINTER
    assert isinstance(definitions, dict) and root in definitions, f"schema 缺少 {root}"
    node: object = definitions[root]
    for key in nested:
        assert isinstance(node, dict), f"schema 节点不是对象: {key}"
        properties = node.get("properties")
        assert isinstance(properties, dict) and key in properties, f"schema 缺少 {key}"
        node = properties[key]
    assert isinstance(node, dict)
    return node


def test_schema_bounds_trace_heartbeat_strictly_below_frontend_idle_timeout() -> None:
    # 上界必须存在：只有 exclusiveMinimum 无法阻止配置被调到危险值。
    maximum = _heartbeat_schema().get("maximum")
    assert isinstance(maximum, (int, float)), (
        "runtime.events.trace_stream.heartbeat_interval_seconds 必须有 maximum 上界，"
        "否则前端会在阈值处把健康长连误判断线"
    )
    idle_timeout_seconds = _frontend_idle_timeout_ms() / 1000
    assert maximum < idle_timeout_seconds, (
        f"心跳上界 {maximum}s 必须严格小于前端空闲阈值 {idle_timeout_seconds}s"
    )
    # 3 倍心跳余量与前端注释承诺的容错窗口一致。
    assert maximum * 3 <= idle_timeout_seconds, (
        f"心跳上界 {maximum}s 的 3 倍必须不超过前端阈值 {idle_timeout_seconds}s"
    )


@pytest.mark.parametrize("heartbeat", [60, 45.1, 45, 20])
def test_schema_rejects_unsafe_heartbeat(heartbeat: float) -> None:
    """任何使 3 倍心跳超过前端阈值的心跳都必须在配置层响亮失败。"""
    schema = read_jsonc_object(_SCHEMA_PATH)
    unsafe = read_jsonc_object(_INLINE_PATH)
    unsafe["runtime"]["events"]["trace_stream"]["heartbeat_interval_seconds"] = heartbeat
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(unsafe, schema)


def test_schema_accepts_the_default_and_inline_heartbeat() -> None:
    """内置默认心跳与 inline 默认配置必须仍通过 schema，避免既有配置失效。"""
    schema = read_jsonc_object(_SCHEMA_PATH)
    jsonschema.validate(read_jsonc_object(_INLINE_PATH), schema)
