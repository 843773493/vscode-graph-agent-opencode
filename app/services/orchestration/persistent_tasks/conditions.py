"""任务条件判断；脚本不获得任务生命周期或动作写入端口。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import rfc8785
from jsonpointer import resolve_pointer
from pydantic import JsonValue, ValidationError

from app.runtime.native_command import CommandOutputLimitError, run_native_command
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskConfigurationDTO,
    PersistentTaskRunDTO,
    TaskDecisionDTO,
)

logger = logging.getLogger(__name__)


class TaskCheckError(ValueError):
    pass


class ModelCheckResponse(TaskDecisionDTO):
    run_id: str
    definition_revision: int


def model_decision(content: str, run: PersistentTaskRunDTO) -> TaskDecisionDTO:
    try:
        response = ModelCheckResponse.model_validate_json(content, strict=True)
    except ValidationError as error:
        logger.error("模型判断输出无效 task=%s run=%s: %r", run.task_id, run.run_id, content)
        raise TaskCheckError("模型输出不符合判断协议，详见原始诊断日志") from error
    if (response.run_id, response.definition_revision) != (run.run_id, run.definition_revision):
        raise TaskCheckError("模型判断结果与本次运行/定义版本不匹配")
    return TaskDecisionDTO(decision=response.decision, reason=response.reason, result=response.result)


def condition_input(configuration: PersistentTaskConfigurationDTO, run: PersistentTaskRunDTO,
                    *, workspace_id: str) -> dict[str, JsonValue]:
    # 冻结输入从原定义版本与触发事实构造，不复制另一份可变上下文历史。
    trigger: dict[str, JsonValue] = {"key": run.trigger_key, "type": run.trigger_type,
                                     "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None}
    if run.trigger_payload is not None:
        trigger["payload"] = run.trigger_payload
    return {
        "version": "1", "workspace_id": workspace_id, "task_id": run.task_id, "run_id": run.run_id,
        "definition_revision": run.definition_revision, "task_revision": run.task_revision,
        "trigger": trigger,
        "check_only": run.check_only, "context": configuration.context.model_dump(mode="json"),
    }


async def evaluate_condition(configuration: PersistentTaskConfigurationDTO, run: PersistentTaskRunDTO,
                             *, workspace: Path, workspace_id: str) -> TaskDecisionDTO:
    condition = configuration.condition
    if condition.type_id == "builtin.always":
        return TaskDecisionDTO(decision="proceed", reason="无附加条件，始终通过")
    input_value = condition_input(configuration, run, workspace_id=workspace_id)
    if condition.type_id == "builtin.rule":
        rule = condition.rule
        assert rule is not None
        missing = object()
        actual = resolve_pointer(input_value, rule.pointer, default=missing)
        found = actual is not missing
        value = cast(JsonValue, actual) if found else None
        # JSON相等保留数组顺序并区分true和1，不使用Python的宽松bool/number相等。
        matches = found and rfc8785.dumps(value) == rfc8785.dumps(rule.expected)
        return TaskDecisionDTO(
            decision="proceed" if matches else "wait",
            reason="规则匹配" if matches else ("规则未匹配" if found else "规则输入字段不存在"),
            result={"pointer": rule.pointer, "found": found, "actual": value, "expected": rule.expected},
        )
    script = condition.script
    assert script is not None
    try:
        result = await run_native_command(
            script.command, workspace=workspace, stdin=json.dumps(input_value, ensure_ascii=False).encode(),
            timeout_seconds=script.timeout_seconds, memory_limit_mb=script.memory_limit_mb,
            output_limit_bytes=script.output_limit_bytes,
        )
    except OSError as error:
        logger.exception("持久任务脚本启动失败 task=%s run=%s", run.task_id, run.run_id)
        raise TaskCheckError(f"脚本启动失败: {type(error).__name__}, errno={error.errno}") from error
    except (CommandOutputLimitError, TimeoutError) as error:
        raise TaskCheckError("脚本输出超限" if isinstance(error, CommandOutputLimitError) else "脚本监督器超过执行期限") from error
    if result.stderr:
        # 原生诊断只进原始服务日志；普通运行记录保留身份、退出码与判断事实。
        logger.warning("持久任务脚本诊断 task=%s run=%s: %s", run.task_id, run.run_id, result.stderr.decode("utf-8", errors="replace"))
    if result.returncode != 0:
        reason = "脚本执行超时" if result.returncode == 124 else "脚本执行失败"
        raise TaskCheckError(f"{reason}: exit_code={result.returncode}")
    try:
        return TaskDecisionDTO.model_validate_json(result.stdout)
    except ValidationError as error:
        logger.error("持久任务脚本输出无效 task=%s run=%s: %r", run.task_id, run.run_id, result.stdout)
        raise TaskCheckError("脚本输出不符合判断协议，详见原始诊断日志") from error
