"""SKILL.md 安全 frontmatter parser。

按 OpenSpec 4.2 合同实现:只接受唯一、有界标量 name/description,拒绝
custom tag、alias、对象/列表构造、重复字段与非标量值;其它字段不进入
facet payload。activation 只使用 frontmatter 结束 offset 之后的精确正文。

本模块刻意不使用 YAML 库:确定性行式文法本身就能表达 Skill metadata,
同时从根上关闭 alias/tag/嵌套结构的攻击面。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SKILL_NAME_MAX_LENGTH = 64
SKILL_DESCRIPTION_MAX_LENGTH = 1024

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillFrontmatterError(ValueError):
    """SKILL.md frontmatter 不满足安全合同。"""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ParsedSkillFrontmatter:
    """解析后的安全 metadata 与 activation 正文起点。

    body_offset 是 frontmatter 结束行之后的字符 offset;activation facet
    只消费 content[body_offset:] 的精确正文,不做任何规范化。
    """

    name: str
    description: str
    body_offset: int


def parse_skill_frontmatter(content: str) -> ParsedSkillFrontmatter:
    """解析 SKILL.md frontmatter,违反安全合同直接抛错,不做静默降级。"""
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SkillFrontmatterError(
            "missing_frontmatter",
            "SKILL.md 必须以 --- frontmatter 开始",
        )
    seen: dict[str, str] = {}
    line_index = 1
    close_index: int | None = None
    while line_index < len(lines):
        raw_line = lines[line_index]
        stripped = raw_line.rstrip("\r\n")
        if stripped == "---":
            close_index = line_index
            break
        if not stripped.strip():
            line_index += 1
            continue
        if stripped != stripped.lstrip():
            raise SkillFrontmatterError(
                "unsupported_structure",
                f"frontmatter 不允许缩进嵌套行: {stripped!r}",
            )
        key, separator, raw_value = stripped.partition(":")
        if not separator:
            raise SkillFrontmatterError(
                "invalid_frontmatter",
                f"frontmatter 行必须是 'key: value' 标量形式: {stripped!r}",
            )
        key = key.strip()
        value = raw_value.strip()
        _reject_unsafe_scalar(key, value)
        if key in seen:
            raise SkillFrontmatterError(
                "duplicate_key",
                f"frontmatter 字段重复: {key}",
            )
        seen[key] = value
        line_index += 1
    if close_index is None:
        raise SkillFrontmatterError(
            "missing_frontmatter",
            "SKILL.md frontmatter 缺少结束 --- 分隔行",
        )
    name = seen.get("name")
    description = seen.get("description")
    if not name:
        raise SkillFrontmatterError(
            "missing_name",
            "SKILL.md frontmatter 缺少 name 标量字段",
        )
    if not description:
        raise SkillFrontmatterError(
            "missing_description",
            "SKILL.md frontmatter 缺少 description 标量字段",
        )
    if len(name) > SKILL_NAME_MAX_LENGTH:
        raise SkillFrontmatterError(
            "name_too_long",
            f"Skill name 超过 {SKILL_NAME_MAX_LENGTH} 字符: {name!r}",
        )
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillFrontmatterError(
            "invalid_name",
            f"Skill name 只允许小写字母/数字与单连字符: {name!r}",
        )
    if len(description) > SKILL_DESCRIPTION_MAX_LENGTH:
        raise SkillFrontmatterError(
            "description_too_long",
            f"Skill description 超过 {SKILL_DESCRIPTION_MAX_LENGTH} 字符",
        )
    body_offset = sum(len(line) for line in lines[: close_index + 1])
    return ParsedSkillFrontmatter(
        name=name,
        description=description,
        body_offset=body_offset,
    )


def _reject_unsafe_scalar(key: str, value: str) -> None:
    """拒绝 custom tag、alias、流集合、块标量与控制字符等非平凡构造。"""
    if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
        raise SkillFrontmatterError(
            "invalid_frontmatter",
            f"frontmatter 字段名无效: {key!r}",
        )
    if value[:1] in {"&", "*", "!", "{", "[", "|", ">", "\"", "'"}:
        raise SkillFrontmatterError(
            "unsupported_structure",
            f"frontmatter 字段 {key} 的值必须是裸标量,拒绝 alias/tag/对象/块标量: {value!r}",
        )
    if any(ord(ch) < 0x20 for ch in value):
        raise SkillFrontmatterError(
            "unsupported_structure",
            f"frontmatter 字段 {key} 的值包含控制字符",
        )
