"""显式退休旧 Gateway 生成器文件，通过目标 API 单次移交；不参与生产调度。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
import portalocker
from pydantic import BaseModel, ConfigDict, Field

from app.core.atomic_fs import atomic_write_bytes, fsync_directory
from app.core.resource_uri import resource_display_uri
from app.domain.itemized.hashing import sha256_jcs
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.internal_v2.generator_import import SessionGeneratorImportRequest
from app.schemas.internal_v2.legacy_generators import GeneratorDefinitionDTO
from app.schemas.internal_v2.persistent_task import SessionGenerationActionDTO

logger = logging.getLogger(__name__)


class GeneratorImportSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    request: SessionGeneratorImportRequest
    workspace_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    files: dict[str, str]

    def result(self, *, imported: bool = True) -> dict[str, str]:
        return {"task_id": self.request.task_id, "workspace_id": self.workspace_id,
                "source_hash": self.request.source_hash, "status": "imported" if imported else "planned"}


@dataclass(frozen=True, slots=True)
class GeneratorImportInput:
    source: Path
    task_id: str
    workspace_id: str = ""
    definition: GeneratorDefinitionDTO | None = None
    snapshot: GeneratorImportSnapshot | None = None
    remaining_sources: tuple[Path, ...] = ()
    received: bool = False
    error: Exception | None = None

    @property
    def settled(self) -> bool:
        return self.error is None and self.snapshot is not None and self.received and not self.remaining_sources


def _source(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("旧生成器迁移拒绝符号链接")
    return path.read_bytes()


def _definition(path: Path) -> GeneratorDefinitionDTO:
    definition = GeneratorDefinitionDTO.model_validate_json(_source(path))
    generator_id = definition.generator_id
    if not generator_id.startswith("gen_") or "/" in generator_id or "\\" in generator_id:
        raise ValueError("旧生成器身份非法")
    if generator_id != path.stem:
        raise ValueError("旧生成器文件与定义身份不匹配")
    return definition


def _source_hash(files: dict[str, str], workspace_ids: dict[str, str], owner: str,
                 gateway_id: str, created_from_uri: str | None) -> str:
    return sha256_jcs({"files": files, "workspace_ids": workspace_ids, "owner": owner,
                       "gateway_id": gateway_id, "created_from_uri": created_from_uri}).rsplit(":", 1)[-1]


def _bundle(root: Path, definition: GeneratorDefinitionDTO, registry: GatewayWorkspaceRegistry,
            gateway_id: str, *, local_gateway_id: str) -> tuple[SessionGeneratorImportRequest, dict[Path, bytes]]:
    generator_id = definition.generator_id
    sources = {root / "generators" / f"{generator_id}.json": _source(root / "generators" / f"{generator_id}.json")}
    run_dir = root / "generation-runs" / generator_id
    if run_dir.is_dir():
        sources.update({path: _source(path) for path in sorted(run_dir.glob("*.json"))})
    schedule_path = root / "generator-schedules" / f"{generator_id}.json"
    if schedule_path.exists():
        sources[schedule_path] = _source(schedule_path)
    mappings: dict[str, str] = {}

    def workspace(value: str) -> str:
        target = registry.resolve(value)
        if target.backend_workspace_id is None:
            raise ValueError(f"旧生成器目标缺少后端稳定身份: {value}")
        mappings[value] = target.backend_workspace_id
        return target.backend_workspace_id

    owner = workspace(definition.execution_workspace_id)
    placement = definition.placement.model_dump(mode="json")
    placement["workspace_id"] = workspace(definition.placement.workspace_id)
    context = definition.context_source.model_dump(mode="json")
    if definition.context_source.workspace_id is not None:
        context["workspace_id"] = workspace(definition.context_source.workspace_id)
    strategy = definition.session_strategy.model_dump(mode="json")
    if definition.session_strategy.target is not None:
        strategy["target"]["workspace_id"] = workspace(definition.session_strategy.target.workspace_id)
    created_from = definition.created_from
    created_uri = None
    if created_from:
        origin_target = registry.resolve(created_from.workspace_id)
        origin_gateway = local_gateway_id
        if origin_target.connection_kind == "remote_gateway":
            connection_id = origin_target.remote_gateway_connection_id
            assert connection_id is not None
            origin_gateway = registry.remote_gateway_connection(connection_id).remote_gateway_id
            if not origin_gateway:
                raise ValueError("旧生成器创建来源缺少 Gateway 身份")
        created_uri = resource_display_uri(scope="workspace", scope_id=workspace(created_from.workspace_id), kind="session", tail_segments=(created_from.session_id,), gateway_authority=origin_gateway)
    configuration = {
        "name": definition.name,
        "trigger": {"type_id": f"builtin.{definition.trigger.type}", "version": "1",
                    "interval_seconds": definition.trigger.interval_seconds, "expression": definition.trigger.expression,
                    "timezone": definition.trigger.timezone},
        "condition": {"type_id": "builtin.always", "version": "1"},
        "context": {"type_id": "builtin.literal", "version": "1", "content": ""},
        "action": {"type_id": "builtin.session_generation", "version": "1", "generation": {
            "generator_type": definition.generator_type.model_dump(mode="json"), "config": definition.config,
            "placement": placement, "context_source": context, "session_strategy": strategy,
            "title_template": definition.naming.title_template, "path_template": definition.naming.path_template,
            "created_from_uri": created_uri, "gateway_id": gateway_id, "ui_policy": definition.ui_policy.model_dump(mode="json"),
            "mount_missing": definition.policies.mount_missing, "delete_outputs": definition.policies.delete_outputs,
        }},
        "policies": {"completion": "continuous", "misfire": definition.policies.misfire, "match_mode": "each_match"},
    }
    schedule = json.loads(sources[schedule_path]) if schedule_path in sources else None
    if schedule is not None and schedule["definition_revision"] != definition.revision:
        schedule = None
    source_hash = _source_hash({path.relative_to(root).as_posix(): hashlib.sha256(raw).hexdigest() for path, raw in sources.items()},
                               mappings, owner, gateway_id, created_uri)
    return SessionGeneratorImportRequest.model_validate({
        "task_id": generator_id, "source_hash": source_hash, "configuration": configuration,
        "revision": definition.revision, "lifecycle": "needs_attention" if definition.status == "blocked" else "active" if definition.enabled else "paused",
        "reason": definition.status_reason, "created_at": definition.created_at, "updated_at": definition.updated_at,
        "next_run_at": schedule["next_run_at"] if schedule else None,
        "workspace_ids": mappings, "runs": [json.loads(raw) for path, raw in sources.items() if path.parent == run_dir],
    }), sources


def _verified_sources(root: Path, backup: Path, sources: dict[str, str]) -> list[Path]:
    remaining: list[Path] = []
    for relative, digest in sources.items():
        source, archived = root / relative, backup / relative
        if not source.resolve().is_relative_to(root) or not archived.resolve().is_relative_to(backup):
            raise ValueError("生成器迁移快照路径越界")
        if hashlib.sha256(_source(archived)).hexdigest() != digest:
            raise RuntimeError("生成器迁移备份缺失或内容冲突")
        if source.exists():
            if hashlib.sha256(_source(source)).hexdigest() != digest:
                raise RuntimeError("旧生成器在迁移期间变化，停止退休源文件")
            remaining.append(source)
    return remaining


def _verified_snapshot(root: Path, backup: Path, snapshot: GeneratorImportSnapshot) -> tuple[tuple[Path, ...], bool]:
    bundle = snapshot.request
    action = bundle.configuration.action
    if not isinstance(action, SessionGenerationActionDTO):
        raise ValueError("旧生成器导入只能安装会话动作")  # noqa: TRY004 - 非法迁移输入沿用显式入口的 ValueError 合同。
    if backup.name != bundle.source_hash or snapshot.owner != bundle.workspace_ids.get(snapshot.workspace_id):
        raise ValueError("生成器迁移快照身份或映射冲突")
    generation = action.generation
    if generation.gateway_id is None:
        raise ValueError("生成器迁移快照缺少冻结 Gateway 身份")
    if bundle.source_hash != _source_hash(snapshot.files, bundle.workspace_ids, snapshot.owner,
                                          generation.gateway_id, generation.created_from_uri):
        raise ValueError("生成器迁移 source_hash 与冻结输入不匹配")
    definition_path = f"generators/{bundle.task_id}.json"
    if definition_path not in snapshot.files:
        raise ValueError("生成器迁移快照缺少原定义")
    remaining = tuple(_verified_sources(root, backup, snapshot.files))
    definition = _definition(backup / definition_path)
    if definition.execution_workspace_id != snapshot.workspace_id:
        raise ValueError("生成器迁移快照与原定义工作区冲突")
    receipt = backup / "receipt.json"
    received = receipt.exists()
    if received and json.loads(_source(receipt)) != snapshot.result():
        raise RuntimeError("旧生成器迁移回执与快照冲突")
    if not received and len(remaining) != len(snapshot.files):
        raise RuntimeError("旧生成器尚无导入回执但源文件已缺失")
    return remaining, received


def read_session_generator_inputs(root: Path) -> tuple[GeneratorImportInput, ...]:
    """显式导入与身份预检共用原文件、冻结快照及回执的唯一只读校验。"""
    inputs: list[GeneratorImportInput] = []
    pending = root / "migration-backups/session-generators"
    for backup in sorted(pending.iterdir()) if pending.exists() else ():
        snapshot = None
        try:
            if backup.is_symlink() or not backup.is_dir():
                raise ValueError("生成器迁移备份必须是目录且不能为符号链接")
            snapshot = GeneratorImportSnapshot.model_validate_json(_source(backup / "snapshot.json"))
            remaining, received = _verified_snapshot(root, backup, snapshot)
            inputs.append(GeneratorImportInput(backup, snapshot.request.task_id, snapshot.workspace_id,
                                               snapshot=snapshot, remaining_sources=remaining, received=received))
        except (LookupError, ValueError, RuntimeError, OSError, TypeError) as error:
            inputs.append(GeneratorImportInput(backup, snapshot.request.task_id if snapshot else backup.name,
                                               snapshot.workspace_id if snapshot else "", snapshot=snapshot, error=error))
    known = {item.task_id for item in inputs if item.snapshot is not None}
    for path in sorted((root / "generators").glob("*.json")):
        if path.stem in known:
            continue
        try:
            definition = _definition(path)
            inputs.append(GeneratorImportInput(path, definition.generator_id, definition.execution_workspace_id, definition))
        except (LookupError, ValueError, RuntimeError, OSError, TypeError) as error:
            inputs.append(GeneratorImportInput(path, path.stem, error=error))
    return tuple(inputs)


async def _install(root: Path, backup: Path, snapshot: GeneratorImportSnapshot, *, registry: GatewayWorkspaceRegistry,
                   api: GatewayResourceCatalogService, http_client: httpx.AsyncClient, request_id: str) -> dict[str, str]:
    bundle = snapshot.request
    result = snapshot.result()
    receipt = backup / "receipt.json"
    # 先核验整个冻结快照；损坏或变化的输入不能先在目标创建可调度任务。
    _, received = _verified_snapshot(root, backup, snapshot)
    if not received:
        target = registry.resolve(snapshot.workspace_id)
        if target.backend_workspace_id != snapshot.owner:
            raise ValueError("旧生成器的路由已改绑，拒绝迁入其他工作区")
        url, headers = api.target_request(target, "persistent-tasks/imports/session-generators", request_id=request_id)
        response = await http_client.post(url, headers=headers, json=bundle.model_dump(mode="json"), timeout=60)
        response.raise_for_status()
        if response.json()["data"]["task_id"] != bundle.task_id:
            raise RuntimeError("旧生成器导入回执身份不匹配")
        atomic_write_bytes(receipt, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    # 目标接受期间源文件仍可能变化；全量重验后才退休，回执支持断点重试。
    for source in _verified_snapshot(root, backup, snapshot)[0]:
        source.unlink()
        fsync_directory(source.parent)
    return result


async def _import_all(*, root: Path, registry: GatewayWorkspaceRegistry,
                      api: GatewayResourceCatalogService, http_client: httpx.AsyncClient,
                      gateway_id: str, request_id: str, preview: bool) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pending = root / "migration-backups/session-generators"
    inputs = read_session_generator_inputs(root)
    unsealed = {item.source for item in inputs if isinstance(item.error, FileNotFoundError)
                and item.error.filename == str(item.source / "snapshot.json")}
    prepared: dict[Path, tuple[GeneratorImportSnapshot, dict[Path, bytes]]] = {}
    errors = {item.source: item.error for item in inputs if item.error is not None and item.source not in unsealed}
    # 先只读准备整批原件；任何半备份对账失败，都不能先发布其它未结算项。
    for item in inputs:
        if item.definition is None:
            continue
        try:
            target = registry.resolve(item.workspace_id)
            target_gateway = gateway_id
            if target.connection_kind == "remote_gateway":
                connection_id = target.remote_gateway_connection_id
                assert connection_id is not None
                target_gateway = registry.remote_gateway_connection(connection_id).remote_gateway_id
                if not target_gateway:
                    raise ValueError("旧生成器远端 Gateway 缺少稳定身份")
            bundle, sources = _bundle(root, item.definition, registry, target_gateway, local_gateway_id=gateway_id)
            snapshot = GeneratorImportSnapshot.model_validate({"request": bundle, "workspace_id": item.workspace_id,
                        "owner": target.backend_workspace_id,
                        "files": {source.relative_to(root).as_posix(): hashlib.sha256(raw).hexdigest() for source, raw in sources.items()}})
            backup = pending / bundle.source_hash
            for archived in backup.rglob("*"):
                if archived.is_dir() and not archived.is_symlink():
                    continue
                source = root / archived.relative_to(backup)
                if source not in sources or _source(archived) != sources[source]:
                    raise RuntimeError("旧生成器备份内容冲突")
            prepared[item.source] = snapshot, sources
        except (LookupError, ValueError, RuntimeError, OSError, TypeError) as error:
            errors[item.source] = error
    matched = {pending / snapshot.request.source_hash for snapshot, _ in prepared.values()}
    blocked: ValueError | None = None
    if any(item.error is not None and item.snapshot is None and item.source.parent == pending
           and item.source not in unsealed for item in inputs):
        blocked = ValueError("存在无法确认归属的冻结备份，停止本批生成器导入")
    elif unsealed and (errors or not unsealed <= matched):
        blocked = ValueError("半备份未全部匹配相同 source_hash 的完整原件，或已有归档冲突，停止本批生成器导入")
    for item in inputs:
        if item.source in unsealed and blocked is None:
            continue
        generator_id, workspace_id = item.task_id, item.workspace_id
        stage = "安装" if item.snapshot is not None else "准备"
        try:
            if item.error is not None:
                raise item.error
            if item.source in errors:
                raise errors[item.source]
            if blocked is not None and not item.settled:
                raise blocked
            if item.snapshot is not None:
                result = item.snapshot.result(imported=item.settled)
                if not preview and not item.settled:
                    result = await _install(root, item.source, item.snapshot, registry=registry, api=api,
                                            http_client=http_client, request_id=request_id)
                results.append(result)
                continue
            snapshot, sources = prepared[item.source]
            backup = pending / snapshot.request.source_hash
            if not preview:
                for source, raw in sources.items():
                    archived = backup / source.relative_to(root)
                    atomic_write_bytes(archived, raw)
                atomic_write_bytes(backup / "snapshot.json", (snapshot.model_dump_json(indent=2) + "\n").encode())
                stage = "安装"
                result = await _install(root, backup, snapshot, registry=registry, api=api, http_client=http_client, request_id=request_id)
            else:
                result = snapshot.result(imported=False)
            results.append(result)
        except (LookupError, ValueError, RuntimeError, OSError, TypeError, httpx.HTTPError) as error:
            logger.exception("旧生成器迁移%s失败: task=%s request_id=%s", stage, generator_id, request_id)
            results.append({"task_id": generator_id, "workspace_id": workspace_id, "status": "failed",
                            "error": f"生成器迁移{stage}失败（{type(error).__name__}），详见原始诊断日志；request_id={request_id}"})
    return results


async def import_session_generators(*, root: Path, registry: GatewayWorkspaceRegistry,
                                    api: GatewayResourceCatalogService, http_client: httpx.AsyncClient,
                                    gateway_id: str, request_id: str, preview: bool) -> list[dict[str, str]]:
    if preview:
        return await _import_all(root=root, registry=registry, api=api, http_client=http_client,
                                 gateway_id=gateway_id, request_id=request_id, preview=True)
    lock = root / "migration-backups/session-generators.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(lock, timeout=0):
            return await _import_all(root=root, registry=registry, api=api, http_client=http_client,
                                     gateway_id=gateway_id, request_id=request_id, preview=False)
    except portalocker.AlreadyLocked as error:
        raise ValueError("已有生成器迁移正在进行，请等待该请求完成") from error
