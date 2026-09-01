import React from "react";
import type { MessageStreamActivity } from "../../../state/messageStream";

export type ActivityRenderer = (
  activity: MessageStreamActivity,
) => React.ReactNode;

function activityLabel(activity: MessageStreamActivity): string {
  if (activity.status === "waiting") return "等待继续";
  if (activity.status === "stopping") return "正在停止";
  if (activity.status === "completed") return "已完成";
  if (activity.status === "failed") return "处理失败";
  if (activity.status === "unknown") return "结果未知";
  return "正在处理";
}

function compactionLabel(activity: MessageStreamActivity): string {
  if (activity.status === "waiting") return "等待上下文压缩继续";
  if (activity.status === "stopping") return "正在完成上下文压缩";
  if (activity.status === "completed") return "上下文压缩已完成";
  if (activity.status === "failed") return "上下文压缩失败";
  if (activity.status === "unknown") return "上下文压缩结果未知";
  return "正在压缩上下文";
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
  const subject = isBrowserOperation ? "浏览器操作" : "工作区资源操作";
  return {
    running: `正在处理${subject}`,
    waiting: `等待${subject}继续`,
    stopping: `正在停止${subject}`,
    completed: `${subject}已完成`,
    failed: `${subject}失败`,
    unknown: `${subject}结果未知`,
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
            ? "详细结果不可用，请查看会话历史"
            : "详细进度不可用"
          }
        </span>
      ) : null}
    </>
  );
}

function genericActivityRenderer(activity: MessageStreamActivity): React.ReactNode {
  const label = `${activityLabel(activity)} ${activity.kind}`;
  return (
    <>
      <span>{label}</span>
      {activity.summary && activity.summary !== label ? (
        <span className="chat-working-detail">{activity.summary}</span>
      ) : null}
      {!activity.detail_available ? (
        <span className="chat-working-detail">当前 Activity 仅提供通用状态</span>
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
              ? "压缩细节不可用，请查看会话历史"
              : "压缩细节将在完成后写入历史"}
          </span>
        ) : null}
      </>
    );
  },
  "approval.wait": (activity) => (
    <>
      {renderKnownActivity(activity, {
        running: "正在处理审批",
        waiting: "等待审批",
        stopping: "正在停止审批",
        completed: "审批已完成",
        failed: "审批失败",
        unknown: "审批结果未知",
      })}
      {activity.status === "waiting" ? (
        <span className="chat-working-detail">需要用户确认后继续</span>
      ) : null}
    </>
  ),
  "subagent.run": (activity) => (
    <>
      {renderKnownActivity(activity, {
        running: "子 Agent 正在运行",
        waiting: "子 Agent 等待继续",
        stopping: "子 Agent 正在停止",
        completed: "子 Agent 已完成",
        failed: "子 Agent 执行失败",
        unknown: "子 Agent 结果未知",
      })}
      {activity.resumable && activity.status !== "completed" ? (
        <span className="chat-working-detail">后端重启后可从 checkpoint 恢复</span>
      ) : null}
    </>
  ),
  "resource.operation": (activity) => (
    <>
      {renderKnownActivity(activity, resourceActivityLabels(activity))}
      {activity.status === "failed" && activity.detail?.retryable === true ? (
        <span className="chat-working-detail">
          可恢复：{activity.detail.recovery === "page_reset"
            ? "页面已重置，请重新读取页面后重试"
            : "请按错误提示重试"}
        </span>
      ) : null}
      {activity.resource_refs.length > 0 ? (
        <span className="chat-working-detail">
          {activity.resource_refs.length} 个资源关联
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
