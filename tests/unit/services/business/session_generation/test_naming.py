from datetime import UTC, datetime

import pytest

from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationPreviewRequest,
)
from app.services.business.session_generation.naming import render_generation_names


def test_render_date_and_title_without_physical_locator() -> None:
    payload = SessionGenerationPreviewRequest(
        name="每日审查", session_title="依赖更新",
        generated_at=datetime(2026, 7, 23, 10, 11, 12, tzinfo=UTC),
        title_template="{generated_at:HH-mm-ss}_{session.title}",
        path_template=["{generator.name}", "{generated_at:yyyy-MM-dd}", "{session.title}"],
    )
    assert render_generation_names(payload) == (
        "10-11-12_依赖更新", ["每日审查", "2026-07-23", "10-11-12_依赖更新"],
    )


@pytest.mark.parametrize("template", [
    "..", "/etc", "C:\\temp", "a/b", "a\\b", "CON", "NUL.txt", "x\x00y",
    "{unknown}", "{session.title", "{generated_at:YYYY}", "{generated_at:%Y}",
])
def test_reject_invalid_navigation_template(template: str) -> None:
    with pytest.raises(ValueError):
        render_generation_names(SessionGenerationPreviewRequest(
            name="检查", title_template="{generator.name}", path_template=[template],
        ))
