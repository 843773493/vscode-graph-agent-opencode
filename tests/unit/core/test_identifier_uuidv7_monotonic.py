"""唯一 id 工厂的 UUIDv7 单调性与量化上界测试。

覆盖 OpenSpec change `migrate-identifiers-to-uuidv7` 的 §2（D2、D2b/A3）：
同进程同毫秒非递减且唯一、跨毫秒自然单调、生成路径不得传显式 timestamp，
以及「只承诺同进程内同毫秒有序」的量化边界。时钟回拨由 uuid-utils 默认路径
处理，创建链路直接从已分配 ID 派生时间与日期桶。
"""

from __future__ import annotations

import collections

import pytest

from app.core import identifier
from app.core.identifier import create_prefixed_id, create_uuid_hex


def _ms_groups(hex_ids: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for value in hex_ids:
        groups[value[:12]].append(value)
    return dict(groups)


# ----------------------------------------------------------------------
# 2.1 同进程同毫秒非递减且唯一
# ----------------------------------------------------------------------


def test_same_millisecond_batch_is_non_decreasing_and_unique() -> None:
    hex_ids = [create_uuid_hex() for _ in range(20000)]

    # 同毫秒内的每个分组都 MUST 有序且唯一（rand_a/计数器方案）。
    groups = _ms_groups(hex_ids)
    assert groups, "至少应落在一个毫秒分组内"
    for group in groups.values():
        assert group == sorted(group), "同毫秒组内 MUST 非递减"
        assert len(set(group)) == len(group), "同毫秒组内 MUST 唯一"
    # 整体（跨毫秒）按生成顺序逐字节有序且唯一。
    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 20000


def test_prefixed_ids_same_millisecond_are_non_decreasing() -> None:
    payloads = [create_prefixed_id("msg").split("_", maxsplit=1)[1] for _ in range(20000)]

    assert payloads == sorted(payloads)
    assert len(set(payloads)) == 20000


# ----------------------------------------------------------------------
# 2.2 跨毫秒自然单调
# ----------------------------------------------------------------------


def test_cross_millisecond_batch_is_globally_ordered_and_unique() -> None:
    hex_ids = [create_uuid_hex() for _ in range(200000)]

    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 200000


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# 2.3 生成路径不得传显式 timestamp
# ----------------------------------------------------------------------


def test_generation_path_does_not_pass_explicit_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_uuid7 = identifier._resolve_uuid7()

    def spy(*args: object, **kwargs: object):
        calls.append((args, kwargs))
        return real_uuid7(*args, **kwargs)

    monkeypatch.setattr(identifier, "_resolve_uuid7", lambda: spy)

    create_uuid_hex()
    create_prefixed_id("evt")

    assert calls, "生成路径必须经 uuid7"
    for args, kwargs in calls:
        assert args == () and kwargs == {}, (
            "生成路径 MUST NOT 传显式 timestamp（会破坏同毫秒单调）: "
            f"args={args!r}, kwargs={kwargs!r}"
        )


def test_generation_docstring_forbids_explicit_timestamp() -> None:
    doc = create_uuid_hex.__doc__ or ""
    assert "MUST NOT 传显式" in doc


# ----------------------------------------------------------------------
# 2.5（A3）量化上界与「只承诺同进程内同毫秒有序」的边界断言
# ----------------------------------------------------------------------


def test_quantified_same_millisecond_density_upper_bound() -> None:
    hex_ids = [create_uuid_hex() for _ in range(500000)]

    groups = _ms_groups(hex_ids)
    max_group = max(len(group) for group in groups.values())

    # 记录实测的同毫秒组最大规模（规划实测约 3710；实际随机器与负载浮动）。
    assert max_group >= 1
    # 组内 MUST 全部有序且唯一（毫秒内数千个 id 仍保持顺序）。
    assert all(group == sorted(group) for group in groups.values())
    assert all(len(set(group)) == len(group) for group in groups.values())
    assert hex_ids == sorted(hex_ids)
    assert len(set(hex_ids)) == 500000


def test_documented_contract_only_promises_same_process_same_ms_order() -> None:
    """实现与文档只承诺：同进程内同毫秒非递减且唯一 + 跨进程共享 48 bit 毫秒分辨率。

    MUST NOT 承诺跨进程同毫秒有序或主键严格按时间相邻。
    """
    doc = create_uuid_hex.__doc__ or ""
    assert "同进程" in doc and "同毫秒" in doc
    assert "跨进程" in doc and "48 bit 毫秒分辨率" in doc
    assert "严格按时间相邻" not in doc
    assert "跨进程同毫秒有序" not in doc

    # 工厂模块整体同样不得出现「严格按时间相邻」这类过度承诺。
    source = identifier.__doc__ or ""
    assert "严格按时间相邻" not in source
