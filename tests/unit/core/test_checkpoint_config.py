"""build_checkpoint_config 单测。

验证：
- 基础情形：thread_id 必备，checkpoint_ns 默认为空串
- 显式传入 checkpoint_id 时透传
- 不传 checkpoint_id 时**不**写入键（避免 saver 用 None 覆盖默认行为）
- 业务键（session_id / job_id / user_id）**不会**被工具接受 —— 这是工具的契约
"""
from __future__ import annotations

import pytest

from app.core.checkpoint_config import build_checkpoint_config


class TestBuildCheckpointConfig:
    def test_basic(self) -> None:
        cfg = build_checkpoint_config("sess_1")
        assert cfg == {
            "configurable": {
                "thread_id": "sess_1",
                "checkpoint_ns": "",
            }
        }

    def test_with_checkpoint_ns(self) -> None:
        cfg = build_checkpoint_config("sess_1", checkpoint_ns="ns_a")
        assert cfg["configurable"]["checkpoint_ns"] == "ns_a"
        assert cfg["configurable"]["thread_id"] == "sess_1"

    def test_with_checkpoint_id_omits_when_none(self) -> None:
        """None 时**不**写 checkpoint_id 键，避免 saver 把 None 当作有效值。"""
        cfg = build_checkpoint_config("sess_1", checkpoint_id=None)
        assert "checkpoint_id" not in cfg["configurable"]

    def test_with_checkpoint_id_passes_through(self) -> None:
        cfg = build_checkpoint_config("sess_1", checkpoint_id="ckpt_42")
        assert cfg["configurable"]["checkpoint_id"] == "ckpt_42"

    def test_does_not_accept_business_keys(self) -> None:
        """业务键（session_id / job_id）应通过 contextvars / state schema 传递，
        不应混入 configurable —— 这是工具的契约（防御性检查）。"""
        cfg = build_checkpoint_config("sess_1")
        configurable = cfg["configurable"]
        assert "session_id" not in configurable
        assert "job_id" not in configurable
        assert "user_id" not in configurable


class TestBuildCheckpointConfigRuntimeContract:
    """模拟 saver 实际读取 configurable 的方式：使用 .get(key) 的回退语义。"""

    def test_saver_can_resolve_thread_id(self) -> None:
        cfg = build_checkpoint_config("sess_abc")
        # 模拟 saver 内部：configurable.get("thread_id") 一定能拿到
        assert cfg["configurable"].get("thread_id") == "sess_abc"

    def test_saver_falls_back_when_checkpoint_id_absent(self) -> None:
        cfg = build_checkpoint_config("sess_abc")
        # saver 期望 checkpoint_id 缺省时拿到 None
        assert cfg["configurable"].get("checkpoint_id") is None
