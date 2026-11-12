import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { MessageStreamActivity } from "../../../state/messageStream/index";

export type ActivityRenderer = (
  activity: MessageStreamActivity,
) => React.ReactNode;

function activityLabel(activity: MessageStreamActivity): string {
  if (activity.status === "waiting") return t("chat.activity.waiting");
  if (activity.status === "stopping") return t("chat.activity.stopping");
  if (activity.status === "completed") return t("chat.activity.completed");
  if (activity.status === "failed") return t("chat.activity.failed");
  if (activity.status === "unknown") return t("chat.activity.unknown");
  return t("chat.activity.running");
}

export function activityKindLabel(kind: string): string {
  const key = kind === "context.compaction" ? "chat.activity.kind.contextCompaction"
    : kind === "approval.wait" ? "chat.activity.kind.approvalWait"
      : kind === "subagent.run" ? "chat.activity.kind.subagentRun"
        : kind === "resource.operation" ? "chat.activity.kind.resourceOperation" : undefined;
  return key ? t(key) : kind;
}

function compactionLabel(activity: MessageStreamActivity): string {
  if (activity.status === "waiting") return t("chat.compaction.waiting");
  if (activity.status === "stopping") return t("chat.compaction.stopping");
  if (activity.status === "completed") return t("chat.compaction.completed");
  if (activity.status === "failed") return t("chat.compaction.failed");
  if (activity.status === "unknown") return t("chat.compaction.unknown");
  return t("chat.compaction.running");
}

type ActivityLabels = Record<MessageStreamActivity["status"], string>;

const BROWSER_OPERATIONS = new Set([
  "listBrowserPage",
  "openBrowserPage",
  "readPage",
  "navigatePage",
  "clickElement",
  "typeInPage",
  "hoverElement",
  "dragElement",
  "handleDialog",
  "screenshotPage",
  "runPlaywrightCode",
]);

function resourceActivityLabels(
  activity: MessageStreamActivity,
): ActivityLabels {
  const operation = activity.detail?.operation;
  const isBrowserOperation = typeof operation === "string"
    && BROWSER_OPERATIONS.has(operation);
  const subject = isBrowserOperation ? t("chat.resource.browser") : t("chat.resource.workspace");
  return {
    running: t("chat.resource.running", subject),
    waiting: t("chat.resource.waiting", subject),
    stopping: t("chat.resource.stopping", subject),
    completed: t("chat.resource.completed", subject),
    failed: t("chat.resource.failed", subject),
    unknown: t("chat.resource.unknown", subject),
  };
}

function renderKnownActivity(
  activity: MessageStreamActivity,
  labels: ActivityLabels,
): React.ReactNode {
  const label = labels[activity.status];
  return (
    <>
      <span>{label}</span>
      {activity.summary && activity.summary !== label ? (
        <span className="chat-working-detail">{activity.summary}</span>
      ) : null}
      {!activity.detail_available ? (
        <span className="chat-working-detail">
          {activity.status === "failed" || activity.status === "unknown"
            ? t("chat.activity.noResult")
            : t("chat.activity.noProgress")
          }
        </span>
      ) : null}
    </>
  );
}

function genericActivityRenderer(activity: MessageStreamActivity): React.ReactNode {
  const label = `${activityLabel(activity)} ${activityKindLabel(activity.kind)}`;
  return (
    <>
      <span>{label}</span>
      {activity.summary && activity.summary !== label ? (
        <span className="chat-working-detail">{activity.summary}</span>
      ) : null}
      {!activity.detail_available ? (
        <span className="chat-working-detail">{t("chat.activity.generic")}</span>
      ) : null}
    </>
  );
}

const builtInRenderers: Record<string, ActivityRenderer> = {
  "context.compaction": (activity) => {
    const label = compactionLabel(activity);
    return (
      <>
        <span>{label}</span>
        {activity.summary && activity.summary !== label ? (
          <span className="chat-working-detail">{activity.summary}</span>
        ) : null}
        {!activity.detail_available ? (
          <span className="chat-working-detail">
            {activity.status === "failed" || activity.status === "unknown"
              ? t("chat.compaction.noDetails")
              : t("chat.compaction.detailsLater")}
          </span>
        ) : null}
      </>
    );
  },
  "approval.wait": (activity) => (
    <>
      {renderKnownActivity(activity, {
        running: t("chat.approval.running"),
        waiting: t("chat.approval.waiting"),
        stopping: t("chat.approval.stopping"),
        completed: t("chat.approval.completed"),
        failed: t("chat.approval.failed"),
        unknown: t("chat.approval.unknown"),
      })}
      {activity.status === "waiting" ? (
        <span className="chat-working-detail">{t("chat.approval.needsUser")}</span>
      ) : null}
    </>
  ),
  "subagent.run": (activity) => (
    <>
      {renderKnownActivity(activity, {
        running: t("chat.subagent.running"),
        waiting: t("chat.subagent.waiting"),
        stopping: t("chat.subagent.stopping"),
        completed: t("chat.subagent.completed"),
        failed: t("chat.subagent.failed"),
        unknown: t("chat.subagent.unknown"),
      })}
      {activity.resumable && activity.status !== "completed" ? (
        <span className="chat-working-detail">{t("chat.subagent.resume")}</span>
      ) : null}
    </>
  ),
  "resource.operation": (activity) => (
    <>
      {renderKnownActivity(activity, resourceActivityLabels(activity))}
      {activity.status === "failed" && activity.detail?.retryable === true ? (
        <span className="chat-working-detail">
          {t("chat.resource.recoverable", activity.detail.recovery === "page_reset"
            ? t("chat.resource.pageReset")
            : t("chat.resource.retry"))}
        </span>
      ) : null}
      {activity.resource_refs.length > 0 ? (
        <span className="chat-working-detail">
          {t("chat.resource.references", formatNumber(activity.resource_refs.length))}
        </span>
      ) : null}
    </>
  ),
};

export class ActivityRendererRegistry {
  private readonly renderers = new Map<string, ActivityRenderer>(
    Object.entries(builtInRenderers),
  );

  register(kind: string, renderer: ActivityRenderer): void {
    if (!kind.trim()) throw new Error("Activity Renderer 缺少 kind");
    if (this.renderers.has(kind)) {
      throw new Error(`Activity Renderer 重复注册: ${kind}`);
    }
    this.renderers.set(kind, renderer);
  }

  render(activity: MessageStreamActivity): React.ReactNode {
    return (this.renderers.get(activity.kind) ?? genericActivityRenderer)(activity);
  }
}

export const activityRendererRegistry = new ActivityRendererRegistry();
