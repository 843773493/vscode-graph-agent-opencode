from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from app.agents.tools.apply_patch.matcher import (
    IndentStyle,
    find_context,
    find_hint,
    guess_indentation,
    indent_level,
    indent_unit,
    replace_explicit_newlines,
    replace_explicit_tabs,
    transform_indentation,
)
from app.agents.tools.apply_patch.models import (
    ActionType,
    Chunk,
    Commit,
    DiffError,
    FileChange,
    Fuzz,
    InvalidContextError,
    InvalidPatchFormatError,
    Patch,
    PatchAction,
)


PATCH_PREFIX = "*** Begin Patch"
PATCH_SUFFIX = "*** End Patch"
ADD_FILE_PREFIX = "*** Add File: "
DELETE_FILE_PREFIX = "*** Delete File: "
UPDATE_FILE_PREFIX = "*** Update File: "
MOVE_FILE_TO_PREFIX = "*** Move to: "
END_OF_FILE_PREFIX = "*** End of File"
CHUNK_DELIMITER = "@@"
AVOID_EXPLICIT_TABS_REGEX = re.compile(r"\.(tex|latex|sty|cls|bib|bst|ins)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Section:
    context: list[str]
    chunks: list[Chunk]
    end_index: int
    eof: bool
    fuzz_merges: int


class _Mode(StrEnum):
    ADD = "add"
    DELETE = "delete"
    KEEP = "keep"


class _Parser:
    def __init__(self, current_files: dict[str, str], lines: list[str]) -> None:
        self.current_files = current_files
        self.lines = lines
        self.index = 0
        self.patch = Patch()
        self.fuzz = Fuzz.NONE
        self.indent_styles = {
            path: guess_indentation(content.split("\n"))
            for path, content in current_files.items()
        }

    def parse(self) -> None:
        while not self._is_done((PATCH_SUFFIX,)):
            path = self._read_prefixed(UPDATE_FILE_PREFIX)
            if path:
                self._reject_duplicate(path, "Update")
                move_to = self._read_prefixed(MOVE_FILE_TO_PREFIX)
                if path not in self.current_files:
                    raise DiffError(f"Update File Error: Missing File: {path}")
                action = self._parse_update_file(
                    path,
                    self.current_files[path],
                    self.indent_styles[path],
                )
                action.move_path = move_to or None
                self.patch.actions[path] = action
                continue

            path = self._read_prefixed(DELETE_FILE_PREFIX)
            if path:
                self._reject_duplicate(path, "Delete")
                if path not in self.current_files:
                    raise DiffError(f"Delete File Error: Missing File: {path}")
                self.patch.actions[path] = PatchAction(type=ActionType.DELETE)
                continue

            path = self._read_prefixed(ADD_FILE_PREFIX)
            if path:
                self._reject_duplicate(path, "Add")
                if path in self.current_files:
                    raise DiffError(f"Add File Error: File already exists: {path}")
                self.patch.actions[path] = self._parse_add_file()
                continue

            current = self.lines[self.index] if self.index < len(self.lines) else "<EOF>"
            raise DiffError(f"Unknown Line: {current}")

        if not self._starts_with(PATCH_SUFFIX):
            raise InvalidPatchFormatError("Missing End Patch")
        self.index += 1

    def _parse_update_file(
        self,
        path: str,
        text: str,
        target_indent: IndentStyle,
    ) -> PatchAction:
        action = PatchAction(type=ActionType.UPDATE)
        file_lines = text.split("\n")
        should_replace_explicit_tabs = not AVOID_EXPLICIT_TABS_REGEX.search(path.rstrip())
        search_index = 0

        while not self._is_done(
            (
                PATCH_SUFFIX,
                UPDATE_FILE_PREFIX,
                DELETE_FILE_PREFIX,
                ADD_FILE_PREFIX,
                END_OF_FILE_PREFIX,
            )
        ):
            section_line = self._read_prefixed(CHUNK_DELIMITER, return_entire=True)
            section_hint = section_line[len(CHUNK_DELIMITER) :].strip()
            if not section_line and search_index != 0:
                current = self.lines[self.index] if self.index < len(self.lines) else "<EOF>"
                raise DiffError(
                    "Invalid line. Consider splitting each change into individual "
                    f"apply_patch tool calls:\n{current}"
                )
            if section_hint:
                hinted_index = find_hint(file_lines, section_hint, search_index)
                if hinted_index is not None:
                    search_index = hinted_index

            section = _peek_next_section(self.lines, self.index)
            match = None
            for fuzz_merge in range(section.fuzz_merges + 1):
                if fuzz_merge:
                    section = _peek_next_section(self.lines, self.index, fuzz_merge)
                match = find_context(
                    path,
                    file_lines,
                    section.context,
                    search_index,
                    section.eof,
                )
                if match is None:
                    match = find_context(path, file_lines, section.context, 0, section.eof)
                if match is not None:
                    if fuzz_merge:
                        match.fuzz |= Fuzz.MERGED_OPERATOR_SECTION
                    break

            if match is None:
                context = "\n".join(section.context)
                location = "EOF context" if section.eof else "context"
                raise InvalidContextError(
                    f"Invalid {location} at line {search_index}:\n{context}",
                    file_path=path,
                )

            self.fuzz |= match.fuzz
            source_indent = guess_indentation(
                [line for chunk in section.chunks for line in chunk.ins_lines]
                + section.context,
                fallback=target_indent,
            )
            matched_indent = indent_level(
                file_lines[match.line] if match.line < len(file_lines) else "",
                target_indent.tab_size,
            )
            first_context = section.context[0] if section.context else ""
            if match.fuzz & Fuzz.NORMALIZED_EXPLICIT_NL:
                first_context = replace_explicit_newlines(first_context)
            elif match.fuzz & Fuzz.NORMALIZED_EXPLICIT_TAB:
                first_context = replace_explicit_tabs(first_context)
            source_level = indent_level(first_context, source_indent.tab_size)
            extra_indent = indent_unit(target_indent) * max(0, matched_indent - source_level)

            for chunk in section.chunks:
                chunk.orig_index += match.line
                if match.fuzz & Fuzz.NORMALIZED_EXPLICIT_NL:
                    chunk.ins_lines = [replace_explicit_newlines(line) for line in chunk.ins_lines]
                    chunk.del_lines = [replace_explicit_newlines(line) for line in chunk.del_lines]
                if should_replace_explicit_tabs or match.fuzz & Fuzz.NORMALIZED_EXPLICIT_TAB:
                    chunk.ins_lines = [replace_explicit_tabs(line) for line in chunk.ins_lines]
                chunk.ins_lines = [
                    line
                    if not line.strip()
                    else extra_indent + transform_indentation(line, source_indent, target_indent)
                    for line in chunk.ins_lines
                ]
                if match.fuzz & Fuzz.NORMALIZED_EXPLICIT_TAB:
                    chunk.del_lines = [replace_explicit_tabs(line) for line in chunk.del_lines]
                action.chunks.append(chunk)

            search_index = match.line + len(section.context)
            self.index = section.end_index

        return action

    def _parse_add_file(self) -> PatchAction:
        content: list[str] = []
        while not self._is_done(
            (PATCH_SUFFIX, UPDATE_FILE_PREFIX, DELETE_FILE_PREFIX, ADD_FILE_PREFIX)
        ):
            line = self._read_prefixed("", return_entire=True)
            if not line.startswith("+"):
                raise InvalidPatchFormatError(f"Invalid Add File Line: {line}")
            content.append(line[1:])
        return PatchAction(type=ActionType.ADD, new_file="\n".join(content))

    def _is_done(self, prefixes: tuple[str, ...]) -> bool:
        if self.index >= len(self.lines):
            return True
        current = self.lines[self.index]
        return any(current.startswith(prefix.strip()) for prefix in prefixes)

    def _starts_with(self, prefix: str) -> bool:
        return self.index < len(self.lines) and self.lines[self.index].startswith(prefix)

    def _read_prefixed(self, prefix: str, *, return_entire: bool = False) -> str:
        if self.index >= len(self.lines):
            raise DiffError(f"Index {self.index} exceeds patch length {len(self.lines)}")
        current = self.lines[self.index]
        if not current.startswith(prefix):
            return ""
        self.index += 1
        return current if return_entire else current[len(prefix) :]

    def _reject_duplicate(self, path: str, operation: str) -> None:
        if path in self.patch.actions:
            raise DiffError(f"{operation} File Error: Duplicate Path: {path}")


def parse_patch(text: str, current_files: dict[str, str]) -> Commit:
    if not isinstance(text, str) or not text.startswith(PATCH_PREFIX + "\n"):
        raise InvalidPatchFormatError("Patch must start with *** Begin Patch\\n")
    lines = text.strip().split("\n")
    if len(lines) < 2:
        raise InvalidPatchFormatError("Invalid patch text")
    if not lines[0].startswith(PATCH_PREFIX):
        raise InvalidPatchFormatError(f"Patch must start with {PATCH_PREFIX}")
    if lines[-1] != PATCH_SUFFIX:
        lines.append(PATCH_SUFFIX)

    parser = _Parser(current_files, lines)
    parser.index = 1
    parser.parse()
    return _patch_to_commit(parser.patch, current_files, parser.fuzz)


def identify_files_affected(text: str) -> list[str]:
    prefixes = (
        UPDATE_FILE_PREFIX,
        DELETE_FILE_PREFIX,
        MOVE_FILE_TO_PREFIX,
        ADD_FILE_PREFIX,
    )
    result: dict[str, None] = {}
    for line in text.strip().split("\n"):
        for prefix in prefixes:
            if line.startswith(prefix):
                result.setdefault(line[len(prefix) :], None)
                break
    return list(result)


def identify_files_needed(text: str) -> list[str]:
    result: dict[str, None] = {}
    for line in text.strip().split("\n"):
        for prefix in (UPDATE_FILE_PREFIX, DELETE_FILE_PREFIX):
            if line.startswith(prefix):
                result.setdefault(line[len(prefix) :], None)
                break
    return list(result)


def identify_files_added(text: str) -> list[str]:
    return [
        line[len(ADD_FILE_PREFIX) :]
        for line in text.strip().split("\n")
        if line.startswith(ADD_FILE_PREFIX)
    ]


def _patch_to_commit(
    patch: Patch,
    current_files: dict[str, str],
    fuzz: Fuzz,
) -> Commit:
    changes: dict[str, FileChange] = {}
    for path, action in patch.actions.items():
        if action.type == ActionType.DELETE:
            changes[path] = FileChange(type=ActionType.DELETE, old_content=current_files[path])
        elif action.type == ActionType.ADD:
            changes[path] = FileChange(type=ActionType.ADD, new_content=action.new_file or "")
        else:
            old_content = current_files[path]
            changes[path] = FileChange(
                type=ActionType.UPDATE,
                old_content=old_content,
                new_content=_get_updated_file(old_content, action, path),
                move_path=action.move_path,
            )
    return Commit(changes=changes, fuzz=fuzz)


def _get_updated_file(text: str, action: PatchAction, path: str) -> str:
    source = text.split("\n")
    destination: list[str] = []
    source_index = 0
    for chunk in action.chunks:
        if chunk.orig_index > len(source):
            raise DiffError(
                f"{path}: chunk.origIndex {chunk.orig_index} > len(lines) {len(source)}"
            )
        if source_index > chunk.orig_index:
            raise DiffError(
                f"{path}: origIndex {source_index} > chunk.origIndex {chunk.orig_index}"
            )
        destination.extend(source[source_index : chunk.orig_index])
        source_index = chunk.orig_index
        destination.extend(chunk.ins_lines)
        source_index += len(chunk.del_lines)
    destination.extend(source[source_index:])
    return "\n".join(destination)


def _peek_next_section(lines: list[str], initial_index: int, fuzz_merge: int = 0) -> _Section:
    index = initial_index
    old: list[str] = []
    deleted: list[str] = []
    inserted: list[str] = []
    chunks: list[Chunk] = []
    mode = _Mode.KEEP
    fuzz_merge_number = 0
    section_prefixes = (
        CHUNK_DELIMITER,
        PATCH_SUFFIX,
        UPDATE_FILE_PREFIX,
        DELETE_FILE_PREFIX,
        ADD_FILE_PREFIX,
        END_OF_FILE_PREFIX,
    )

    while index < len(lines):
        current = lines[index]
        if any(current.startswith(prefix.strip()) for prefix in section_prefixes):
            if mode == _Mode.KEEP and old and not old[-1].strip():
                old.pop()
            break
        if current == "***":
            break
        if current.startswith("***"):
            raise InvalidPatchFormatError(f"Invalid Line: {current}")
        index += 1
        last_mode = mode
        line = current
        if line.startswith("+"):
            mode = _Mode.ADD
        elif line.startswith("-"):
            mode = _Mode.DELETE
        elif line.startswith(" "):
            mode = _Mode.KEEP
        else:
            next_line = lines[index] if index < len(lines) else ""
            next_mode = (
                _Mode.ADD
                if next_line.startswith("+")
                else _Mode.DELETE
                if next_line.startswith("-")
                else _Mode.KEEP
            )
            can_fuzz = mode != _Mode.KEEP and next_mode == mode
            mode = _Mode.KEEP
            line = " " + line
            if can_fuzz:
                fuzz_merge_number += 1
                if fuzz_merge == fuzz_merge_number:
                    mode = next_mode

        line = line[1:]
        if mode == _Mode.KEEP and last_mode != mode:
            if inserted or deleted:
                chunks.append(
                    Chunk(
                        orig_index=len(old) - len(deleted),
                        del_lines=deleted,
                        ins_lines=inserted,
                    )
                )
            deleted = []
            inserted = []
        if mode == _Mode.DELETE:
            deleted.append(line)
            old.append(line)
        elif mode == _Mode.ADD:
            inserted.append(line)
        else:
            old.append(line)

    if inserted or deleted:
        chunks.append(
            Chunk(
                orig_index=len(old) - len(deleted),
                del_lines=deleted,
                ins_lines=inserted,
            )
        )
    eof = index < len(lines) and lines[index] == END_OF_FILE_PREFIX
    if eof:
        index += 1
    return _Section(old, chunks, index, eof, fuzz_merge_number)
