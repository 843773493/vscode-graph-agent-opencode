import type {
  NodeDebugActionRecord,
  NodeDebugLaunchProfile,
  NodeDebugState,
} from "../../types/backend";

export function nodeDebugStatusLabel(status: NodeDebugState["status"]): string {
  return {
    idle: "未启动",
    starting: "启动中",
    running: "运行中",
    paused: "已暂停",
    exited: "已退出",
    failed: "失败",
  }[status];
}

export function nodeDebugActionActor(action: NodeDebugActionRecord): string {
  return action.actor === "ai" ? "AI" : action.actor === "system" ? "系统" : "用户";
}

export function nodeDebugProfileLabel(profile: NodeDebugLaunchProfile): string {
  const support = profile.supported ? "" : " · 当前不支持";
  return `${profile.name} · ${profile.runtime}${support}`;
}

export function nodeDebugPauseReasonLabel(reason: string): string {
  return {
    other: "断点",
    exception: "异常",
    step: "单步",
    pause: "手动暂停",
  }[reason] ?? reason;
}
