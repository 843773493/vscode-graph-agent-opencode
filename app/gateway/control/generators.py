from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.core.session_paths import validate_generator_physical_segment
from app.schemas.gateway_control import (
    GenerationRunDTO,
    GenerationRunListDTO,
    GeneratorDefinitionCreateRequest,
    GeneratorDefinitionDTO,
    GeneratorDefinitionListDTO,
    GeneratorDefinitionUpdateRequest,
    GeneratorPlacementPreviewDTO,
    GeneratorPlacementPreviewRequest,
)
from app.gateway.control.storage import atomic_write_json, read_json_object


_TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")


class SessionGeneratorStore:
    def __init__(self, *, root: Path) -> None:
        self._root = root
        self._definitions_dir = root / "generators"
        self._runs_dir = root / "generation-runs"
        self._schedules_dir = root / "generator-schedules"

    def list_definitions(self) -> GeneratorDefinitionListDTO:
        items = self._read_all_definitions()
        return GeneratorDefinitionListDTO(
            revision=self._definitions_revision(items),
            items=items,
        )

    def get_definition(self, generator_id: str) -> GeneratorDefinitionDTO:
        path = self._definition_path(generator_id)
        if not path.exists():
            raise KeyError(f"会话生成器不存在: {generator_id}")
        return GeneratorDefinitionDTO.model_validate(
            read_json_object(path, default={})
        )

    def create_definition(
        self,
        payload: GeneratorDefinitionCreateRequest,
    ) -> GeneratorDefinitionDTO:
        now = datetime.now(timezone.utc)
        definition = GeneratorDefinitionDTO(
            **payload.model_dump(),
            generator_id=create_prefixed_id("gen"),
            status="ready" if payload.enabled else "paused",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self._write_definition(definition)
        return definition

    def update_definition(
        self,
        generator_id: str,
        payload: GeneratorDefinitionUpdateRequest,
    ) -> GeneratorDefinitionDTO:
        current = self.get_definition(generator_id)
        values = payload.model_dump(exclude_unset=True)
        updated = current.model_copy(
            update={
                **values,
                "status": (
                    "ready"
                    if values.get("enabled", current.enabled)
                    else "paused"
                ),
                "status_reason": None,
                "revision": current.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._write_definition(updated)
        return updated

    def delete_definition(self, generator_id: str) -> GeneratorDefinitionDTO:
        current = self.get_definition(generator_id)
        path = self._definition_path(generator_id)
        path.unlink()
        return current

    def set_definition_status(
        self,
        generator_id: str,
        *,
        status: str,
        reason: str | None,
    ) -> GeneratorDefinitionDTO:
        if status not in {"ready", "paused", "blocked"}:
            raise ValueError(f"非法生成器状态: {status}")
        current = self.get_definition(generator_id)
        updated = current.model_copy(
            update={
                "status": status,
                "status_reason": reason,
                "revision": current.revision + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._write_definition(updated)
        return updated

    def preview(
        self,
        payload: GeneratorPlacementPreviewRequest,
    ) -> GeneratorPlacementPreviewDTO:
        if payload.session_strategy.mode == "continue_existing":
            target = payload.session_strategy.target
            if target is None:
                raise ValueError("continue_existing 预览缺少目标会话")
            return GeneratorPlacementPreviewDTO(
                title=payload.session_title,
                path_segments=[],
                session_path_segment="",
                relative_path=(
                    f"<继续现有会话:{target.workspace_id}/{target.session_id}>"
                    "（不创建物理目录）"
                ),
            )
        generated_at = payload.generated_at or datetime.now(timezone.utc)
        values = {
            "generator.name": payload.name,
            "session.title": payload.session_title,
        }
        title = self._render_segment(
            payload.naming.title_template,
            values=values,
            generated_at=generated_at,
        )
        path_segments = [
            self._render_segment(
                segment,
                values={**values, "session.title": title},
                generated_at=generated_at,
            )
            for segment in payload.naming.path_template
        ]
        session_path_segment = "<session-id>"
        placement_prefix: str | None = None
        if payload.placement is not None:
            if payload.placement.kind == "workspace":
                placement_prefix = (
                    f"<工作区会话根:{payload.placement.workspace_id}>"
                )
            elif payload.placement.kind == "session":
                placement_prefix = (
                    f"<会话:{payload.placement.session_id}>/children"
                )
            else:
                placement_prefix = f"<会话文件夹:{payload.placement.folder_id}>"
        physical_folder_segments = ["<folder-id>" for _ in path_segments]
        return GeneratorPlacementPreviewDTO(
            title=title,
            path_segments=path_segments,
            session_path_segment=session_path_segment,
            relative_path="/".join(
                [
                    *([placement_prefix] if placement_prefix else []),
                    *physical_folder_segments,
                    session_path_segment,
                ]
            ),
        )

    def find_run_by_idempotency_key(
        self,
        generator_id: str,
        idempotency_key: str,
    ) -> GenerationRunDTO | None:
        for item in self.list_runs(generator_id).items:
            if item.idempotency_key == idempotency_key:
                return item
        return None

    def create_run(
        self,
        *,
        generator_id: str,
        idempotency_key: str,
        trigger_type: str,
        scheduled_for: datetime,
    ) -> GenerationRunDTO:
        existing = self.find_run_by_idempotency_key(generator_id, idempotency_key)
        if existing is not None:
            return existing
        run = GenerationRunDTO(
            run_id=create_prefixed_id("grun"),
            generator_id=generator_id,
            idempotency_key=idempotency_key,
            status="planned",
            trigger_type=trigger_type,
            scheduled_for=scheduled_for,
        )
        self.write_run(run)
        return run

    def write_run(self, run: GenerationRunDTO) -> None:
        atomic_write_json(
            self._run_path(run.generator_id, run.run_id),
            run.model_dump(mode="json"),
        )

    def get_run(self, generator_id: str, run_id: str) -> GenerationRunDTO:
        path = self._run_path(generator_id, run_id)
        if not path.exists():
            raise KeyError(
                f"会话生成运行记录不存在: generator_id={generator_id}, run_id={run_id}"
            )
        return GenerationRunDTO.model_validate(read_json_object(path, default={}))

    def list_runs(self, generator_id: str) -> GenerationRunListDTO:
        directory = self._runs_dir / generator_id
        if not directory.exists():
            return GenerationRunListDTO()
        items = [
            GenerationRunDTO.model_validate(read_json_object(path, default={}))
            for path in directory.glob("*.json")
            if path.is_file()
        ]
        items.sort(key=lambda item: (item.scheduled_for, item.run_id), reverse=True)
        return GenerationRunListDTO(items=items)

    def read_schedule_state(self, generator_id: str) -> dict[str, object] | None:
        path = self._schedule_path(generator_id)
        if not path.exists():
            return None
        return read_json_object(path, default={})

    def write_schedule_state(
        self,
        generator_id: str,
        value: dict[str, object],
    ) -> None:
        atomic_write_json(self._schedule_path(generator_id), value)

    def _read_all_definitions(self) -> list[GeneratorDefinitionDTO]:
        if not self._definitions_dir.exists():
            return []
        items = [
            GeneratorDefinitionDTO.model_validate(read_json_object(path, default={}))
            for path in self._definitions_dir.glob("*.json")
            if path.is_file()
        ]
        items.sort(key=lambda item: (item.name.casefold(), item.generator_id))
        return items

    def _write_definition(self, definition: GeneratorDefinitionDTO) -> None:
        atomic_write_json(
            self._definition_path(definition.generator_id),
            definition.model_dump(mode="json"),
        )

    def _definition_path(self, generator_id: str) -> Path:
        self._validate_id(generator_id, prefix="gen_")
        return self._definitions_dir / f"{generator_id}.json"

    def _run_path(self, generator_id: str, run_id: str) -> Path:
        self._validate_id(generator_id, prefix="gen_")
        self._validate_id(run_id, prefix="grun_")
        return self._runs_dir / generator_id / f"{run_id}.json"

    def _schedule_path(self, generator_id: str) -> Path:
        self._validate_id(generator_id, prefix="gen_")
        return self._schedules_dir / f"{generator_id}.json"

    @staticmethod
    def _validate_id(value: str, *, prefix: str) -> None:
        if not value.startswith(prefix) or "/" in value or "\\" in value:
            raise ValueError(f"非法 ID: {value}")

    @staticmethod
    def _definitions_revision(items: list[GeneratorDefinitionDTO]) -> str:
        encoded = json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _render_segment(
        cls,
        template: str,
        *,
        values: dict[str, str],
        generated_at: datetime,
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.startswith("generated_at:"):
                pattern = token.removeprefix("generated_at:")
                format_tokens = {
                    "yyyy": "%Y",
                    "MM": "%m",
                    "dd": "%d",
                    "HH": "%H",
                    "mm": "%M",
                    "ss": "%S",
                }
                unknown_pattern = pattern
                for source in format_tokens:
                    unknown_pattern = unknown_pattern.replace(source, "")
                if re.search(r"[A-Za-z]", unknown_pattern):
                    raise ValueError(f"命名模板包含未知时间格式: {pattern}")
                strftime_pattern = pattern
                for source, target in format_tokens.items():
                    strftime_pattern = strftime_pattern.replace(source, target)
                return generated_at.strftime(strftime_pattern)
            value = values.get(token)
            if value is None:
                raise ValueError(f"命名模板包含未知变量: {token}")
            return value

        rendered = _TOKEN_PATTERN.sub(replace, template).strip()
        cls._validate_path_segment(rendered)
        return rendered

    @staticmethod
    def _validate_path_segment(value: str) -> None:
        validate_generator_physical_segment(value)
