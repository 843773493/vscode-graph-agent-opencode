"""所选目录内的 gitignore 规则作用域；读取及预算仍由原文件 owner 持有。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pathspec import GitIgnoreSpec

IgnoreReader = Callable[[tuple[str, ...]], tuple[str, tuple[int, ...]] | None]


class IgnorePatternError(ValueError):
    """目标规则文件无法编译。"""


@dataclass(frozen=True, slots=True)
class IgnoreScope:
    path: tuple[str, ...]
    parent: IgnoreScope | None
    git: GitIgnoreSpec | None
    ignore: GitIgnoreSpec | None
    version: tuple[int, ...]

    @classmethod
    def load(cls, path: tuple[str, ...], parent: IgnoreScope | None, read: IgnoreReader) -> IgnoreScope:
        specs = []
        version = parent.version if parent else ()
        for name in (".gitignore", ".ignore"):
            value = read((*path, name))
            version += (0,) if value is None else (1, *value[1])
            try:
                specs.append(GitIgnoreSpec.from_lines(value[0].splitlines()) if value is not None else None)
            except ValueError as error:
                raise IgnorePatternError(f"无法解析 {name}: {error}") from error
        return cls(path, parent, specs[0], specs[1], version)

    def excludes(self, path: tuple[str, ...], directory: bool) -> bool:
        # ripgrep 的 .ignore 优先于 .gitignore；同类规则由更近目录覆盖。
        for use_ignore in (True, False):
            scope: IgnoreScope | None = self
            while scope is not None:
                spec = scope.ignore if use_ignore else scope.git
                if spec is not None:
                    relative = "/".join(path[len(scope.path):]) + ("/" if directory else "")
                    included = spec.check_file(relative).include
                    if included is not None:
                        return included
                scope = scope.parent
        return False
