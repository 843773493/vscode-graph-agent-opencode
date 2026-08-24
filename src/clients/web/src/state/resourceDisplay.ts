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
  if (kind === "browser" && action === "resume") {
    return "重新打开";
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
  if (kind === "browser" && action === "resume") {
    return "重新打开浏览器";
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
    emit_system_time_messages: "定时发送系统时间",
  };
  return labels[resource.name] ?? resource.name;
}

export function isClosedBackgroundTask(resource: SessionResource): boolean {
  return resource.kind === "background_task" &&
    !["pending", "running"].includes(resource.status);
}

export type ResourceAttentionGroup =
  | "active"
  | "attention"
  | "available"
  | "sleeping"
  | "history";

export interface ResourceTreeGroup {
  key: ResourceAttentionGroup;
  label: string;
  description: string;
  defaultOpen: boolean;
  resources: SessionResource[];
}

const RESOURCE_GROUP_DEFINITIONS: Omit<ResourceTreeGroup, "resources">[] = [
  {
    key: "active",
    label: "正在使用",
    description: "当前预览、已连接或正在执行",
    defaultOpen: true,
  },
  {
    key: "attention",
    label: "需要处理",
    description: "失败、断开或等待用户操作",
    defaultOpen: true,
  },
  {
    key: "available",
    label: "后台可用",
    description: "仍在运行，可随时切换",
    defaultOpen: true,
  },
  {
    key: "sleeping",
    label: "已挂起 / 可恢复",
    description: "已冻结或冷回收，打开时自动唤醒",
    defaultOpen: false,
  },
  {
    key: "history",
    label: "历史记录",
    description: "已释放，仅保留元数据",
    defaultOpen: false,
  },
];

function metadataString(resource: SessionResource, key: string): string {
  const value = resource.metadata[key];
  return typeof value === "string" ? value.trim() : "";
}

function metadataNumber(resource: SessionResource, key: string): number {
  const value = resource.metadata[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function resourcePreviewPath(resource: SessionResource): string | null {
  if (resource.kind === "browser") {
    return `browser://${resource.resource_id}`;
  }
  if (resource.kind === "terminal") {
    return `terminal://${resource.resource_id}`;
  }
  return null;
}

export function isPreviewedResource(
  resource: SessionResource,
  activePreviewPath: string | null,
): boolean {
  return resourcePreviewPath(resource) === activePreviewPath;
}

export function isRecoverableBrowser(resource: SessionResource): boolean {
  return resource.kind === "browser"
    && ["running", "lost"].includes(resource.status)
    && metadataString(resource, "resource_state") === "discarded"
    && typeof resource.metadata.checkpoint === "object"
    && resource.metadata.checkpoint !== null;
}

export function resourceAttentionGroup(
  resource: SessionResource,
  activePreviewPath: string | null,
): ResourceAttentionGroup {
  const resourceState = metadataString(resource, "resource_state");
  const commandStatus = metadataString(resource, "command_status");
  const isPreviewed = isPreviewedResource(resource, activePreviewPath);
  const hasClients = metadataNumber(resource, "client_count") > 0;
  const needsUserAction = Boolean(
    resource.metadata.pending_dialog || resource.metadata.pending_file_chooser,
  );

  if (isRecoverableBrowser(resource)) {
    return "sleeping";
  }
  if (["failed", "lost"].includes(resource.status) || needsUserAction) {
    return "attention";
  }
  if (
    ["closed", "terminated", "deleted", "completed", "cancelled"].includes(
      resource.status,
    ) || resource.metadata.resource_source === "历史记录"
  ) {
    return "history";
  }
  if (isPreviewed || hasClients) {
    return resource.status === "running" ? "active" : "attention";
  }
  if (["frozen", "discarded"].includes(resourceState) || resource.status === "paused") {
    return "sleeping";
  }
  if (
    (resource.kind === "background_task" && ["pending", "running"].includes(resource.status)) ||
    commandStatus === "running" ||
    ["active", "restoring", "freezing", "discarding"].includes(resourceState)
  ) {
    return "active";
  }
  return "available";
}

function resourceRecency(resource: SessionResource): number {
  const candidates = [
    metadataString(resource, "last_input_at"),
    metadataString(resource, "last_wake_at"),
    resource.updated_at,
    resource.started_at ?? "",
    resource.created_at,
  ];
  for (const candidate of candidates) {
    const value = Date.parse(candidate);
    if (Number.isFinite(value)) {
      return value;
    }
  }
  return 0;
}

export function groupSessionResources(
  resources: SessionResource[],
  activePreviewPath: string | null,
): ResourceTreeGroup[] {
  const buckets = new Map<ResourceAttentionGroup, SessionResource[]>();
  for (const definition of RESOURCE_GROUP_DEFINITIONS) {
    buckets.set(definition.key, []);
  }
  for (const resource of resources) {
    buckets.get(resourceAttentionGroup(resource, activePreviewPath))?.push(resource);
  }
  return RESOURCE_GROUP_DEFINITIONS.map((definition) => ({
    ...definition,
    resources: [...(buckets.get(definition.key) ?? [])].sort((left, right) => {
      const previewDifference = Number(isPreviewedResource(right, activePreviewPath)) -
        Number(isPreviewedResource(left, activePreviewPath));
      if (previewDifference !== 0) {
        return previewDifference;
      }
      const clientDifference = metadataNumber(right, "client_count") -
        metadataNumber(left, "client_count");
      return clientDifference || resourceRecency(right) - resourceRecency(left);
    }),
  })).filter((group) => group.resources.length > 0);
}

function resourceUrlSummary(resource: SessionResource): string {
  const url = metadataString(resource, "url");
  if (!url || url === "about:blank") {
    return "空白页";
  }
  if (url.startsWith("data:")) {
    return "内嵌页面";
  }
  if (url.startsWith("chrome-error:")) {
    return "加载错误页面";
  }
  try {
    const parsed = new URL(url);
    return parsed.hostname || parsed.protocol.replace(":", "");
  } catch {
    return url.length > 44 ? `${url.slice(0, 41)}…` : url;
  }
}

function pathBaseName(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).pop() || path;
}

export function resourceTreeTitle(resource: SessionResource): string {
  if (resource.kind === "browser") {
    const title = metadataString(resource, "title");
    return title && title !== "无标题" ? title : resourceUrlSummary(resource);
  }
  if (resource.kind === "terminal") {
    const normalizedName = resource.name.replace(/^终端\s*\/\s*/u, "").trim();
    if (normalizedName && normalizedName !== resource.resource_id) {
      return normalizedName;
    }
    const cwd = metadataString(resource, "cwd");
    return cwd ? `终端 · ${pathBaseName(cwd)}` : "用户终端";
  }
  return resourceName(resource);
}

export function resourceTreeDescription(resource: SessionResource): string {
  if (resource.kind === "browser") {
    return resourceUrlSummary(resource);
  }
  if (resource.kind === "terminal") {
    const command = metadataString(resource, "command") ||
      metadataString(resource, "last_input") || metadataString(resource, "cwd");
    return command || "等待命令";
  }
  const error = metadataString(resource, "error_message");
  const target = metadataString(resource, "target_session_id");
  return error || (target ? `目标 ${target}` : statusLabel(resource.status));
}

export function resourceTreeStatus(resource: SessionResource): string {
  if (isRecoverableBrowser(resource)) {
    return "已冷回收";
  }
  if (resource.status !== "running") {
    return statusLabel(resource.status);
  }
  if (metadataNumber(resource, "client_count") > 0) {
    return `${metadataNumber(resource, "client_count")} 个连接`;
  }
  const resourceState = metadataString(resource, "resource_state");
  const labels: Record<string, string> = {
    active: "活跃",
    background: "后台",
    freezing: "冻结中",
    frozen: "已冻结",
    discarding: "释放中",
    discarded: "可恢复",
    restoring: "唤醒中",
  };
  return labels[resourceState] ?? statusLabel(resource.status);
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
    pending: "等待中",
    paused: "已暂停",
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
    device_profile: "设备模拟",
    device_orientation: "设备方向",
    device_scale_factor: "像素比",
    touch_simulation_enabled: "触摸模拟",
    pending_dialog: "待处理对话框",
    pending_file_chooser: "待处理文件选择",
    client_count: "连接数",
    sequence: "输出序号",
    resource_source: "资源来源",
    status_note: "状态说明",
    historical_status: "历史状态",
    error_message: "错误信息",
    resource_state: "资源状态",
    resource_policy: "资源策略",
    resource_protection_reasons: "回收保护原因",
    resource_transition_reason: "状态切换原因",
    resource_transition_error: "状态切换错误",
    frozen_at: "冻结时间",
    discarded_at: "冷回收时间",
    last_wake_at: "最近唤醒",
    runtime_generation: "运行时代次",
    stream_metrics: "流性能",
    checkpoint: "恢复检查点",
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
    const resourceState = typeof resource.metadata.resource_state === "string"
      ? resource.metadata.resource_state
      : "unknown";
    const resourceStateLabels: Record<string, string> = {
      active: "活跃",
      background: "后台",
      freezing: "正在冻结",
      frozen: "已冻结",
      discarding: "正在冷回收",
      discarded: "已冷回收",
      restoring: "正在唤醒",
      lost: "运行时丢失",
    };
    const resourceStateLabel = resourceStateLabels[resourceState] ?? resourceState;
    const browserStatus =
      resource.status === "running"
        ? "浏览器页面运行中"
        : isRecoverableBrowser(resource)
          ? "浏览器页面可重新打开"
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
    return `${browserStatus} · 资源 ${resourceStateLabel} · ${title} · ${url}`;
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
