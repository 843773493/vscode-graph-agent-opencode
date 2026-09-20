import type { ChildThreadStatus } from "../types/protocol";

const STATUS_LABELS: Record<ChildThreadStatus, string> = {
  pending: "等待启动",
  running: "运行中",
  failed: "启动失败",
};

export function childThreadStatusLabel(status: ChildThreadStatus): string {
  return STATUS_LABELS[status];
}

/** 状态徽标的配色类名；与 styles/childThreadPanel.css 中的状态变量对应。 */
const STATUS_CLASSES: Record<ChildThreadStatus, string> = {
  pending: "child-thread-status-pending",
  running: "child-thread-status-running",
  failed: "child-thread-status-failed",
};

export function childThreadStatusClass(status: ChildThreadStatus): string {
  return STATUS_CLASSES[status];
}
