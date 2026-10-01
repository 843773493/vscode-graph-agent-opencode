from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from fastapi import Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from app.core.identifier import create_prefixed_id
from app.core.trace_middleware import get_request_id
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.control.generators import SessionGeneratorStore
from app.schemas.gateway_control import (
    GenerationOutputDTO,
    GenerationRunDTO,
    GeneratorDefinitionCreateRequest,
    GeneratorDefinitionDTO,
    GeneratorManualRunRequest,
    GeneratorPlacementPreviewRequest,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.core.path_utils import get_gateway_root


logger = logging.getLogger(__name__)


class GeneratorCapabilityMissingError(ValueError):
    pass


class SessionGeneratorCoordinator:
    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        store: SessionGeneratorStore,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._registry = registry
        self._store = store
        self._http_client = http_client
        self._monitor_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self._monitor_tasks:
            raise RuntimeError("会话生成协调器已经启动")
        for definition in self._store.list_definitions().items:
            for run in self._store.list_runs(definition.generator_id).items:
                if run.status in {"running", "reporting"} and run.job_id is not None:
                    self._schedule_monitor(definition, run)
                elif run.status == "dispatching":
                    task = asyncio.create_task(
                        self._resume_dispatch(definition, run),
                        name=f"session-generation-resume:{run.run_id}",
                    )
                    self._monitor_tasks[run.run_id] = task
                    task.add_done_callback(
                        lambda completed, run_id=run.run_id: self._task_finished(
                            run_id,
                            completed,
                        )
                    )

    async def stop(self) -> None:
        tasks = tuple(self._monitor_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._monitor_tasks.clear()

    async def validate_definition_capability(
        self,
        definition: GeneratorDefinitionCreateRequest | GeneratorDefinitionDTO,
        *,
        request_id: str,
    ) -> None:
        target = self._registry.resolve(self._target_workspace_id(definition))
        url, headers = self._target_request(
            target,
            path="/api/v1/session-generations/capabilities",
            request_id=request_id,
        )
        response = await self._http_client.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(
                "目标工作区生成能力响应缺少 data.items 数组: "
                f"workspace_id={target.workspace_id}"
            )
        capability = next(
            (
                item
                for item in items
                if isinstance(item, dict)
                and item.get("type_id") == definition.generator_type.type_id
            ),
            None,
        )
        if capability is None:
            raise GeneratorCapabilityMissingError(
                "目标工作区未注册会话生成器类型: "
                f"workspace_id={target.workspace_id}, "
                f"type_id={definition.generator_type.type_id}"
            )
        versions = capability.get("supported_versions")
        if not isinstance(versions, list) or definition.generator_type.version not in versions:
            raise GeneratorCapabilityMissingError(
                "目标工作区不支持会话生成器版本: "
                f"workspace_id={target.workspace_id}, "
                f"type_id={definition.generator_type.type_id}, "
                f"version={definition.generator_type.version}"
            )
        schema = capability.get("config_schema")
        if not isinstance(schema, dict):
            raise RuntimeError(
                "目标工作区生成能力缺少 config_schema: "
                f"workspace_id={target.workspace_id}, "
                f"type_id={definition.generator_type.type_id}"
            )
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(definition.config)
        except SchemaError as error:
            raise RuntimeError(
                "目标工作区返回了非法生成器 config_schema: "
                f"workspace_id={target.workspace_id}, error={error.message}"
            ) from error
        except ValidationError as error:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            raise ValueError(
                "会话生成器配置不符合目标工作区 provider schema: "
                f"path={location}, error={error.message}"
            ) from error
        locator_kinds: dict[tuple[str, str], str] = {}
        if definition.placement.kind == "session":
            await self._validate_catalog_locator(
                workspace_id=definition.placement.workspace_id,
                node_id=definition.placement.session_id,
                expected_kind="session",
                description="会话挂载位置",
                request_id=request_id,
                known_kinds=locator_kinds,
            )
        elif definition.placement.kind == "session_folder":
            await self._validate_catalog_locator(
                workspace_id=definition.placement.workspace_id,
                node_id=definition.placement.folder_id,
                expected_kind="folder",
                description="会话文件夹挂载位置",
                request_id=request_id,
                known_kinds=locator_kinds,
            )
        strategy_target = definition.session_strategy.target
        if strategy_target is not None:
            await self._validate_catalog_locator(
                workspace_id=strategy_target.workspace_id,
                node_id=strategy_target.session_id,
                expected_kind="session",
                description="生成策略目标会话",
                request_id=request_id,
                known_kinds=locator_kinds,
            )
        context_source = definition.context_source
        if context_source.kind == "live_session":
            await self._validate_catalog_locator(
                workspace_id=context_source.workspace_id,
                node_id=context_source.session_id,
                expected_kind="session",
                description="实时上下文来源会话",
                request_id=request_id,
                known_kinds=locator_kinds,
            )

    async def _validate_catalog_locator(
        self,
        *,
        workspace_id: str | None,
        node_id: str | None,
        expected_kind: str,
        description: str,
        request_id: str,
        known_kinds: dict[tuple[str, str], str],
    ) -> None:
        if workspace_id is None or node_id is None:
            raise ValueError(f"{description}缺少 workspace_id/node_id")
        locator_key = (workspace_id, node_id)
        actual_kind = known_kinds.get(locator_key)
        if actual_kind is None:
            target = self._registry.resolve(workspace_id)
            url, headers = self._target_request(
                target,
                path=f"/api/v1/session-catalog/breadcrumb/{node_id}",
                request_id=request_id,
            )
            response = await self._http_client.get(
                url,
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                raise RuntimeError(
                    "目标工作区 breadcrumb 响应缺少 data.items: "
                    f"workspace_id={workspace_id}, node_id={node_id}"
                )
            terminal = items[-1]
            if not isinstance(terminal, dict):
                raise RuntimeError(
                    "目标工作区 breadcrumb 末节点格式无效: "
                    f"workspace_id={workspace_id}, node_id={node_id}"
                )
            actual_kind = terminal.get("kind")
            if actual_kind not in {"folder", "session"}:
                raise RuntimeError(
                    "目标工作区 breadcrumb 末节点 kind 无效: "
                    f"workspace_id={workspace_id}, node_id={node_id}, "
                    f"kind={actual_kind}"
                )
            terminal_node_id = terminal.get("node_id")
            if terminal_node_id != node_id:
                raise RuntimeError(
                    "目标工作区 breadcrumb 末节点与请求不一致: "
                    f"workspace_id={workspace_id}, requested={node_id}, "
                    f"actual={terminal_node_id}"
                )
            known_kinds[locator_key] = actual_kind
        if actual_kind != expected_kind:
            raise ValueError(
                f"{description}节点类型错误: workspace_id={workspace_id}, "
                f"node_id={node_id}, expected={expected_kind}, actual={actual_kind}"
            )

    async def run_manual(
        self,
        generator_id: str,
        payload: GeneratorManualRunRequest,
        *,
        request: Request,
    ) -> GenerationRunDTO:
        definition = self._store.get_definition(generator_id)
        if not definition.enabled or definition.status != "ready":
            raise ValueError(
                f"会话生成器不可运行: generator_id={generator_id}, "
                f"status={definition.status}, reason={definition.status_reason}"
            )
        now = datetime.now(timezone.utc)
        idempotency_key = payload.idempotency_key or (
            f"{generator_id}:manual:{now.isoformat()}"
        )
        return await self._run(
            definition,
            idempotency_key=idempotency_key,
            trigger_type="manual",
            scheduled_for=now,
            request_id=get_request_id(request),
        )

    async def run_scheduled(
        self,
        generator_id: str,
        *,
        scheduled_for: datetime,
        trigger_type: str,
        request_id: str,
    ) -> GenerationRunDTO:
        definition = self._store.get_definition(generator_id)
        return await self._run(
            definition,
            idempotency_key=(
                f"{generator_id}:{trigger_type}:{scheduled_for.isoformat()}"
            ),
            trigger_type=trigger_type,
            scheduled_for=scheduled_for,
            request_id=request_id,
        )

    async def _run(
        self,
        definition: GeneratorDefinitionDTO,
        *,
        idempotency_key: str,
        trigger_type: str,
        scheduled_for: datetime,
        request_id: str,
    ) -> GenerationRunDTO:
        generator_id = definition.generator_id
        if not definition.enabled or definition.status != "ready":
            raise ValueError(
                f"会话生成器不可运行: generator_id={generator_id}, "
                f"status={definition.status}, reason={definition.status_reason}"
            )
        existing = self._store.find_run_by_idempotency_key(
            generator_id,
            idempotency_key,
        )
        if existing is not None and existing.status in {
            "running",
            "reporting",
            "completed",
        }:
            return existing
        run = existing or self._store.create_run(
            generator_id=generator_id,
            idempotency_key=idempotency_key,
            trigger_type=trigger_type,
            scheduled_for=scheduled_for,
        )
        run = run.model_copy(
            update={
                "status": "dispatching",
                "started_at": datetime.now(timezone.utc),
                "ended_at": None,
                "error": None,
            }
        )
        self._store.write_run(run)
        try:
            target = self._registry.resolve(self._target_workspace_id(definition))
            execution = await self._execute(
                definition,
                run,
                target=target,
                request_id=request_id,
            )
        except Exception as error:
            if definition.policies.mount_missing == "pause" and isinstance(
                error,
                (LookupError, httpx.HTTPStatusError, httpx.RequestError),
            ):
                self._store.set_definition_status(
                    definition.generator_id,
                    status="blocked",
                    reason=f"生成目标不可用: {type(error).__name__}: {error}",
                )
            failed = run.model_copy(
                update={
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                    "ended_at": datetime.now(timezone.utc),
                }
            )
            self._store.write_run(failed)
            raise
        running = run.model_copy(
            update={
                "status": "running",
                "outputs": execution["outputs"],
                "execution_workspace_id": target.workspace_id,
                "message_id": execution["message_id"],
                "job_id": execution["job_id"],
                "report_back_job_id": execution["report_back_job_id"],
            }
        )
        self._store.write_run(running)
        self._schedule_monitor(definition, running)
        return running

    @staticmethod
    def _target_workspace_id(
        definition: GeneratorDefinitionCreateRequest | GeneratorDefinitionDTO,
    ) -> str:
        placement_workspace_id = definition.placement.workspace_id
        if definition.execution_workspace_id != placement_workspace_id:
            raise ValueError(
                "当前版本要求 execution_workspace_id 与会话挂载工作区一致: "
                f"execution_workspace_id={definition.execution_workspace_id}, "
                f"placement_workspace_id={placement_workspace_id}"
            )
        strategy_target = definition.session_strategy.target
        if (
            strategy_target is not None
            and strategy_target.workspace_id != placement_workspace_id
        ):
            raise ValueError(
                "生成策略目标工作区与挂载工作区不一致: "
                f"target={strategy_target.workspace_id}, "
                f"placement={placement_workspace_id}"
            )
        return definition.execution_workspace_id

    async def _execute(
        self,
        definition: GeneratorDefinitionDTO,
        run: GenerationRunDTO,
        *,
        target: WorkspaceTarget,
        request_id: str,
    ) -> dict[str, object]:
        preview = self._store.preview(
            GeneratorPlacementPreviewRequest(
                name=definition.name,
                naming=definition.naming,
                session_title=str(
                    definition.config.get("session_title") or definition.name
                ),
                generated_at=run.scheduled_for,
                placement=definition.placement,
                session_strategy=definition.session_strategy,
            )
        )
        url, headers = self._target_request(
            target,
            path="/api/v1/session-generations/execute",
            request_id=request_id,
        )
        response = await self._http_client.post(
            url,
            headers=headers,
            json={
                "run_id": run.run_id,
                "generator_id": definition.generator_id,
                "idempotency_key": run.idempotency_key,
                "generator_type": definition.generator_type.model_dump(mode="json"),
                "name": definition.name,
                "config": definition.config,
                "placement": definition.placement.model_dump(mode="json"),
                "context_source": definition.context_source.model_dump(mode="json"),
                "session_strategy": definition.session_strategy.model_dump(mode="json"),
                "title": preview.title,
                "navigation_path": preview.path_segments,
                "execution_workspace_id": target.workspace_id,
            },
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise RuntimeError(
                "目标工作区生成执行响应缺少 data 对象: "
                f"workspace_id={target.workspace_id}"
            )
        data = body["data"]
        raw_outputs = data.get("outputs")
        if not isinstance(raw_outputs, list):
            raise RuntimeError(
                "目标工作区生成执行响应缺少 outputs 数组: "
                f"workspace_id={target.workspace_id}"
            )
        message_id = data.get("message_id")
        job_id = data.get("job_id")
        if not isinstance(message_id, str) or not isinstance(job_id, str):
            raise RuntimeError(
                "目标工作区生成执行响应缺少 message_id/job_id: "
                f"workspace_id={target.workspace_id}"
            )
        return {
            "outputs": [
                GenerationOutputDTO.model_validate(item) for item in raw_outputs
            ],
            "message_id": message_id,
            "job_id": job_id,
            "report_back_job_id": (
                data.get("report_back_job_id")
                if isinstance(data.get("report_back_job_id"), str)
                else None
            ),
        }

    async def _resume_dispatch(
        self,
        definition: GeneratorDefinitionDTO,
        run: GenerationRunDTO,
    ) -> None:
        try:
            await self._run(
                definition,
                idempotency_key=run.idempotency_key,
                trigger_type=run.trigger_type,
                scheduled_for=run.scheduled_for,
                request_id=create_prefixed_id("req"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "恢复会话生成分派失败: run_id=%s",
                run.run_id,
                exc_info=(type(error), error, error.__traceback__),
            )
            raise

    def _schedule_monitor(
        self,
        definition: GeneratorDefinitionDTO,
        run: GenerationRunDTO,
    ) -> None:
        existing = self._monitor_tasks.get(run.run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._monitor_run(definition, run),
            name=f"session-generation-monitor:{run.run_id}",
        )
        self._monitor_tasks[run.run_id] = task
        task.add_done_callback(
            lambda completed, run_id=run.run_id: self._task_finished(
                run_id,
                completed,
            )
        )

    def _task_finished(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._monitor_tasks.get(run_id) is task:
            self._monitor_tasks.pop(run_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "会话生成后台任务异常结束: run_id=%s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _monitor_run(
        self,
        definition: GeneratorDefinitionDTO,
        run: GenerationRunDTO,
    ) -> None:
        if run.job_id is None:
            raise RuntimeError(f"running 生成记录缺少 job_id: {run.run_id}")
        try:
            target = self._registry.resolve(
                run.execution_workspace_id or self._target_workspace_id(definition)
            )
            await self._complete_generation(target=target, run=run)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            current = self._store.get_run(run.generator_id, run.run_id)
            self._store.write_run(
                current.model_copy(
                    update={
                        "status": "failed",
                        "error": (
                            "监控生成 Job 失败: "
                            f"{type(error).__name__}: {error}"
                        ),
                        "ended_at": datetime.now(timezone.utc),
                    }
                )
            )
            raise

    async def _complete_generation(
        self,
        *,
        target: WorkspaceTarget,
        run: GenerationRunDTO,
    ) -> None:
        generation_status = await self._wait_for_generation(
            target=target,
            run=run,
        )
        current = self._store.get_run(run.generator_id, run.run_id)
        self._store.write_run(
            current.model_copy(
                update={
                    "status": generation_status["status"],
                    "report_back_job_id": generation_status["report_back_job_id"],
                    "error": generation_status["error"],
                    "ended_at": datetime.now(timezone.utc),
                }
            )
        )

    async def _wait_for_generation(
        self,
        *,
        target: WorkspaceTarget,
        run: GenerationRunDTO,
    ) -> dict[str, str | None]:
        url, headers = self._target_request(
            target,
            path="/api/v1/session-generations/status",
            request_id=create_prefixed_id("req"),
        )
        while True:
            response = await self._http_client.get(
                url,
                headers=headers,
                params={
                    "generator_id": run.generator_id,
                    "idempotency_key": run.idempotency_key,
                },
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            status = data.get("status") if isinstance(data, dict) else None
            if not isinstance(status, str):
                raise RuntimeError(
                    "目标工作区生成状态响应缺少 data.status: "
                    f"workspace_id={target.workspace_id}, run_id={run.run_id}"
                )
            report_back_job_id = data.get("report_back_job_id")
            if report_back_job_id is not None and not isinstance(
                report_back_job_id,
                str,
            ):
                raise RuntimeError(
                    "目标工作区生成状态响应包含非法 report_back_job_id: "
                    f"workspace_id={target.workspace_id}, run_id={run.run_id}"
                )
            if status in {"completed", "failed"}:
                return {
                    "status": status,
                    "report_back_job_id": report_back_job_id,
                    "error": (
                        str(data.get("error") or "会话生成运行失败")
                        if status == "failed"
                        else None
                    ),
                }
            if status not in {"queued", "reporting"}:
                raise RuntimeError(
                    "目标工作区生成运行处于非法状态: "
                    f"workspace_id={target.workspace_id}, run_id={run.run_id}, "
                    f"status={status}"
                )
            current = self._store.get_run(run.generator_id, run.run_id)
            if current.report_back_job_id != report_back_job_id:
                self._store.write_run(
                    current.model_copy(
                        update={"report_back_job_id": report_back_job_id}
                    )
                )
            await asyncio.sleep(1)

    def _target_request(
        self,
        target: WorkspaceTarget,
        *,
        path: str,
        request_id: str,
    ) -> tuple[str, dict[str, str]]:
        if target.connection_kind == "remote_gateway":
            if (
                target.remote_gateway_connection_id is None
                or target.remote_workspace_id is None
            ):
                raise RuntimeError(
                    f"远程工作区投影缺少路由信息: {target.workspace_id}"
                )
            credential = FederationCredentialStore(
                storage_path=get_gateway_root() / "credentials" / "federation.json"
            ).get(target.remote_gateway_connection_id)
            return (
                f"{self._registry.remote_gateway_url(target.remote_gateway_connection_id)}"
                f"{path}",
                {
                    "X-BoxTeam-Workspace-Id": target.remote_workspace_id,
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
        return (
            f"{target.backend_url.rstrip('/')}{path}",
            {"X-Local-Token": LOCAL_TOKEN, "X-Request-ID": request_id},
        )
