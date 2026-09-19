import type { ChildThreadDelegationStartStatus } from "../types/backend";

/**
 * delegation_start_status → 用户可见中文标签。
 * 接收宽化的 string：后端未来新增状态时直接展示原始值，不伪造已知状态。
 */
export function childThreadStatusLabel(status: string): string {
  if (status === "pending") return "等待启动";
  if (status === "running") return "运行中";
  if (status === "failed") return "启动失败";
  return status;
}

/** 状态徽标的配色类名；与 styles/childThreadPanel.css 中的状态变量对应。 */
export function childThreadStatusClass(
  status: ChildThreadDelegationStartStatus | string,
): string {
  if (status === "pending") return "child-thread-status-pending";
  if (status === "running") return "child-thread-status-running";
  if (status === "failed") return "child-thread-status-failed";
  return "child-thread-status-unknown";
}
