import re

from app.gateway.theme.builtins import WARM_TOKENS

CSS_FORBIDDEN = re.compile(r"[;{}]|url\s*\(", re.IGNORECASE)
_COLOR_VALUE = re.compile(
    r"^(?:#[0-9a-f]{3,8}|(?:rgb|rgba|hsl|hsla|lab|lch|oklab|oklch|color|color-mix|var)\(.+\)|transparent|currentColor|[a-z]+)$",
    re.IGNORECASE,
)
_LENGTH_VALUE = re.compile(r"^(?:0|\d+(?:\.\d+)?(?:px|rem|em|%)|var\(.+\))$")
_POSITION_SIZE_VALUE = re.compile(r"^[a-z0-9.%()\-+ /,]+$", re.IGNORECASE)
_COLOR_TOKENS = {
    name
    for name in WARM_TOKENS
    if any(
        marker in name
        for marker in (
            "background",
            "foreground",
            "border",
            "accent",
            "status",
            "text-",
            "icon-",
            "link-",
            "highlight",
        )
    )
} | {
    "--bt-chrome-surface",
    "--bt-workspace-surface",
    "--bt-panel-surface",
    "--bt-floating-surface",
    "--bt-critical-surface",
}
_COLOR_TOKENS -= {
    "--bt-background-image",
    "--bt-background-overlay",
    "--bt-background-position",
    "--bt-background-size",
    "--bt-background-repeat",
    "--bt-surface-border",
}
_LENGTH_TOKENS = {
    "--bt-font-size",
    "--bt-font-size-heading",
    "--bt-font-size-label",
    "--bt-radius-small",
    "--bt-radius-medium",
    "--bt-radius-large",
    "--bt-panel-radius",
    "--bt-surface-gap",
}


def validate_token(name: str, value: str) -> None:
    if name not in WARM_TOKENS:
        raise ValueError(f"未知主题 token: {name}")
    if not value.strip():
        raise ValueError(f"主题 token 不允许为空: {name}")
    if CSS_FORBIDDEN.search(value):
        raise ValueError(f"主题 token 包含不允许的 CSS 内容: {name}")
    normalized = value.strip()
    if name in _COLOR_TOKENS and not _COLOR_VALUE.fullmatch(normalized):
        raise ValueError(f"主题颜色 token 值无效: {name}={value!r}")
    if name in _LENGTH_TOKENS and not _LENGTH_VALUE.fullmatch(normalized):
        raise ValueError(f"主题尺寸 token 值无效: {name}={value!r}")
    if name == "--bt-background-image" and normalized != "none":
        raise ValueError(
            "--bt-background-image 只能为 none；图片必须使用 background 字段"
        )
    if name == "--bt-background-repeat" and normalized not in {
        "no-repeat",
        "repeat",
        "repeat-x",
        "repeat-y",
        "space",
        "round",
    }:
        raise ValueError(f"主题背景重复 token 值无效: {value!r}")
    if name in {"--bt-background-position", "--bt-background-size"} and not (
        _POSITION_SIZE_VALUE.fullmatch(normalized)
    ):
        raise ValueError(f"主题背景布局 token 值无效: {name}={value!r}")
    if name == "--bt-surface-backdrop-filter" and not (
        normalized == "none"
        or re.fullmatch(
            r"(?:(?:blur|saturate|brightness|contrast)\([0-9.]+(?:px|%|)?\)\s*)+",
            normalized,
        )
    ):
        raise ValueError(f"主题表面滤镜 token 值无效: {value!r}")


def validate_background_display_tokens(tokens: dict[str, str]) -> None:
    for name, value in tokens.items():
        if not value.strip() or CSS_FORBIDDEN.search(value):
            raise ValueError(f"背景展示参数包含不允许的 CSS 内容: {name}")
