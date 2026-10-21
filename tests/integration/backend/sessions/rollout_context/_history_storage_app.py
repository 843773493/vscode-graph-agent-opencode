"""历史存储测量装配：只观察既有序列化、写入和恢复，不构造业务状态。"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from time import perf_counter_ns

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.services.infrastructure import rollout_checkpoint_runtime as runtime
from tests.support.e2e_control.backend_app import app

_metrics: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "bytes": 0, "ns": 0})
_lock = Lock()


def _record(name: str, started: int, length: int = 0) -> None:
    elapsed = perf_counter_ns() - started
    with _lock:
        value = _metrics[name]
        value["calls"] += 1
        value["bytes"] += length
        value["ns"] += elapsed


class MeasuredSerializer(JsonPlusSerializer):
    def dumps_typed(self, obj):
        started = perf_counter_ns()
        result = super().dumps_typed(obj)
        _record(f"serialize:{type(obj).__name__}", started, len(result[1]))
        return result

    def loads_typed(self, data):
        started = perf_counter_ns()
        result = super().loads_typed(data)
        _record("deserialize", started, len(data[1]))
        return result


class MeasuredWriter(runtime.RolloutAppendWriter):
    def append_checkpoint(self, **kwargs):
        started = perf_counter_ns()
        result = super().append_checkpoint(**kwargs)
        _record("append_checkpoint", started)
        return result


class MeasuredSaver(runtime.RolloutCheckpointSaver):
    def get_tuple(self, config):
        started = perf_counter_ns()
        result = super().get_tuple(config)
        _record("get_tuple", started)
        return result


# 只替换显式测试进程的构造依赖，所有实现仍委托原有唯一 owner。
runtime.JsonPlusSerializer = MeasuredSerializer
runtime.RolloutAppendWriter = MeasuredWriter
runtime.RolloutCheckpointSaver = MeasuredSaver


@app.get("/__test__/history-storage-metrics")
async def history_storage_metrics() -> dict[str, dict[str, int]]:
    with _lock:
        return {key: dict(value) for key, value in _metrics.items()}
