import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import type {
  SessionResource,
  SessionResourceAction,
  SessionResourceKind,
} from "../../types/backend";
import { formatDateTime } from "../../utils/format";

const ACTION_LABELS: Record<SessionResourceAction, string> = {
  pause: t("resources.action.pause"),
  resume: t("resources.action.resume"),
  cancel: t("resources.action.cancel"),
  delete: t("resources.action.delete"),
  reconcile: t("resources.action.reconcile"),
};

export function actionLabelForKind(
  kind: SessionResourceKind,
  action: SessionResourceAction,
): string {
  if (kind === "terminal" && action === "cancel") {
    return t("resources.action.terminate");
  }
  if (kind === "browser" && action === "cancel") {
    return t("resources.action.close");
  }
  if (kind === "browser" && action === "resume") {
    return t("resources.action.reopen");
  }
  return ACTION_LABELS[action];
}

export function resourceActionStatusLabel(
  kind: SessionResourceKind,
  action: SessionResourceAction,
): string {
  if (kind === "terminal" && action === "cancel") {
    return t("resources.operation.terminateTerminal");
  }
  if (kind === "terminal" && action === "delete") {
    return t("resources.operation.deleteTerminal");
  }
  if (kind === "browser" && action === "cancel") {
    return t("resources.operation.closeBrowser");
  }
  if (kind === "browser" && action === "delete") {
    return t("resources.operation.deleteBrowser");
  }
  if (kind === "browser" && action === "resume") {
    return t("resources.operation.reopenBrowser");
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
    return t("resources.kind.terminal");
  }
  if (kind === "browser") {
    return t("resources.kind.browser");
  }
  return t("resources.kind.background_task");
}

const RESOURCE_KIND_ICONS: Record<SessionResourceKind, string> = {
  browser: "codicon-globe",
  terminal: "codicon-terminal",
  background_task: "codicon-server-process",
};

const BACKGROUND_TASK_NAME_LABELS: Record<string, string> = {
  emit_system_time_messages: t("resources.task.systemTime"),
};

const RESOURCE_RUNNING_STATE_LABELS: Record<string, string> = {
  active: t("resources.running.active"),
  background: t("resources.running.background"),
  freezing: t("resources.running.freezing"),
  frozen: t("resources.running.frozen"),
  discarding: t("resources.running.discarding"),
  discarded: t("resources.running.discarded"),
  restoring: t("resources.running.restoring"),
};

const RESOURCE_STATUS_LABELS: Record<string, string> = {
  // 终端 provider 会发 created/exited，浏览器 provider 会发 frozen/discarded，
  // 记录缺失时两边都会回退 unknown；这些必须使用本次启动语言，不把内部枚举直接当产品文案。
  created: t("resources.status.created"),
  exited: t("resources.status.exited"),
  frozen: t("resources.running.frozen"),
  discarded: t("resources.status.discarded"),
  unknown: t("resources.status.unknown"),
  running: t("resources.status.running"),
  terminated: t("resources.status.terminated"),
  deleted: t("resources.status.deleted"),
  completed: t("resources.status.completed"),
  failed: t("resources.status.failed"),
  cancelled: t("resources.status.cancelled"),
  lost: t("resources.status.lost"),
  closed: t("resources.status.closed"),
  pending: t("resources.status.pending"),
  paused: t("resources.status.paused"),
  queued: t("resources.status.queued"),
  accepted: t("resources.status.accepted"),
};

const LAST_INPUT_SOURCE_LABELS: Record<string, string> = {
  user: t("resources.input.user"),
  agent: t("resources.input.agent"),
  interactive: t("resources.input.interactive"),
};

export function resourceKindIcon(kind: SessionResourceKind): string {
  return RESOURCE_KIND_ICONS[kind];
}

/** 「终端 / xxx」前缀的唯一归一实现；调用方各自决定是否再 trim。 */
const TERMINAL_NAME_PREFIX = /^终端\s*\/\s*/u;

export function stripTerminalNamePrefix(name: string): string {
  return name.replace(TERMINAL_NAME_PREFIX, "");
}

export function resourceName(resource: SessionResource): string {
  if (resource.kind !== "background_task") {
    return resource.name;
  }
  return BACKGROUND_TASK_NAME_LABELS[resource.name] ?? resource.name;
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
    label: t("resources.group.active"),
    description: t("resources.group.activeDescription"),
    defaultOpen: true,
  },
  {
    key: "attention",
    label: t("resources.group.attention"),
    description: t("resources.group.attentionDescription"),
    defaultOpen: true,
  },
  {
    key: "available",
    label: t("resources.group.available"),
    description: t("resources.group.availableDescription"),
    defaultOpen: true,
  },
  {
    key: "sleeping",
    label: t("resources.group.sleeping"),
    description: t("resources.group.sleepingDescription"),
    defaultOpen: false,
  },
  {
    key: "history",
    label: t("resources.group.history"),
    description: t("resources.group.historyDescription"),
    defaultOpen: false,
  },
];

function resourceMetadataString(resource: SessionResource, key: string): string {
  const value = resource.metadata[key];
  return typeof value === "string" ? value.trim() : "";
}

function resourceMetadataNumber(resource: SessionResource, key: string): number {
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
    && (resource.status === "running" || resource.status === "lost")
    && resourceMetadataString(resource, "resource_state") === "discarded"
    && resource.metadata.checkpoint_available === true;
}

function resourceAttentionGroupFromSignals(
  resource: SessionResource,
  isPreviewed: boolean,
  clientCount: number,
): ResourceAttentionGroup {
  const resourceState = resourceMetadataString(resource, "resource_state");
  const commandStatus = resourceMetadataString(resource, "command_status");
  const hasClients = clientCount > 0;
  const needsUserAction = Boolean(
    resource.metadata.pending_dialog || resource.metadata.pending_file_chooser,
  );

  if (
    resource.kind === "browser"
    && (resource.status === "running" || resource.status === "lost")
    && resourceState === "discarded"
    && resource.metadata.checkpoint_available === true
  ) {
    return "sleeping";
  }
  if (resource.status === "failed" || resource.status === "lost" || needsUserAction) {
    return "attention";
  }
  if (
    resource.status === "closed"
    || resource.status === "terminated"
    || resource.status === "deleted"
    || resource.status === "completed"
    || resource.status === "cancelled"
    || resource.metadata.resource_source === "历史记录"
  ) {
    return "history";
  }
  if (isPreviewed || hasClients) {
    return resource.status === "running" ? "active" : "attention";
  }
  if (
    resourceState === "frozen"
    || resourceState === "discarded"
    || resource.status === "paused"
  ) {
    return "sleeping";
  }
  if (
    (resource.kind === "background_task"
      && (resource.status === "pending" || resource.status === "running")) ||
    commandStatus === "running" ||
    resourceState === "active"
    || resourceState === "restoring"
    || resourceState === "freezing"
    || resourceState === "discarding"
  ) {
    return "active";
  }
  return "available";
}

export function resourceAttentionGroup(
  resource: SessionResource,
  activePreviewPath: string | null,
): ResourceAttentionGroup {
  return resourceAttentionGroupFromSignals(
    resource,
    isPreviewedResource(resource, activePreviewPath),
    resourceMetadataNumber(resource, "client_count"),
  );
}

function resourceRecency(resource: SessionResource): number {
  const candidates = [
    resourceMetadataString(resource, "last_input_at"),
    resourceMetadataString(resource, "last_wake_at"),
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
  interface SortableResource {
    resource: SessionResource;
    isPreviewed: boolean;
    clientCount: number;
    recency?: number;
  }

  const buckets = new Map<ResourceAttentionGroup, SortableResource[]>();
  for (const definition of RESOURCE_GROUP_DEFINITIONS) {
    buckets.set(definition.key, []);
  }
  for (const resource of resources) {
    const isPreviewed = isPreviewedResource(resource, activePreviewPath);
    const clientCount = resourceMetadataNumber(resource, "client_count");
    const group = resourceAttentionGroupFromSignals(
      resource,
      isPreviewed,
      clientCount,
    );
    buckets.get(group)?.push({ resource, isPreviewed, clientCount });
  }
  return RESOURCE_GROUP_DEFINITIONS.map((definition) => {
    const entries = buckets.get(definition.key) ?? [];
    if (entries.length > 1) {
      for (const entry of entries) entry.recency = resourceRecency(entry.resource);
      entries.sort((left, right) =>
        Number(right.isPreviewed) - Number(left.isPreviewed)
        || right.clientCount - left.clientCount
        || (right.recency ?? 0) - (left.recency ?? 0),
      );
    }
    return {
      ...definition,
      resources: entries.map((entry) => entry.resource),
    };
  }).filter((group) => group.resources.length > 0);
}

function resourceUrlSummary(resource: SessionResource): string {
  const url = resourceMetadataString(resource, "url");
  if (!url || url === "about:blank") {
    return t("resources.url.blank");
  }
  if (url.startsWith("data:")) {
    return t("resources.url.data");
  }
  if (url.startsWith("chrome-error:")) {
    return t("resources.url.error");
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
    const title = resourceMetadataString(resource, "title");
    return title && title !== "无标题" ? title : resourceUrlSummary(resource);
  }
  if (resource.kind === "terminal") {
    const normalizedName = stripTerminalNamePrefix(resource.name).trim();
    if (normalizedName && normalizedName !== resource.resource_id) {
      return normalizedName;
    }
    // 只消费工作区内相对目录；"." / "" 表示工作区根，回退默认文案（裁定 D-A2/D-A3）。
    const cwdRelative = resourceMetadataString(resource, "cwd_relative");
    return cwdRelative && cwdRelative !== "."
      ? t("resources.terminal.directory", pathBaseName(cwdRelative))
      : t("resources.terminal.default");
  }
  return resourceName(resource);
}

export function resourceTreeDescription(resource: SessionResource): string {
  if (resource.kind === "browser") {
    return resourceUrlSummary(resource);
  }
  if (resource.kind === "terminal") {
    const command = resourceMetadataString(resource, "command") ||
      resourceMetadataString(resource, "last_input") ||
      resourceMetadataString(resource, "cwd_relative");
    return command || t("resources.terminal.waiting");
  }
  const error = resourceMetadataString(resource, "error_message");
  const target = resourceMetadataString(resource, "target_session_id");
  return error || (target ? t("resources.target", target) : statusLabel(resource.status));
}

export function resourceTreeStatus(resource: SessionResource): string {
  if (isRecoverableBrowser(resource)) {
    return t("resources.status.discarded");
  }
  if (resource.status !== "running") {
    return statusLabel(resource.status);
  }
  const clientCount = resourceMetadataNumber(resource, "client_count");
  if (clientCount > 0) {
    return t("resources.connections", formatNumber(clientCount));
  }
  const resourceState = resourceMetadataString(resource, "resource_state");
  return RESOURCE_RUNNING_STATE_LABELS[resourceState] ?? statusLabel(resource.status);
}

export function statusLabel(status: string): string {
  return RESOURCE_STATUS_LABELS[status] ?? status;
}

function resourceMetadataValueLabel(
  key: string,
  value: unknown,
): string {
  if (value === null || value === undefined || value === "") {
    return t("resources.metadata.none");
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
    return LAST_INPUT_SOURCE_LABELS[value] ?? value;
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

const RESOURCE_METADATA_LABELS: Record<string, string> = {
  cwd_relative: t("resources.metadata.cwd"),
  command: t("resources.metadata.command"),
  shell_command: t("resources.metadata.shell"),
  command_status: t("resources.metadata.commandStatus"),
  command_exit_code: t("resources.metadata.exitCode"),
  command_started_at: t("resources.metadata.commandStarted"),
  command_completed_at: t("resources.metadata.commandCompleted"),
  last_input: t("resources.metadata.lastInput"),
  last_input_source: t("resources.metadata.inputSource"),
  last_input_at: t("resources.metadata.inputTime"),
  os_pid: t("resources.metadata.pid"),
  process_group_id: t("resources.metadata.processGroup"),
  process_session_id: t("resources.metadata.processSession"),
  release_reason: t("resources.metadata.releaseReason"),
  page_id: t("resources.metadata.pageId"),
  url: t("resources.metadata.url"),
  title: t("resources.metadata.title"),
  viewport: t("resources.metadata.viewport"),
  device_profile: t("resources.metadata.device"),
  device_orientation: t("resources.metadata.orientation"),
  device_scale_factor: t("resources.metadata.scale"),
  touch_simulation_enabled: t("resources.metadata.touch"),
  pending_dialog: t("resources.metadata.dialog"),
  pending_file_chooser: t("resources.metadata.fileChooser"),
  client_count: t("resources.metadata.clients"),
  sequence: t("resources.metadata.sequence"),
  resource_source: t("resources.metadata.source"),
  status_note: t("resources.metadata.statusNote"),
  historical_status: t("resources.metadata.historicalStatus"),
  error_message: t("resources.metadata.error"),
  resource_state: t("resources.metadata.state"),
  resource_policy: t("resources.metadata.policy"),
  resource_protection_reasons: t("resources.metadata.protection"),
  resource_transition_reason: t("resources.metadata.transitionReason"),
  resource_transition_error: t("resources.metadata.transitionError"),
  frozen_at: t("resources.metadata.frozen"),
  discarded_at: t("resources.metadata.discarded"),
  last_wake_at: t("resources.metadata.wake"),
  runtime_generation: t("resources.metadata.generation"),
  stream_metrics: t("resources.metadata.metrics"),
  checkpoint_available: t("resources.metadata.checkpoint"),
  target_session_id: t("resources.metadata.target"),
  timeout_seconds: t("resources.metadata.timeout"),
  poll_interval_seconds: t("resources.metadata.poll"),
  max_events: t("resources.metadata.maxEvents"),
  source_id: t("resources.metadata.sourceId"),
  submitted_at: t("resources.metadata.submitted"),
  started_at: t("resources.metadata.started"),
  message_count: t("resources.metadata.messages"),
  interval_seconds: t("resources.metadata.interval"),
  result: t("resources.metadata.result"),
};

export function resourceMetadataRows(resource: SessionResource): [string, string][] {
  const metadata = resource.metadata;
  const rows: [string, string][] = [];
  for (const key of Object.keys(metadata)) {
    if (key === "attach_url") continue;
    rows.push([
      RESOURCE_METADATA_LABELS[key] ?? key,
      resourceMetadataValueLabel(key, metadata[key]),
    ]);
  }
  return rows;
}
