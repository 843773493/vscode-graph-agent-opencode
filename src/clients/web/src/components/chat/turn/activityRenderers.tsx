import React from "react";
import type { MessageStreamActivity } from "../../../state/messageStream";

export type ActivityRenderer = (
  activity: MessageStreamActivity,
) => React.ReactNode;

function activityLabel(activity: MessageStreamActivity): string {
  if (activity.status === "waiting") return "等待继续";
  if (activity.status === "stopping") return "正在停止";
  if (activity.status === "unknown") return "状态未知";
  return "正在处理";
}

function genericActivityRenderer(activity: MessageStreamActivity): React.ReactNode {
  return (
    <>
      {activity.summary ?? `${activityLabel(activity)} ${activity.kind}`}
      {!activity.detail_available ? (
        <span className="chat-working-detail">当前 Activity 仅提供通用进度</span>
      ) : null}
    </>
  );
}

const builtInRenderers: Record<string, ActivityRenderer> = {
  "context.compaction": (activity) => (
    <>
      {activity.summary ?? "正在压缩上下文"}
      {!activity.detail_available ? (
        <span className="chat-working-detail">压缩细节将在完成后写入历史</span>
      ) : null}
    </>
  ),
  "approval.wait": (activity) => (
    <>
      {activity.summary ?? "等待审批"}
      {activity.status === "waiting" ? (
        <span className="chat-working-detail">需要用户确认后继续</span>
      ) : null}
    </>
  ),
  "subagent.run": (activity) => (
    <>
      {activity.summary ?? "子 Agent 正在运行"}
      {activity.resumable ? (
        <span className="chat-working-detail">后端重启后可从 checkpoint 恢复</span>
      ) : null}
    </>
  ),
  "resource.operation": (activity) => (
    <>
      {activity.summary ?? "正在处理工作区资源"}
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
