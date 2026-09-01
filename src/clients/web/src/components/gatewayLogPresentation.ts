import type { GatewayDiagnosticLog } from "../types/backend";

export function diagnosticLogStatusLabel(log: GatewayDiagnosticLog): string {
  if (log.status === "available") return "可读";
  if (log.status === "empty") return "空日志";
  return "日志不可用";
}

export function diagnosticLogUnavailableHint(log: GatewayDiagnosticLog): string {
  const detail = log.error ?? "Gateway 没有返回日志内容。";
  return `独立诊断日志当前不可读，这不代表对应工作区服务已离线；请以工作区连接状态为准。${detail}`;
}
