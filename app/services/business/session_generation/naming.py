"""生成会话的导航模板渲染；不推导物理目录。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.path_segments import validate_generator_name
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationPreviewRequest,
)

_TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")
_FORMAT_TOKENS = {
    "yyyy": "%Y", "MM": "%m", "dd": "%d",
    "HH": "%H", "mm": "%M", "ss": "%S",
}
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def validate_navigation_segment(value: str) -> None:
    validate_generator_name(value)
    if (
        any(character in value for character in '/\\<>:"|?*')
        or any(ord(character) < 32 for character in value)
        or value.endswith((".", " "))
        or _RESERVED_NAME.match(value)
    ):
        raise ValueError(f"生成器导航路径段非法: {value!r}")


def render_generation_names(payload: SessionGenerationPreviewRequest) -> tuple[str, list[str]]:
    generated_at = payload.generated_at or datetime.now(UTC)
    values = {"generator.name": payload.name, "session.title": payload.session_title}

    def render(template: str) -> str:
        if "{" in _TOKEN_PATTERN.sub("", template) or "}" in _TOKEN_PATTERN.sub("", template):
            raise ValueError(f"命名模板包含未闭合变量: {template}")

        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.startswith("generated_at:"):
                pattern = token.removeprefix("generated_at:")
                remaining = pattern
                for source in _FORMAT_TOKENS:
                    remaining = remaining.replace(source, "")
                if not pattern or re.search(r"[A-Za-z%]", remaining):
                    raise ValueError(f"命名模板包含未知时间格式: {pattern}")
                for source, target in _FORMAT_TOKENS.items():
                    pattern = pattern.replace(source, target)
                return generated_at.strftime(pattern)
            if token not in values:
                raise ValueError(f"命名模板包含未知变量: {token}")
            return values[token]

        rendered = _TOKEN_PATTERN.sub(replace, template).strip()
        validate_generator_name(rendered)
        return rendered

    title = render(payload.title_template)
    values["session.title"] = title
    segments = [render(template) for template in payload.path_template]
    for segment in segments:
        validate_navigation_segment(segment)
    return title, segments
