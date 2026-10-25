"""rollout 历史游标的查询绑定、验证与编解码。"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import cast

from app.core.history_loading import HistoryLoadingConfig
from app.schemas.internal_v2.turn import (
    TurnCursorDTO,
    TurnCursorQueryMode,
    TurnHistoryLoadRequest,
    TurnInclude,
)
from app.services.infrastructure.rollout_history.snapshot import IndexedHistory
from app.services.infrastructure.turn_history.models import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
)


@dataclass(frozen=True, slots=True)
class HistoryCursorBinding:
    query_mode: TurnCursorQueryMode
    effective_include: tuple[TurnInclude, ...]
    history_view_revision: int
    turn_read_upper_bound: int | None
    logical_item_read_upper_bound: int | None
    anchor_turn_id: str | None = None
    window_before_turns: int | None = None
    window_after_turns: int | None = None
    execution_id: str | None = None


class HistoryCursorMixin:
    """HistoryPageReadMixin 使用的游标绑定、校验与编解码。"""

    @classmethod
    def _cursor_binding(
        cls,
        request: TurnHistoryLoadRequest,
        configured: HistoryLoadingConfig,
        decoded: TurnCursorDTO | None,
        *,
        history_view_revision: int,
        turn_read_upper_bound: int | None,
        logical_item_read_upper_bound: int | None,
    ) -> HistoryCursorBinding:
        if decoded is None:
            if request.execution_id is not None:
                return HistoryCursorBinding(
                    query_mode="execution",
                    effective_include=cls._canonical_include(
                        request.include or ("internal", "text", "final_response")
                    ),
                    history_view_revision=history_view_revision,
                    turn_read_upper_bound=turn_read_upper_bound,
                    logical_item_read_upper_bound=logical_item_read_upper_bound,
                    execution_id=request.execution_id,
                )
            if request.turn_ids is not None:
                include = request.include
                if include is None:
                    include = configured.initial_include
                return HistoryCursorBinding(
                    query_mode="linear",
                    effective_include=cls._canonical_include(include),
                    history_view_revision=history_view_revision,
                    turn_read_upper_bound=turn_read_upper_bound,
                    logical_item_read_upper_bound=logical_item_read_upper_bound,
                )
            query_mode: TurnCursorQueryMode = (
                "window"
                if request.direction == "around"
                else "tail"
                if request.direction == "tail"
                else "linear"
            )
            include = request.include
            if include is None:
                include = (
                    configured.initial_include
                    if query_mode == "tail"
                    else configured.anchor_include
                )
            effective_include = cls._canonical_include(include)
            if query_mode != "window":
                return HistoryCursorBinding(
                    query_mode=query_mode,
                    effective_include=effective_include,
                    history_view_revision=history_view_revision,
                    turn_read_upper_bound=turn_read_upper_bound,
                    logical_item_read_upper_bound=logical_item_read_upper_bound,
                )
            if request.anchor_turn_id is None:
                raise InvalidTurnCursorError("around 历史游标缺少 anchor_turn_id")
            return HistoryCursorBinding(
                query_mode="window",
                effective_include=effective_include,
                history_view_revision=history_view_revision,
                turn_read_upper_bound=turn_read_upper_bound,
                logical_item_read_upper_bound=logical_item_read_upper_bound,
                anchor_turn_id=request.anchor_turn_id,
                window_before_turns=cls._window_limit(
                    request.before_turns,
                    configured.anchor_before_turns,
                ),
                window_after_turns=cls._window_limit(
                    request.after_turns,
                    configured.anchor_after_turns,
                ),
            )

        normalized_direction = (
            "before" if request.direction == "older" else request.direction
        )
        if request.execution_id != decoded.execution_id:
            raise InvalidTurnCursorError("历史游标 execution_id 与请求不一致")
        if (
            request.include is not None
            and cls._canonical_include(request.include) != decoded.effective_include
        ):
            raise InvalidTurnCursorError("历史游标 effective_include 与请求不一致")
        if request.direction == "around":
            if decoded.query_mode != "window":
                raise InvalidTurnCursorError("around 请求与历史游标 query_mode 不一致")
            if (
                request.anchor_turn_id is not None
                and request.anchor_turn_id != decoded.anchor_turn_id
            ):
                raise InvalidTurnCursorError("历史游标 anchor_turn_id 与请求不一致")
            if (
                request.before_turns is not None
                and cls._window_limit(
                    request.before_turns,
                    configured.anchor_before_turns,
                )
                != decoded.window_before_turns
            ):
                raise InvalidTurnCursorError(
                    "历史游标 window_before_turns 与请求不一致"
                )
            if (
                request.after_turns is not None
                and cls._window_limit(
                    request.after_turns,
                    configured.anchor_after_turns,
                )
                != decoded.window_after_turns
            ):
                raise InvalidTurnCursorError("历史游标 window_after_turns 与请求不一致")
        elif normalized_direction in {"before", "after"}:
            if decoded.direction != normalized_direction:
                raise InvalidTurnCursorError("历史游标 direction 与请求不一致")
        elif request.direction not in {"head", "tail"}:
            raise InvalidTurnCursorError("历史游标只能用于同向 before/after 续读")
        else:
            raise InvalidTurnCursorError("分页游标不能切换到 head/tail 查询")

        if decoded.query_mode == "window":
            if normalized_direction not in {"before", "after", "around"}:
                raise InvalidTurnCursorError("window 历史游标不能切换 query_mode")
        elif decoded.query_mode == "tail" and decoded.direction != "before":
            raise InvalidTurnCursorError("tail 历史游标只能向 before 方向续读")
        elif request.direction == "around":
            raise InvalidTurnCursorError("非 window 历史游标不能用于 around")
        return HistoryCursorBinding(
            query_mode=decoded.query_mode,
            effective_include=decoded.effective_include,
            history_view_revision=decoded.history_view_revision,
            turn_read_upper_bound=decoded.turn_read_upper_bound,
            logical_item_read_upper_bound=decoded.logical_item_read_upper_bound,
            anchor_turn_id=decoded.anchor_turn_id,
            window_before_turns=decoded.window_before_turns,
            window_after_turns=decoded.window_after_turns,
            execution_id=decoded.execution_id,
        )

    @staticmethod
    def _canonical_include(include: object) -> tuple[TurnInclude, ...]:
        if not isinstance(include, list | tuple):
            raise TypeError("历史 effective_include 必须是序列")
        if any(not isinstance(item, str) for item in include):
            raise TypeError("历史 effective_include 必须只包含字符串")
        return tuple(sorted({cast(TurnInclude, item) for item in include}))

    @staticmethod
    def _window_limit(requested: int | None, configured: int) -> int:
        return min(configured if requested is None else requested, 64)

    def _indexed_before_cursor(
        self,
        session_id: str,
        indexed: IndexedHistory,
        *,
        binding: HistoryCursorBinding,
        stage: int,
        turn_ordinal: int | None,
        logical_item_ordinal: int | None,
    ) -> str | None:
        if indexed.view_id is None or (
            turn_ordinal is None and logical_item_ordinal is None
        ):
            return None
        return self._encode_cursor(
            session_id=session_id,
            thread_id=indexed.snapshot.thread_id,
            checkpoint_ns=indexed.snapshot.checkpoint_ns,
            rollout_id=indexed.rollout_id,
            projection_epoch=indexed.projection_epoch,
            view_id=indexed.view_id,
            history_view_revision=binding.history_view_revision,
            turn_read_upper_bound=binding.turn_read_upper_bound,
            logical_item_read_upper_bound=binding.logical_item_read_upper_bound,
            direction="before",
            query_mode=binding.query_mode,
            effective_include=binding.effective_include,
            anchor_turn_id=binding.anchor_turn_id,
            window_before_turns=binding.window_before_turns,
            window_after_turns=binding.window_after_turns,
            stage=stage,
            turn_ordinal=turn_ordinal,
            logical_item_ordinal=logical_item_ordinal,
            execution_id=binding.execution_id,
        )

    def _indexed_after_cursor(
        self,
        session_id: str,
        indexed: IndexedHistory,
        *,
        binding: HistoryCursorBinding,
        stage: int,
        turn_ordinal: int | None,
        logical_item_ordinal: int | None,
    ) -> str | None:
        if indexed.view_id is None or (
            turn_ordinal is None and logical_item_ordinal is None
        ):
            return None
        return self._encode_cursor(
            session_id=session_id,
            thread_id=indexed.snapshot.thread_id,
            checkpoint_ns=indexed.snapshot.checkpoint_ns,
            rollout_id=indexed.rollout_id,
            projection_epoch=indexed.projection_epoch,
            view_id=indexed.view_id,
            history_view_revision=binding.history_view_revision,
            turn_read_upper_bound=binding.turn_read_upper_bound,
            logical_item_read_upper_bound=binding.logical_item_read_upper_bound,
            direction="after",
            query_mode=binding.query_mode,
            effective_include=binding.effective_include,
            anchor_turn_id=binding.anchor_turn_id,
            window_before_turns=binding.window_before_turns,
            window_after_turns=binding.window_after_turns,
            stage=stage,
            turn_ordinal=turn_ordinal,
            logical_item_ordinal=logical_item_ordinal,
            execution_id=binding.execution_id,
        )

    def _decode_cursor(
        self,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        rollout_id: str,
        projection_epoch: int,
        view_id: str | None,
        history_view_revision: int | None,
        turn_read_upper_bound: int | None,
        logical_item_read_upper_bound: int | None,
        expected_direction: str | None,
        cursor: str | None,
    ) -> TurnCursorDTO | None:
        if cursor is None:
            return None
        cursor_payload = self._decode_cursor_payload(cursor)
        if (
            "item_sequence" in cursor_payload
            or "item_read_upper_bound" in cursor_payload
        ):
            raise InvalidTurnCursorError("v2 历史游标不得使用物理 item sequence 坐标")
        if (
            cursor_payload.get("session_id") != session_id
            or cursor_payload.get("thread_id") != thread_id
            or cursor_payload.get("checkpoint_ns") != checkpoint_ns
            or cursor_payload.get("rollout_id") != rollout_id
        ):
            raise InvalidTurnCursorError("历史游标不属于当前 rollout")
        raw_direction = cursor_payload.get("direction")
        if not isinstance(raw_direction, str) or raw_direction not in {
            "before",
            "after",
        }:
            raise InvalidTurnCursorError("历史游标 direction 非法")
        if expected_direction is not None and raw_direction != expected_direction:
            raise InvalidTurnCursorError("历史游标 direction 与请求不一致")
        raw_epoch = cursor_payload.get("projection_epoch")
        if (
            isinstance(raw_epoch, bool)
            or not isinstance(raw_epoch, int)
            or raw_epoch < 1
        ):
            raise InvalidTurnCursorError("历史游标 projection_epoch 非法")
        if raw_epoch != projection_epoch:
            raise StaleTurnCursorError(
                session_id=session_id,
                cursor_epoch=raw_epoch,
                current_epoch=projection_epoch,
            )
        raw_history_revision = cursor_payload.get("history_view_revision")
        if (
            isinstance(raw_history_revision, bool)
            or not isinstance(raw_history_revision, int)
            or raw_history_revision < 0
        ):
            raise InvalidTurnCursorError("历史游标 history_view_revision 非法")
        if history_view_revision is None:
            raise InvalidTurnCursorError("当前 snapshot 缺少 history view revision")
        if raw_history_revision != history_view_revision:
            raise StaleTurnCursorError(
                session_id=session_id,
                cursor_epoch=raw_epoch,
                current_epoch=projection_epoch,
                cursor_history_view_revision=raw_history_revision,
                current_history_view_revision=history_view_revision,
            )
        raw_view_id = cursor_payload.get("view_id")
        if view_id is None or raw_view_id != view_id:
            raise InvalidTurnCursorError("历史游标不属于当前 context view")
        if (
            cursor_payload.get("turn_read_upper_bound") != turn_read_upper_bound
            or cursor_payload.get("logical_item_read_upper_bound")
            != logical_item_read_upper_bound
        ):
            raise RuntimeError(
                "相同 history_view_revision 下 rollout history upper bound 漂移"
            )
        try:
            decoded = TurnCursorDTO.model_validate(cursor_payload)
        except ValueError as error:
            raise InvalidTurnCursorError("历史游标字段非法") from error
        if (
            self._encode_cursor(
                session_id=decoded.session_id,
                thread_id=decoded.thread_id,
                checkpoint_ns=decoded.checkpoint_ns,
                rollout_id=decoded.rollout_id,
                projection_epoch=decoded.projection_epoch,
                view_id=decoded.view_id,
                history_view_revision=decoded.history_view_revision,
                turn_read_upper_bound=decoded.turn_read_upper_bound,
                logical_item_read_upper_bound=decoded.logical_item_read_upper_bound,
                direction=decoded.direction,
                query_mode=decoded.query_mode,
                effective_include=decoded.effective_include,
                anchor_turn_id=decoded.anchor_turn_id,
                window_before_turns=decoded.window_before_turns,
                window_after_turns=decoded.window_after_turns,
                stage=decoded.stage,
                turn_ordinal=decoded.turn_ordinal,
                logical_item_ordinal=decoded.logical_item_ordinal,
                execution_id=decoded.execution_id,
            )
            != cursor
        ):
            raise InvalidTurnCursorError("历史游标 JSON 不是规范编码")
        return decoded

    def _encode_cursor(
        self,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        rollout_id: str,
        projection_epoch: int,
        view_id: str,
        history_view_revision: int,
        turn_read_upper_bound: int | None,
        logical_item_read_upper_bound: int | None,
        direction: str,
        query_mode: TurnCursorQueryMode,
        effective_include: tuple[TurnInclude, ...],
        anchor_turn_id: str | None,
        window_before_turns: int | None,
        window_after_turns: int | None,
        stage: int,
        turn_ordinal: int | None,
        logical_item_ordinal: int | None,
        execution_id: str | None = None,
    ) -> str:
        cursor = TurnCursorDTO(
            session_id=session_id,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            rollout_id=rollout_id,
            projection_epoch=projection_epoch,
            view_id=view_id,
            history_view_revision=history_view_revision,
            turn_read_upper_bound=turn_read_upper_bound,
            logical_item_read_upper_bound=logical_item_read_upper_bound,
            direction=direction,
            query_mode=query_mode,
            effective_include=effective_include,
            anchor_turn_id=anchor_turn_id,
            window_before_turns=window_before_turns,
            window_after_turns=window_after_turns,
            stage=stage,
            turn_ordinal=turn_ordinal,
            logical_item_ordinal=logical_item_ordinal,
            execution_id=execution_id,
        )
        payload = json.dumps(
            cursor.model_dump(mode="json"),
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor_payload(cursor: str) -> dict[str, object]:
        if not isinstance(cursor, str) or not cursor:
            raise InvalidTurnCursorError("历史游标不是合法的不透明值")
        try:
            payload = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4),
                altchars=b"-_",
                validate=True,
            )
            value = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidTurnCursorError("历史游标不是合法的不透明值") from error
        if not isinstance(value, dict):
            raise InvalidTurnCursorError("历史游标不是 JSON object")
        version = value.get("version")
        if isinstance(version, bool) or version != 2:
            raise InvalidTurnCursorError("历史游标版本不受支持")
        return value
