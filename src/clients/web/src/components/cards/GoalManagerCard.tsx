import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import { useEffect, useState } from "react";
import type {
  SessionGoal,
  SessionGoalUpdateRequest,
} from "../../types/backend";
import {
  GOAL_STATUS_LABELS,
  goalCanResume,
  goalEditStatus,
  restartCompletedGoalPayload,
} from "../../state/sessionGoal";
import WarmActionDialog from "../overlays/WarmActionDialog";
import { useWarmConfirm } from "../shell/WarmConfirmProvider";
import { errorMessage } from "../../utils/errorMessage";

function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours > 0) {
    return t("goal.duration.hours", formatNumber(hours), formatNumber(minutes));
  }
  if (minutes > 0) {
    return t("goal.duration.minutes", formatNumber(minutes), formatNumber(remainder));
  }
  return t("goal.duration.seconds", formatNumber(remainder));
}

type GoalEditorMode = "create" | "edit" | null;

export default function GoalManagerCard({
  sessionId,
  goal,
  controlRevision = goal?.control_revision ?? 0,
  loading,
  error,
  onRefresh,
  onUpdate,
  onClear,
}: {
  sessionId: string;
  goal: SessionGoal | null;
  controlRevision?: number;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<SessionGoal | null>;
  onUpdate: (payload: SessionGoalUpdateRequest) => Promise<SessionGoal>;
  onClear: (expectedControlRevision: number) => Promise<void>;
}) {
  const confirm = useWarmConfirm();
  const [editorMode, setEditorMode] = useState<GoalEditorMode>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [tokenBudget, setTokenBudget] = useState("");
  const [editControlRevision, setEditControlRevision] = useState<number | null>(null);
  const openEditor = (mode: Exclude<GoalEditorMode, null>) => {
    setTokenBudget(mode === "edit" && goal?.token_budget != null ? String(goal.token_budget) : "");
    setEditControlRevision(mode === "edit" && goal ? goal.control_revision : controlRevision);
    setEditorMode(mode);
  };

  useEffect(() => {
    setEditorMode(null);
    setOperationError(null);
    setEditControlRevision(null);
  }, [sessionId]);

  const run = (operation: () => Promise<unknown>) => {
    setOperationError(null);
    void operation().catch((operationFailure: unknown) => {
      setOperationError(
        errorMessage(operationFailure),
      );
    });
  };

  const deleteGoal = async () => {
    if (!goal) {
      throw new Error(t("goal.manager.missing"));
    }
    const expectedControlRevision = goal.control_revision;
    const confirmed = await confirm({
      title: t("goal.manager.deleteTitle"),
      message: t("goal.manager.deleteDetail", goal.objective),
      confirmText: t("goal.manager.delete"),
      danger: true,
    });
    if (confirmed) {
      await onClear(expectedControlRevision);
    }
  };

  const canManage = Boolean(sessionId) && !loading && !error;
  return (
    <section className="goal-manager" aria-label={t("goal.manager.current")}>
      <div className="goal-manager-heading">
        <div className="goal-manager-title-row">
          <span className="codicon codicon-target" aria-hidden="true" />
          <strong>{t("goal.manager.current")}</strong>
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
          title={t("goal.manager.refresh")}
          aria-label={t("goal.manager.refresh")}
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
            <span>{t("goal.manager.duration", formatDuration(goal.time_used_seconds))}</span>
            <span>
              Token {formatNumber(goal.tokens_used)}
              {goal.token_budget === null
                ? ""
                : ` / ${formatNumber(goal.token_budget)}`}
            </span>
          </div>
          <div className="goal-manager-actions">
            {goal.status === "active" ? (
              <button
                type="button"
                disabled={!canManage}
                title={t("goal.manager.pauseHint")}
                onClick={() => run(() => onUpdate({ status: "paused", expected_control_revision: goal.control_revision }))}
              >
                {t("goal.manager.pause")}
              </button>
            ) : goalCanResume(goal.status) ? (
              <button
                type="button"
                disabled={!canManage}
                title={t("goal.manager.resumeHint")}
                onClick={() => run(() => onUpdate({ status: "active", expected_control_revision: goal.control_revision }))}
              >
                {t("goal.manager.resume")}
              </button>
            ) : goal.status === "complete" ? (
              <button
                type="button"
                disabled={!canManage}
                title={t("goal.manager.restartHint")}
                onClick={() => run(() => onUpdate({
                  ...restartCompletedGoalPayload(goal), expected_control_revision: goal.control_revision,
                }))}
              >
                {t("goal.manager.restart")}
              </button>
            ) : null}
            <button
              type="button"
              disabled={!canManage}
              onClick={() => openEditor("edit")}
            >
              {goal.status === "complete" ? t("goal.manager.editContinue") : t("goal.manager.edit")}
            </button>
            <button
              type="button"
              disabled={!canManage}
              onClick={() => run(deleteGoal)}
            >
              {t("goal.manager.delete")}
            </button>
          </div>
          {goal.status === "budget_limited" ? (
            <p>{t("goal.manager.budgetLimited")}</p>
          ) : null}
        </>
      ) : !sessionId ? (
        <div className="goal-manager-empty">{t("goal.manager.selectSession")}</div>
      ) : loading ? (
        <div className="goal-manager-empty">{t("goal.manager.loading")}</div>
      ) : !error ? (
        <div className="goal-manager-empty">
          <span>{t("goal.manager.missing")}</span>
          <button
            type="button"
            disabled={!canManage}
            onClick={() => openEditor("create")}
          >
            {t("goal.manager.create")}
          </button>
        </div>
      ) : null}

      {operationError ? (
        <div className="goal-manager-error" role="alert">
          {t("goal.manager.failed", operationError)}
        </div>
      ) : error ? (
        <div className="goal-manager-error" role="alert">
          {t("goal.manager.syncFailed", error)}
        </div>
      ) : null}

      <WarmActionDialog
        open={editorMode !== null}
        title={
          editorMode === "edit"
            ? goal?.status === "complete"
              ? t("goal.manager.editContinueTitle")
              : t("goal.manager.editTitle")
            : t("goal.manager.create")
        }
        description={
          editorMode === "edit"
            ? goal?.status === "complete"
              ? t("goal.manager.editCompleteDetail")
              : t("goal.manager.editDetail")
            : t("goal.manager.createDetail")
        }
        inputLabel={t("goal.manager.objective")}
        initialValue={editorMode === "edit" ? goal?.objective ?? "" : ""}
        inputMaxLength={4000}
        inputMultiline
        confirmText={
          editorMode === "edit" && goal?.status === "complete"
            ? t("goal.manager.saveContinue")
            : editorMode === "edit"
              ? t("goal.manager.save")
              : t("goal.manager.start")
        }
        onClose={() => setEditorMode(null)}
        onConfirm={async (objective) => {
          setOperationError(null);
          const budget = tokenBudget.trim() ? Number(tokenBudget) : null;
          if (budget !== null && (!Number.isSafeInteger(budget) || budget <= 0)) {
            throw new Error(t("goal.manager.invalidBudget"));
          }
          if (editorMode === "edit") {
            if (!goal) {
              throw new Error(t("goal.manager.gone"));
            }
            await onUpdate({
              objective,
              token_budget: budget,
              status: goalEditStatus(goal.status),
              expected_control_revision: editControlRevision ?? goal.control_revision,
            });
            return;
          }
          await onUpdate({
            objective,
            token_budget: budget,
            status: "active",
            replace: true,
            expected_control_revision: editControlRevision ?? controlRevision,
          });
        }}
      >
        <label className="workspace-dialog-wide"><span>{t("goal.manager.budget")}</span>
          <input aria-label={t("goal.manager.budgetAria")} type="number" min={1} step={1} value={tokenBudget} onChange={(event) => setTokenBudget(event.target.value)} />
        </label>
      </WarmActionDialog>
    </section>
  );
}
