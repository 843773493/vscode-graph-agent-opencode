"""共用的文本 unified diff；保留 difflib 的匹配、autojunk 与三行上下文语义。"""

from collections.abc import Iterator

from cdifflib import CSequenceMatcher


def iter_unified_diff(
    before_content: str, after_content: str, *, fromfile: str, tofile: str,
) -> Iterator[str]:
    before = before_content.splitlines(keepends=True)
    after = after_content.splitlines(keepends=True)
    for index, group in enumerate(CSequenceMatcher(None, before, after).get_grouped_opcodes(3)):
        if index == 0:
            yield f"--- {fromfile}\n"
            yield f"+++ {tofile}\n"
        first, last = group[0], group[-1]
        ranges = []
        for start, end in ((first[1], last[2]), (first[3], last[4])):
            length = end - start
            beginning = start + 1 if length else start
            ranges.append(str(beginning) if length == 1 else f"{beginning},{length}")
        yield f"@@ -{ranges[0]} +{ranges[1]} @@\n"
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                yield from (" " + line for line in before[i1:i2])
            if tag in {"replace", "delete"}:
                yield from ("-" + line for line in before[i1:i2])
            if tag in {"replace", "insert"}:
                yield from ("+" + line for line in after[j1:j2])
