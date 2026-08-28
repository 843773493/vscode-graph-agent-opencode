from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx

from .assets import Interaction, ModelStreamCassette
from .context import current_replay_session_id
from .errors import ModelStreamMatchError
from .matcher import StrictRequestMatcher, request_summary

ReplayPolicyName = Literal["request_reusable", "session_sequence"]


@dataclass(slots=True)
class ReplayCoordinator:
    cassette: ModelStreamCassette
    scenario_id: str
    policy: ReplayPolicyName
    matcher: StrictRequestMatcher = field(default_factory=StrictRequestMatcher)
    _hit_counts: dict[int, int] = field(default_factory=dict)
    _session_steps: dict[str, dict[str, int]] = field(default_factory=dict)

    def select(self, request: httpx.Request) -> Interaction:
        if self.policy == "request_reusable":
            interaction = self.matcher.require_one(
                self.cassette,
                request,
                scenario_id=self.scenario_id,
            )
            self._hit_counts[interaction.index] = self._hit_counts.get(interaction.index, 0) + 1
            return interaction

        session_id = current_replay_session_id()
        if session_id is None:
            raise ModelStreamMatchError(
                "模型 stream session_sequence 需要 transport request context 中的 "
                "replay_session_id"
            )
        candidates = self.matcher.matching_interactions(self.cassette, request)
        if not candidates:
            raise ModelStreamMatchError(
                "模型 stream session_sequence 没有匹配 interaction: "
                f"scenario={self.scenario_id!r}, asset={self.cassette.path or '<memory>'!s}, "
                f"actual={request_summary(request)!r}"
            )
        session_steps = self._session_steps.setdefault(session_id, {})
        ready = tuple(
            interaction
            for interaction in candidates
            if interaction.replay is not None
            and interaction.replay.step
            == session_steps.get(interaction.replay.sequence_id, 0)
        )
        if len(ready) != 1:
            expected = {
                interaction.index: (
                    interaction.replay.sequence_id,
                    interaction.replay.step,
                )
                for interaction in candidates
                if interaction.replay is not None
            }
            raise ModelStreamMatchError(
                "模型 stream session_sequence 无法唯一选择 interaction: "
                f"scenario={self.scenario_id!r}, session={session_id!r}, "
                f"expected={expected!r}"
            )
        interaction = ready[0]
        replay = interaction.replay
        if replay is None:
            raise RuntimeError("session_sequence 选中的 interaction 缺少 replay 元数据")
        session_steps[replay.sequence_id] = replay.step + 1
        self._hit_counts[interaction.index] = self._hit_counts.get(interaction.index, 0) + 1
        return interaction

    def hit_count(self, interaction_index: int) -> int:
        return self._hit_counts.get(interaction_index, 0)

    def hit_counts(self) -> dict[int, int]:
        return dict(self._hit_counts)
