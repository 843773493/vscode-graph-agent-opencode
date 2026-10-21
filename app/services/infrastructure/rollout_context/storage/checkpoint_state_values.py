"""checkpoint 的不可变状态版本；引用和候选值在同一 SQLite 事务内核对。"""

from __future__ import annotations

import sqlite3
import struct
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.core.hashing import sha256_hex
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


def read_state_value(
    connection: sqlite3.Connection, value_id: object, *, channel: str,
    version: str, checkpoint_ns: str, session_id: str, thread_id: str,
) -> tuple[str, bytes]:
    if type(value_id) is not int or value_id <= 0:
        raise RuntimeError(f"checkpoint channel 版本引用非法: {channel}/{version}/{value_id}")
    row = connection.execute(
        "SELECT session_id,thread_id,checkpoint_ns,channel_name,channel_version,"
        "serializer_name,value_blob,value_length,value_hash FROM checkpoint_state_values WHERE value_id=?",
        (value_id,),
    ).fetchone()
    if row is None or row[:5] != (session_id, thread_id, checkpoint_ns, channel, version):
        raise RuntimeError(f"checkpoint channel 版本引用不匹配: {channel}/{version}/{value_id}")
    require_historical_scope(connection, row[0], row[1])
    serializer, blob, length, digest = row[5:]
    if (not isinstance(serializer, str) or not serializer or not isinstance(blob, bytes)
            or type(length) is not int or length != len(blob) or sha256_hex(blob) != digest):
        raise RuntimeError(f"checkpoint channel BLOB 校验失败: {channel}/{version}/{value_id}")
    return serializer, blob


def _builtin_equal(left: object, right: object) -> bool | None:
    """只对精确内置类型作结构比较；不信任扩展类型可能自定义的 __eq__。"""
    if type(left) is not type(right):
        return None
    if type(left) in (str, bytes, int, bool, type(None)):
        return left == right
    if type(left) is float:
        return struct.pack("!d", left) == struct.pack("!d", right)
    if isinstance(left, BaseModel):
        model = type(left)
        decorators = model.__pydantic_decorators__
        if (model.model_dump is not BaseModel.model_dump or decorators.field_serializers
                or decorators.model_serializers or decorators.computed_fields):
            return None
        # 官方 JsonPlus 以类身份和 model_dump() 编码普通 Pydantic 模型。
        # 完整字段与 extra 相等即可沿用；不调用用户 __eq__，也不重新生成大 dump。
        fields = _builtin_equal(left.__dict__, right.__dict__)
        extras = _builtin_equal(left.__pydantic_extra__, right.__pydantic_extra__)
        return True if fields is True and extras is True else None
    if type(left) is dict:
        if any(type(key) is not str for key in left) or any(type(key) is not str for key in right):
            return None
        if tuple(left) != tuple(right):
            return False
        pairs = ((left[key], right[key]) for key in left)
    elif type(left) in (list, tuple):
        if len(left) != len(right):
            return False
        pairs = zip(left, right, strict=True)
    else:
        return None
    unknown = False
    for first, second in pairs:
        equal = _builtin_equal(first, second)
        if equal is False:
            return False
        unknown |= equal is None
    return None if unknown else True


def persist_state_value(
    storage: RolloutStorage, connection: sqlite3.Connection, *, session_id: str,
    thread_id: str, checkpoint_ns: str, channel: str, version: str,
    candidate: object, parent_checkpoint_id: str | None, is_new_version: bool,
) -> tuple[int, str, str]:
    """沿用明确父引用时不重复 dumps；新候选始终核对冻结正文，拒绝版本碰撞。"""
    inherited = None
    if not is_new_version and parent_checkpoint_id is not None:
        inherited = connection.execute(
            "SELECT channel.value_id,channel.value_session_id,channel.value_thread_id FROM checkpoint_channels channel JOIN checkpoints checkpoint "
            "ON checkpoint.checkpoint_id=channel.checkpoint_id WHERE channel.checkpoint_id=? "
            "AND checkpoint.checkpoint_ns=? AND channel.channel_name=? "
            "AND channel.channel_version=? AND channel.value_state='present'",
            (parent_checkpoint_id, checkpoint_ns, channel, version),
        ).fetchone()
    existing = inherited or connection.execute(
        "SELECT value_id,session_id,thread_id FROM checkpoint_state_values WHERE session_id=? AND thread_id=? "
        "AND checkpoint_ns=? AND channel_name=? AND channel_version=?",
        (session_id, thread_id, checkpoint_ns, channel, version),
    ).fetchone()
    frozen = None
    if existing is not None:
        frozen = read_state_value(connection, existing[0], channel=channel, version=version, checkpoint_ns=checkpoint_ns,
                                  session_id=existing[1], thread_id=existing[2])
        if inherited is not None:
            equal = _builtin_equal(candidate, storage.decode_value(frozen))
            if equal is True:
                return existing
            if equal is False:
                raise ValueError(f"checkpoint channel 同版本异内容: {session_id}/{thread_id}/{checkpoint_ns}/{channel}/{version}")
    # TODO：带自定义 serializer 的扩展类型需另核合同；目前保留字节校验，不能信任其 __eq__。
    serializer, blob, length, digest = storage._encode(candidate)
    if existing is not None:
        if frozen != (serializer, blob):
            raise ValueError(f"checkpoint channel 同版本异内容: {session_id}/{thread_id}/{checkpoint_ns}/{channel}/{version}")
        return existing
    result = connection.execute(
        "INSERT INTO checkpoint_state_values(session_id,thread_id,checkpoint_ns,channel_name,channel_version,"
        "serializer_name,value_blob,value_length,value_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, thread_id, checkpoint_ns, channel, version, serializer, blob, length, digest),
    )
    return result.lastrowid, session_id, thread_id
