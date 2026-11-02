"""同一次有界文件读取中的文本匹配、UTF-16 位置和上下文；不执行文件 I/O。"""

from __future__ import annotations

import bisect
import codecs
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import regex

from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationContextDTO,
    ResourceNavigationMatchDTO,
    ResourceNavigationRequest,
    ResourceNavigationSearchOptions,
)


@dataclass(frozen=True, slots=True)
class ContentMatch:
    match: ResourceNavigationMatchDTO
    context: tuple[ResourceNavigationContextDTO, ...]


def content_matches(
    uri: str, content: str, request: ResourceNavigationRequest, deadline: float, pattern: regex.Pattern[str],
    monotonic: Callable[[], float],
) -> Iterator[ContentMatch]:
    options = request.search_options or ResourceNavigationSearchOptions()
    starts = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", content))]
    astral = [] if content.isascii() else [index for index, character in enumerate(content) if ord(character) > 0xFFFF]

    def check_deadline() -> None:
        if monotonic() >= deadline:
            raise TimeoutError("文件内容匹配超过本次总期限")

    def utf16_column(offset: int, row: int) -> int:
        return offset - starts[row] + 1 + bisect.bisect_left(astral, offset) - bisect.bisect_left(astral, starts[row])

    def line_content(row: int) -> str:
        return content[starts[row]:starts[row + 1] if row + 1 < len(starts) else len(content)].rstrip("\r\n")

    def context_line(row: int) -> ResourceNavigationContextDTO:
        check_deadline()
        raw = line_content(row).encode("utf-8")
        text = codecs.getincrementaldecoder("utf-8")().decode(raw[:request.max_snippet_bytes], final=False)
        return ResourceNavigationContextDTO(uri=uri, line=row + 1, text=text, truncated=len(raw) > request.max_snippet_bytes)

    segments = [(0, content)] if options.is_multiline else (
        (start, line_content(index)) for index, start in enumerate(starts))
    last_line = -1
    for offset, segment in segments:
        check_deadline()
        for hit in pattern.finditer(segment, timeout=max(0, deadline - monotonic())):
            check_deadline()
            begin, end = offset + hit.start(), offset + hit.end()
            row, end_row = bisect.bisect_right(starts, begin) - 1, bisect.bisect_right(starts, end) - 1
            if not options.all_matches and row == last_line:
                continue
            last_line = row
            line = line_content(row)
            column = begin - starts[row]
            start = max(0, column - request.max_snippet_bytes // 8)
            raw = line[start:].encode("utf-8")
            snippet = codecs.getincrementaldecoder("utf-8")().decode(raw[:request.max_snippet_bytes], final=False)
            match = ResourceNavigationMatchDTO(uri=uri, line=row + 1, column=column + 1,
                end_line=end_row + 1, end_column=end - starts[end_row] + 1,
                utf16_column=utf16_column(begin, row), utf16_end_column=utf16_column(end, end_row),
                snippet_start_utf16_column=utf16_column(starts[row] + start, row),
                text=snippet, snippet_start_column=start + 1, truncated=start > 0 or len(raw) > request.max_snippet_bytes)
            context = tuple(context_line(index) for index in (
                *range(max(0, row - options.context_lines), row),
                *range(end_row + 1, min(len(starts), end_row + options.context_lines + 1)),
            ))
            yield ContentMatch(match, context)
            if not options.all_matches and not options.is_multiline:
                break
