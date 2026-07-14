from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from app.agents.tools.apply_patch.models import Fuzz


EDIT_DISTANCE_ALLOWANCE_PER_LINE = 0.34
_PUNCTUATION_EQUIVALENTS = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "‘": "'",
        "’": "'",
        "‛": "'",
        "\u00a0": " ",
        "\u202f": " ",
    }
)


@dataclass(frozen=True, slots=True)
class IndentStyle:
    tab_size: int
    insert_spaces: bool


@dataclass(slots=True)
class FuzzMatch:
    line: int
    fuzz: Fuzz


def find_hint(lines: list[str], hint: str, start: int) -> int | None:
    canonical_hint = _canonicalize(hint)
    for trim in (False, True):
        for index in range(start, len(lines)):
            candidate = lines[index].strip() if trim else lines[index]
            if _canonicalize(candidate) == canonical_hint:
                return index + 1
    return None


def find_context(
    path: str,
    lines: list[str],
    context: list[str],
    start: int,
    eof: bool,
) -> FuzzMatch | None:
    path = path.strip()
    candidate_lines = lines[1:] if lines and path in lines[0] else lines
    candidate_context = context[1:] if context and path in context[0] else context
    if eof:
        match = _find_context_core(
            candidate_lines,
            candidate_context,
            max(0, len(candidate_lines) - len(candidate_context)),
        )
        if match is not None:
            return match
        match = _find_context_core(candidate_lines, candidate_context, start)
        if match is not None:
            match.fuzz |= Fuzz.IGNORED_EOF_SIGNAL
        return match
    return _find_context_core(candidate_lines, candidate_context, start)


def _find_context_core(
    lines: list[str],
    context: list[str],
    start: int,
) -> FuzzMatch | None:
    if not context:
        return FuzzMatch(start, Fuzz.NONE)
    canonical_lines = [_canonicalize(line) for line in lines]
    canonical_context = [_canonicalize(line) for line in context]
    match = _find_exact(canonical_lines, canonical_context, start)
    if match is not None:
        return FuzzMatch(match, Fuzz.NONE)

    trailing_lines = [line.rstrip() for line in canonical_lines]
    trailing_context = [line.rstrip() for line in canonical_context]
    match = _find_exact(trailing_lines, trailing_context, start)
    fuzz = Fuzz.IGNORED_TRAILING_WHITESPACE
    if match is not None:
        return FuzzMatch(match, fuzz)

    explicit_tab_context = [replace_explicit_tabs(line) for line in trailing_context]
    if explicit_tab_context != trailing_context:
        fuzz |= Fuzz.NORMALIZED_EXPLICIT_TAB
        match = _find_exact(trailing_lines, explicit_tab_context, start)
        if match is not None:
            return FuzzMatch(match, fuzz)

    if len(context) == 1:
        explicit_newline = replace_explicit_newlines(explicit_tab_context[0]).split("\n")
        if explicit_newline != explicit_tab_context:
            match = _find_exact(trailing_lines, explicit_newline, start)
            if match is not None:
                return FuzzMatch(match, fuzz | Fuzz.NORMALIZED_EXPLICIT_NL)

    stripped_lines = [line.strip() for line in trailing_lines]
    stripped_context = [line.strip() for line in explicit_tab_context]
    fuzz |= Fuzz.IGNORED_WHITESPACE
    match = _find_exact(stripped_lines, stripped_context, start)
    if match is not None:
        return FuzzMatch(match, fuzz)

    max_distance = int(len(context) * EDIT_DISTANCE_ALLOWANCE_PER_LINE)
    if max_distance <= 0:
        return None
    fuzz |= Fuzz.EDIT_DISTANCE_MATCH
    for index in range(max(0, start), len(stripped_lines) - len(stripped_context) + 1):
        distance = sum(
            _levenshtein(stripped_lines[index + offset], expected)
            for offset, expected in enumerate(stripped_context)
        )
        if distance <= max_distance:
            return FuzzMatch(index, fuzz)
    return None


def _find_exact(lines: list[str], context: list[str], start: int) -> int | None:
    first = max(0, start)
    last = len(lines) - len(context)
    for index in range(first, last + 1):
        if lines[index : index + len(context)] == context:
            return index
    return None


def _canonicalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).translate(_PUNCTUATION_EQUIVALENTS)


def replace_explicit_tabs(value: str) -> str:
    match = re.match(r"^(?:\s|\\t|/|#)*", value)
    prefix_end = match.end() if match else 0
    return value[:prefix_end].replace("\\t", "\t") + value[prefix_end:]


def replace_explicit_newlines(value: str) -> str:
    return replace_explicit_tabs(value.replace("\\n", "\n"))


def guess_indentation(
    lines: list[str],
    *,
    fallback: IndentStyle | None = None,
) -> IndentStyle:
    default = fallback or IndentStyle(tab_size=4, insert_spaces=False)
    tab_lines = 0
    space_lines = 0
    previous_line = ""
    previous_indent = 0
    scores = [0] * 9
    for line in lines[:10000]:
        indent = len(line) - len(line.lstrip(" \t"))
        if indent == len(line):
            continue
        prefix = line[:indent]
        tabs = prefix.count("\t")
        spaces = prefix.count(" ")
        if tabs:
            tab_lines += 1
        elif spaces > 1:
            space_lines += 1
        difference, looks_like_alignment = _spaces_difference(
            previous_line,
            previous_indent,
            line,
            indent,
        )
        if looks_like_alignment and not (
            default.insert_spaces and default.tab_size == difference
        ):
            continue
        if 0 <= difference <= 8:
            scores[difference] += 1
        previous_line = line
        previous_indent = indent

    insert_spaces = default.insert_spaces
    if tab_lines != space_lines:
        insert_spaces = tab_lines < space_lines
    tab_size = default.tab_size
    if insert_spaces:
        best_score = 0
        for candidate in (2, 4, 6, 8, 3, 5, 7):
            if scores[candidate] > best_score:
                best_score = scores[candidate]
                tab_size = candidate
        if (
            tab_size == 4
            and scores[4] > 0
            and scores[2] > 0
            and scores[2] >= scores[4] / 2
        ):
            tab_size = 2
    return IndentStyle(tab_size=tab_size, insert_spaces=insert_spaces)


def indent_level(line: str, tab_size: int) -> int:
    level = 0
    for character in line:
        if character == " ":
            level += 1
        elif character == "\t":
            level += tab_size - level % tab_size
        else:
            break
    return level // tab_size


def indent_unit(style: IndentStyle) -> str:
    return " " * style.tab_size if style.insert_spaces else "\t"


def transform_indentation(
    line: str,
    source: IndentStyle,
    target: IndentStyle,
) -> str:
    if source == target:
        return line
    source_unit = indent_unit(source)
    target_unit = indent_unit(target)
    offset = 0
    while line[offset : offset + len(source_unit)] == source_unit:
        offset += len(source_unit)
    return target_unit * (offset // len(source_unit)) + line[offset:]


def _spaces_difference(
    left: str,
    left_indent: int,
    right: str,
    right_indent: int,
) -> tuple[int, bool]:
    shared = 0
    while (
        shared < left_indent
        and shared < right_indent
        and left[shared] == right[shared]
    ):
        shared += 1
    left_suffix = left[shared:left_indent]
    right_suffix = right[shared:right_indent]
    left_spaces = left_suffix.count(" ")
    left_tabs = left_suffix.count("\t")
    right_spaces = right_suffix.count(" ")
    right_tabs = right_suffix.count("\t")
    if (left_spaces and left_tabs) or (right_spaces and right_tabs):
        return 0, False
    tab_difference = abs(left_tabs - right_tabs)
    space_difference = abs(left_spaces - right_spaces)
    if tab_difference == 0:
        looks_like_alignment = (
            space_difference > 0
            and 0 <= right_spaces - 1 < len(left)
            and right_spaces < len(right)
            and right[right_spaces] != " "
            and left[right_spaces - 1] == " "
            and left.endswith(",")
        )
        return space_difference, looks_like_alignment
    if space_difference % tab_difference == 0:
        return space_difference // tab_difference, False
    return 0, False


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]
