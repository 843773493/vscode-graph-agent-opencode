import { useEffect, useState } from "react";
import type {
  SessionGoal,
  SessionGoalUpdateRequest,
} from "../types/backend";
import {
  GOAL_STATUS_LABELS,
  goalCanResume,
  goalEditStatus,
  restartCompletedGoalPayload,
} from "../state/sessionGoal";
import WarmActionDialog from "./WarmActionDialog";
import { useWarmConfirm } from "./WarmConfirmProvider";

function formatTokens(tokens: number): string {
  return new Intl.NumberFormat("zh-CN").format(tokens);
}

function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours > 0) {
    return `${hours} 小时 ${minutes} 分`;
  }
  if (minutes > 0) {
    return `${minutes} 分 ${remainder} 秒`;
  }
  return `${remainder} 秒`;
}

type GoalEditorMode = "create" | "edit" | null;

export default function GoalManagerCard({
  sessionId,
  goal,
  loading,
  error,
  onRefresh,
  onUpdate,
  onClear,
}: {
  sessionId: string;
  goal: SessionGoal | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<SessionGoal | null>;
  onUpdate: (payload: SessionGoalUpdateRequest) => Promise<SessionGoal>;
  onClear: () => Promise<void>;
}) {
  const confirm = useWarmConfirm();
  const [editorMode, setEditorMode] = useState<GoalEditorMode>(null);
  const [operationError, setOperationError] = useState<string | null>(null);

  useEffect(() => {
    setEditorMode(null);
    setOperationError(null);
  }, [sessionId]);

  const run = (operation: () => Promise<unknown>) => {
    setOperationError(null);
    void operation().catch((operationFailure: unknown) => {
      setOperationError(
        operationFailure instanceof Error
          ? operationFailure.message
          : String(operationFailure),
      );
    });
  };

  const clear = async () => {
    if (!goal) {
      throw new Error("当前会话没有 Goal");
    }
    const confirmed = await confirm({
      title: "清除当前 Goal？",
      message: `将清除“${goal.objective}”的 Goal 状态。`,
      confirmText: "清除 Goal",
      danger: true,
    });
    if (confirmed) {
      await onClear();
    }
  };

  const canManage = Boolean(sessionId) && !loading;
  return (
    <section className="goal-manager" aria-label="当前 Goal">
      <div className="goal-manager-heading">
        <div className="goal-manager-title-row">
          <span className="codicon codicon-target" aria-hidden="true" />
          <strong>当前 Goal</strong>
          {goal ? (
            <span className={`goal-manager-status goal-manager-status-${goal.status}`}>
              {GOAL_STATUS_LABELS[goal.status]}
            </span>
          ) : null}
        </div>
        <button
          type="button"
          className="resource-icon-button"
          disabled={!sessionId || loading}
          title="刷新 Goal"
          aria-label="刷新 Goal"
          onClick={() => run(onRefresh)}
        >
          <span
            className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`}
            aria-hidden="true"
          />
        </button>
      </div>

      {goal ? (
        <>
          <p className="goal-manager-objective">{goal.objective}</p>
          <div className="goal-manager-metrics">
            <span>耗时 {formatDuration(goal.time_used_seconds)}</span>
            <span>
              Token {formatTokens(goal.tokens_used)}
              {goal.token_budget === null
                ? ""
                : ` / ${formatTokens(goal.token_budget)}`}
            </span>
          </div>
          <div className="goal-manager-actions">
            {goal.status === "active" ? (
              <button
                type="button"
                disabled={!canManage}
                onClick={() => run(() => onUpdate({ status: "paused" }))}
              >
                暂停
              </button>
            ) : goalCanResume(goal.status) ? (
              <button
                type="button"
                disabled={!canManage}
                onClick={() => run(() => onUpdate({ status: "active" }))}
              >
                继续
              </button>
            ) : goal.status === "complete" ? (
              <button
                type="button"
                disabled={!canManage}
                onClick={() => run(() => onUpdate(restartCompletedGoalPayload(goal)))}
              >
                重新开始
              </button>
            ) : null}
            <button
              type="button"
              disabled={!canManage}
              onClick={() => setEditorMode("edit")}
            >
              {goal.status === "complete" ? "修改后继续" : "编辑"}
            </button>
            <button
              type="button"
              disabled={!canManage}
              onClick={() => run(clear)}
            >
              清除
            </button>
          </div>
        </>
      ) : loading ? (
        <div className="goal-manager-empty">正在读取 Goal...</div>
      ) : (
        <div className="goal-manager-empty">
          <span>当前会话没有 Goal</span>
          <button
            type="button"
            disabled={!sessionId}
            onClick={() => setEditorMode("create")}
          >
            新建 Goal
          </button>
        </div>
      )}

      {operationError ? (
        <div className="goal-manager-error" role="alert">
          Goal 操作失败：{operationError}
        </div>
      ) : error ? (
        <div className="goal-manager-error" role="alert">
          Goal 同步失败：{error}
        </div>
      ) : null}

      <WarmActionDialog
        open={editorMode !== null}
        title={
          editorMode === "edit"
            ? goal?.status === "complete"
              ? "修改后继续 Goal"
              : "编辑 Goal"
            : "新建 Goal"
        }
        description={
          editorMode === "edit"
            ? goal?.status === "complete"
              ? "保存后重新激活原 Goal，并保留已经累计的耗时与 Token 用量。"
              : "预算仍不足时会继续保持受限，其他状态保持不变。"
            : "Goal 会跨轮次持续执行，直到完成、暂停、受限或被清除。"
        }
        inputLabel="目标"
        initialValue={editorMode === "edit" ? goal?.objective ?? "" : ""}
        inputMaxLength={4000}
        inputMultiline
        confirmText={
          editorMode === "edit" && goal?.status === "complete"
            ? "保存并继续"
            : editorMode === "edit"
              ? "保存 Goal"
              : "开始 Goal"
        }
        onClose={() => setEditorMode(null)}
        onConfirm={async (objective) => {
          setOperationError(null);
          if (editorMode === "edit") {
            if (!goal) {
              throw new Error("当前 Goal 已不存在");
            }
            await onUpdate({
              objective,
              status: goalEditStatus(goal.status),
            });
            return;
          }
          await onUpdate({
            objective,
            status: "active",
            replace: true,
          });
        }}
      />
    </section>
  );
}
