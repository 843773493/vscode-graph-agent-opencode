from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from app.core.identifier import create_prefixed_id
from app.gateway.control.storage import atomic_write_json, read_json_object
from app.schemas.gateway_control import (
    GenerationRunDTO,
    GenerationRunListDTO,
    GeneratorDefinitionCreateRequest,
    GeneratorDefinitionDTO,
    GeneratorDefinitionListDTO,
    GeneratorDefinitionUpdateRequest,
)


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
        now = datetime.now(UTC)
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
        updated = GeneratorDefinitionDTO.model_validate(
            {
                **current.model_dump(mode="json"),
                **values,
                "status": (
                    "ready"
                    if values.get("enabled", current.enabled)
                    else "paused"
                ),
                "status_reason": None,
                "revision": current.revision + 1,
                "updated_at": datetime.now(UTC),
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
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_definition(updated)
        return updated

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
