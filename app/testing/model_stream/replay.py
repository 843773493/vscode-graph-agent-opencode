from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx

from .assets import Interaction, ModelStreamCassette, load_cassette
from .context import current_model_call_identity
from .errors import ModelStreamMatchError
from .matcher import StrictRequestMatcher, request_summary

ReplayPolicyName = Literal["request_reusable", "call_identity"]


@dataclass(slots=True)
class ReplayCoordinator:
    cassette: ModelStreamCassette
    scenario_id: str
    policy: ReplayPolicyName
    matcher: StrictRequestMatcher = field(default_factory=StrictRequestMatcher)
    _hit_counts: dict[int, int] = field(default_factory=dict)

    def select(self, request: httpx.Request) -> Interaction:
        if self.policy == "request_reusable":
            interaction = self.matcher.require_one(self.cassette, request, scenario_id=self.scenario_id)
        else:
            identity = current_model_call_identity()
            cassette = load_cassette(self.cassette.path) if self.cassette.path is not None else self.cassette
            matches = tuple(entry for entry in self.matcher.matching_interactions(cassette, request)
                            if entry.identity == identity)
            if len(matches) != 1:
                raise ModelStreamMatchError(
                    "模型 stream call_identity 未唯一匹配: "
                    f"scenario={self.scenario_id!r}, identity={identity.to_dict()!r}, "
                    f"asset={self.cassette.path or '<memory>'!s}, matched={[entry.index for entry in matches]!r}, "
                    f"actual={request_summary(request)!r}"
                )
            interaction = matches[0]
        self._hit_counts[interaction.index] = self._hit_counts.get(interaction.index, 0) + 1
        return interaction

    def hit_count(self, interaction_index: int) -> int:
        return self._hit_counts.get(interaction_index, 0)

    def hit_counts(self) -> dict[int, int]:
        return dict(self._hit_counts)
