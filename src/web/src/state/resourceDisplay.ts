import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import { formatDateTime } from "../utils/format";

const ACTION_LABELS: Record<SessionResourceAction, string> = {
  pause: "暂停",
  resume: "继续",
  cancel: "取消",
  delete: "删除",
};

export function actionLabelForKind(
  kind: SessionResourceKind,
  action: SessionResourceAction,
): string {
  if (kind === "terminal" && action === "cancel") {
    return "终止";
  }
  if (kind === "browser" && action === "cancel") {
    return "关闭";
  }
  return ACTION_LABELS[action];
}

export function resourceActionStatusLabel(
  kind: SessionResourceKind,
  action: SessionResourceAction,
): string {
  if (kind === "terminal" && action === "cancel") {
    return "终止终端";
  }
  if (kind === "terminal" && action === "delete") {
    return "删除终端";
  }
  if (kind === "browser" && action === "cancel") {
    return "关闭浏览器";
  }
  if (kind === "browser" && action === "delete") {
    return "删除浏览器";
  }
  return actionLabelForKind(kind, action);
}

export function actionLabel(
  resource: SessionResource,
  action: SessionResourceAction,
): string {
  return actionLabelForKind(resource.kind, action);
}

export function kindLabel(kind: SessionResourceKind): string {
  if (kind === "terminal") {
    return "终端";
  }
  if (kind === "browser") {
    return "浏览器";
  }
  return "后台任务";
}

export function resourceName(resource: SessionResource): string {
  if (resource.kind !== "background_task") {
    return resource.name;
  }
  const labels: Record<string, string> = {
    monitor_session_agent_end: "监听会话 Agent 完成",
    managed_auto_continue: "会话自动继续",
    emit_system_time_messages: "定时发送系统时间",
  };
  return labels[resource.name] ?? resource.name;
}

export function isClosedBackgroundTask(resource: SessionResource): boolean {
  return resource.kind === "background_task" &&
    !["pending", "running"].includes(resource.status);
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: "运行中",
    terminated: "已终止",
    deleted: "已删除",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    lost: "已断开 (lost)",
    closed: "已关闭",
    queued: "排队中",
    accepted: "已接收",
  };
  return labels[status] ?? status;
}

function metadataValueLabel(
  key: string,
  value: unknown,
  resource?: SessionResource,
): string {
  if (value === null || value === undefined || value === "") {
    return "无";
  }
  if (key.endsWith("_at") && typeof value === "string") {
    return formatDateTime(value) || value;
  }
  if (
    key === "command_status" ||
    key === "historical_status" ||
    key === "status"
  ) {
    return typeof value === "string" ? statusLabel(value) : JSON.stringify(value);
  }
  if (key === "last_input_source" && typeof value === "string") {
    const labels: Record<string, string> = {
      user: "用户",
      agent: "Agent",
      interactive: "交互",
    };
    return labels[value] ?? value;
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

export function metadataRows(resource: SessionResource): [string, string][] {
  const metadata = resource.metadata;
  const labels: Record<string, string> = {
    cwd: "工作目录",
    command: "最近工具命令",
    shell_command: "启动命令",
    command_status: "命令状态",
    command_exit_code: "退出码",
    command_started_at: "命令开始",
    command_completed_at: "命令完成",
    last_input: "最近输入",
    last_input_source: "输入来源",
    last_input_at: "输入时间",
    os_pid: "系统 PID",
    process_group_id: "进程组",
    process_session_id: "进程会话",
    release_reason: "释放原因",
    page_id: "页面 ID",
    url: "URL",
    title: "标题",
    viewport: "视口",
    pending_dialog: "待处理对话框",
    pending_file_chooser: "待处理文件选择",
    client_count: "连接数",
    sequence: "输出序号",
    resource_source: "资源来源",
    status_note: "状态说明",
    historical_status: "历史状态",
    error_message: "错误信息",
    target_session_id: "目标会话",
    timeout_seconds: "超时秒数",
    poll_interval_seconds: "轮询间隔秒数",
    max_events: "最多转发事件数",
    source_id: "消息来源 ID",
    submitted_at: "提交时间",
    started_at: "启动时间",
    message_count: "消息数量",
    interval_seconds: "发送间隔秒数",
    result: "完成结果",
  };
  return Object.entries(metadata).filter(([key]) => key !== "attach_url").map(([key, value]) => [
    labels[key] ?? key,
    metadataValueLabel(key, value, resource),
  ]);
}

export function resourceStateSummary(resource: SessionResource): string | null {
  if (resource.kind === "background_task") {
    const targetSession =
      typeof resource.metadata.target_session_id === "string"
        ? resource.metadata.target_session_id
        : "";
    const errorMessage =
      typeof resource.metadata.error_message === "string"
        ? resource.metadata.error_message
        : "";
    const base = `${resourceName(resource)} · ${statusLabel(resource.status)}`;
    if (errorMessage) {
      return `${base} · ${errorMessage}`;
    }
    return targetSession ? `${base} · 目标 ${targetSession}` : base;
  }
  if (resource.kind === "browser") {
    const errorMessage =
      typeof resource.metadata.error_message === "string"
        ? resource.metadata.error_message.trim()
        : "";
    const title =
      typeof resource.metadata.title === "string" && resource.metadata.title
        ? resource.metadata.title
        : "无标题";
    const url =
      typeof resource.metadata.url === "string" && resource.metadata.url
        ? resource.metadata.url
        : "about:blank";
    const browserStatus =
      resource.status === "running"
        ? "浏览器页面运行中"
        : resource.status === "lost"
          ? "浏览器页面已断开"
          : resource.status === "closed"
            ? "浏览器页面已关闭"
            : resource.status === "deleted"
              ? "浏览器页面已删除"
              : resource.status === "failed"
                ? "浏览器页面启动失败"
                : `浏览器页面 ${statusLabel(resource.status)}`;
    if (resource.status === "failed" && errorMessage) {
      return `${browserStatus} · ${errorMessage}`;
    }
    return `${browserStatus} · ${title} · ${url}`;
  }

  if (resource.kind !== "terminal") {
    return null;
  }
  const commandStatus =
    typeof resource.metadata.command_status === "string"
      ? resource.metadata.command_status
      : "无命令";
  const lastInput =
    typeof resource.metadata.last_input === "string"
      ? resource.metadata.last_input
      : "";
  const lastInputSource =
    typeof resource.metadata.last_input_source === "string"
      ? metadataValueLabel("last_input_source", resource.metadata.last_input_source)
      : "手动";
  const exitCode =
    typeof resource.metadata.command_exit_code === "number"
      ? `，退出码 ${resource.metadata.command_exit_code}`
      : "";
  const terminalStatus =
    resource.status === "running"
      ? "终端会话运行中"
      : resource.status === "lost"
        ? "终端会话已断开"
        : resource.status === "terminated"
          ? "终端会话已终止"
          : resource.status === "deleted"
            ? "终端会话已删除"
            : `终端会话 ${statusLabel(resource.status)}`;
  const commandLabel =
    commandStatus === "completed"
      ? `最近工具命令已完成${exitCode}`
      : commandStatus === "running"
        ? "最近工具命令运行中"
        : commandStatus === "deleted"
          ? "最近工具命令已随终端删除"
          : commandStatus === "terminated"
            ? "最近工具命令已终止"
            : commandStatus === "无命令"
              ? "最近工具命令：无"
              : `最近工具命令 ${statusLabel(commandStatus)}${exitCode}`;
  const inputLabel = lastInput
    ? ` · 最近${lastInputSource}输入：${lastInput}`
    : "";
  return `${terminalStatus} · ${commandLabel}${inputLabel}`;
}
