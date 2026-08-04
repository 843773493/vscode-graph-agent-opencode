from dataclasses import dataclass
from typing import Literal

from app.gateway.theme.builtins import ThemeBase


@dataclass(frozen=True, slots=True)
class ThemeDefinition:
    id: str
    label: str
    extends: ThemeBase
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background: dict[str, object] | None
    source: Literal["builtin", "gateway_config"]
