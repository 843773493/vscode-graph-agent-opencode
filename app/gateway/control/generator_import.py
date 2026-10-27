"""显式退休旧 Gateway 生成器文件，通过目标 API 单次移交；不参与生产调度。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx
import portalocker

from app.core.atomic_fs import atomic_write_bytes, fsync_directory
from app.core.resource_uri import resource_display_uri
from app.domain.itemized.hashing import sha256_jcs
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.internal_v2.generator_import import SessionGeneratorImportRequest
from app.schemas.internal_v2.legacy_generators import GeneratorDefinitionDTO

logger = logging.getLogger(__name__)

def _source(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("旧生成器迁移拒绝符号链接")
    return path.read_bytes()


def _bundle(root: Path, definition: GeneratorDefinitionDTO, registry: GatewayWorkspaceRegistry,
            gateway_id: str, *, local_gateway_id: str) -> tuple[SessionGeneratorImportRequest, dict[Path, bytes]]:
    generator_id = definition.generator_id
    if not generator_id.startswith("gen_") or "/" in generator_id or "\\" in generator_id:
        raise ValueError("旧生成器身份非法")
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
    source_hash = sha256_jcs({"files": {path.relative_to(root).as_posix(): hashlib.sha256(raw).hexdigest() for path, raw in sources.items()},
                              "workspace_ids": mappings, "owner": owner, "gateway_id": gateway_id, "created_from_uri": created_uri}).rsplit(":", 1)[-1]
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


async def _install(root: Path, backup: Path, snapshot: dict, *, registry: GatewayWorkspaceRegistry,
                   api: GatewayResourceCatalogService, http_client: httpx.AsyncClient, request_id: str) -> dict[str, str]:
    bundle = SessionGeneratorImportRequest.model_validate(snapshot["request"])
    result = {"task_id": bundle.task_id, "workspace_id": snapshot["workspace_id"], "source_hash": bundle.source_hash, "status": "imported"}
    receipt = backup / "receipt.json"
    # 先核验整个冻结快照；损坏或变化的输入不能先在目标创建可调度任务。
    _verified_sources(root, backup, snapshot["files"])
    if not receipt.exists():
        target = registry.resolve(snapshot["workspace_id"])
        if target.backend_workspace_id != snapshot["owner"]:
            raise ValueError("旧生成器的路由已改绑，拒绝迁入其他工作区")
        url, headers = api.target_request(target, "persistent-tasks/imports/session-generators", request_id=request_id)
        response = await http_client.post(url, headers=headers, json=bundle.model_dump(mode="json"), timeout=60)
        response.raise_for_status()
        if response.json()["data"]["task_id"] != bundle.task_id:
            raise RuntimeError("旧生成器导入回执身份不匹配")
        atomic_write_bytes(receipt, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    elif json.loads(_source(receipt)) != result:
        raise RuntimeError("旧生成器迁移回执与快照冲突")
    # 目标接受期间源文件仍可能变化；全量重验后才退休，回执支持断点重试。
    for source in _verified_sources(root, backup, snapshot["files"]):
        source.unlink()
        fsync_directory(source.parent)
    return result


async def _import_all(*, root: Path, registry: GatewayWorkspaceRegistry,
                      api: GatewayResourceCatalogService, http_client: httpx.AsyncClient,
                      gateway_id: str, request_id: str, preview: bool) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pending = root / "migration-backups/session-generators"
    snapshots = {path.parent: json.loads(_source(path)) for path in sorted(pending.glob("*/snapshot.json"))}
    known = {value["request"]["task_id"] for value in snapshots.values()}
    for path in sorted((root / "generators").glob("*.json")):
        if path.stem in known:
            continue
        generator_id, workspace_id = path.stem, ""
        try:
            definition = GeneratorDefinitionDTO.model_validate_json(_source(path))
            generator_id, workspace_id = definition.generator_id, definition.execution_workspace_id
            if generator_id != path.stem:
                raise ValueError("旧生成器文件与定义身份不匹配")
            target = registry.resolve(workspace_id)
            target_gateway = gateway_id
            if target.connection_kind == "remote_gateway":
                connection_id = target.remote_gateway_connection_id
                assert connection_id is not None
                target_gateway = registry.remote_gateway_connection(connection_id).remote_gateway_id
                if not target_gateway:
                    raise ValueError("旧生成器远端 Gateway 缺少稳定身份")
            bundle, sources = _bundle(root, definition, registry, target_gateway, local_gateway_id=gateway_id)
            snapshot = {"request": bundle.model_dump(mode="json"), "workspace_id": workspace_id,
                        "owner": target.backend_workspace_id,
                        "files": {source.relative_to(root).as_posix(): hashlib.sha256(raw).hexdigest() for source, raw in sources.items()}}
            backup = pending / bundle.source_hash
            if not preview:
                for source, raw in sources.items():
                    archived = backup / source.relative_to(root)
                    if archived.exists() and _source(archived) != raw:
                        raise RuntimeError("旧生成器备份内容冲突")
                    atomic_write_bytes(archived, raw)
                atomic_write_bytes(backup / "snapshot.json", (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode())
            snapshots[backup] = snapshot
        except (LookupError, ValueError, RuntimeError, OSError) as error:
            logger.exception("旧生成器迁移准备失败: task=%s request_id=%s", generator_id, request_id)
            results.append({"task_id": generator_id, "workspace_id": workspace_id, "status": "failed",
                            "error": f"生成器迁移准备失败（{type(error).__name__}），详见原始诊断日志；request_id={request_id}"})
    for backup, snapshot in snapshots.items():
        try:
            result = {"task_id": snapshot["request"]["task_id"], "workspace_id": snapshot["workspace_id"],
                      "source_hash": snapshot["request"]["source_hash"], "status": "imported" if (backup / "receipt.json").exists() else "planned"}
            if not preview:
                result = await _install(root, backup, snapshot, registry=registry, api=api, http_client=http_client, request_id=request_id)
            results.append(result)
        except (LookupError, ValueError, RuntimeError, OSError, httpx.HTTPError) as error:
            logger.exception("旧生成器迁移安装失败: task=%s request_id=%s", snapshot["request"]["task_id"], request_id)
            results.append({"task_id": snapshot["request"]["task_id"], "workspace_id": snapshot["workspace_id"], "status": "failed",
                            "error": f"生成器迁移安装失败（{type(error).__name__}），详见原始诊断日志；request_id={request_id}"})
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
