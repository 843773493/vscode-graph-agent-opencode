"""任务条件判断；脚本不获得任务生命周期或动作写入端口。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

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


async def evaluate_condition(configuration: PersistentTaskConfigurationDTO, run: PersistentTaskRunDTO,
                             *, workspace: Path, workspace_id: str) -> TaskDecisionDTO:
    condition = configuration.condition
    if condition.type_id == "builtin.always":
        return TaskDecisionDTO(decision="proceed", reason="无附加条件，始终通过")
    script = condition.script
    assert script is not None
    # 冻结输入从原定义版本与触发事实构造，不复制另一份可变上下文历史。
    trigger: dict[str, JsonValue] = {"key": run.trigger_key, "type": run.trigger_type,
                                     "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None}
    if run.trigger_payload is not None:
        trigger["payload"] = run.trigger_payload
    input_value = {
        "version": "1", "workspace_id": workspace_id, "task_id": run.task_id, "run_id": run.run_id,
        "definition_revision": run.definition_revision, "task_revision": run.task_revision,
        "trigger": trigger,
        "check_only": run.check_only, "context": configuration.context.model_dump(mode="json"),
    }
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
