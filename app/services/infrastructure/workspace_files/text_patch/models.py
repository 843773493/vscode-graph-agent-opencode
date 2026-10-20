from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag, StrEnum


class ActionType(StrEnum):
    ADD = "add"
    DELETE = "delete"
    UPDATE = "update"


class Fuzz(IntFlag):
    NONE = 0
    IGNORED_TRAILING_WHITESPACE = 1 << 1
    NORMALIZED_EXPLICIT_TAB = 1 << 2
    IGNORED_WHITESPACE = 1 << 3
    EDIT_DISTANCE_MATCH = 1 << 4
    IGNORED_EOF_SIGNAL = 1 << 5
    MERGED_OPERATOR_SECTION = 1 << 6
    NORMALIZED_EXPLICIT_NL = 1 << 7


@dataclass(slots=True)
class Chunk:
    orig_index: int
    del_lines: list[str] = field(default_factory=list)
    ins_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PatchAction:
    type: ActionType
    chunks: list[Chunk] = field(default_factory=list)
    new_file: str | None = None
    move_path: str | None = None


@dataclass(slots=True)
class Patch:
    actions: dict[str, PatchAction] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FileChange:
    type: ActionType
    old_content: str | None = None
    new_content: str | None = None
    move_path: str | None = None


@dataclass(frozen=True, slots=True)
class Commit:
    changes: dict[str, FileChange]
    fuzz: Fuzz = Fuzz.NONE


class DiffError(ValueError):
    pass


class InvalidPatchFormatError(DiffError):
    pass


class InvalidContextError(DiffError):
    def __init__(self, message: str, *, file_path: str) -> None:
        super().__init__(message)
        self.file_path = file_path
