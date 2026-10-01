import type { APIResponse } from "../../types/backend";
import { requestJson, unwrapApiData, workspaceHeader } from "../http";

/**
 * 会话目录异步 mutation 协议客户端（OpenSpec 8.1-H）。
 *
 * 同步 PATCH/PUT/DELETE 目录端点不得成为第二业务 writer：本模块是目录写协议的
 * **唯一** HTTP 入口，所有导航写操作都先经本地 outbox 持久化，再走这里的批量入队。
 *
 * TODO(8.1-G/8.1-H 后端接线)：URL 按 design.md 与 tasks.md 原文写死，但工作区后端
 * 尚未注册这些路由（当前 app/api/session_navigation.py 仍是同步 mutation 端点）。
 * 后端落地前调用这些函数会收到 404，属于预期；不得为此另写兼容分支或第二路径。
 */

export type NavigationMutationKind =
  | "create_folder"
  | "rename_node"
  | "move_node"
  | "delete_folder"
  | "delete_session";

/** operation 状态闭集；`dependency_failed` 只能由 `queued` 直接进入。 */
export type NavigationMutationState =
  | "queued"
  | "running"
  | "committed"
  | "rejected"
  | "cancelled"
  | "dependency_failed";

/** 终态闭集：进入后只保留 compact tombstone，迟到重放返回原 terminal。 */
export const NAVIGATION_MUTATION_TERMINAL_STATES: readonly NavigationMutationState[] = [
  "committed",
  "rejected",
  "cancelled",
  "dependency_failed",
];

export function isNavigationMutationTerminalState(
  state: NavigationMutationState,
): boolean {
  return NAVIGATION_MUTATION_TERMINAL_STATES.includes(state);
}

/** 批量入队信封中的一个 typed intent（字段名与后端 DTO 逐字一致）。 */
export interface SessionCatalogOperationIntent {
  client_operation_id: string;
  client_sequence: number;
  kind: NavigationMutationKind;
  base_catalog_revision: number;
  expected_revision?: number | null;
  target_node_id?: string | null;
  created_by_operation_id?: string | null;
  name?: string | null;
  parent_node_id?: string | null;
  recursive?: boolean;
  depends_on?: readonly string[];
}

export interface SessionCatalogOperationReceipt {
  operation_id: string;
  client_sequence: number;
  queue_seq: number;
  kind: NavigationMutationKind;
  state: NavigationMutationState;
  created_node_id: string | null;
  committed_catalog_revision: number | null;
  error_code: string | null;
  error_detail: string | null;
  pending_settlement: boolean;
  receipt_revision: number;
  updated_at: string;
}

export interface SessionCatalogEnqueueResult {
  workspace_id: string;
  accepted_count: number;
  receipts: SessionCatalogOperationReceipt[];
  created_node_ids: Record<string, string>;
}

export interface SessionCatalogOperationStatusPage {
  workspace_id: string;
  catalog_revision: number;
  items: SessionCatalogOperationReceipt[];
  /** 缺失的 operation_id：客户端据此保留 pending 并按同一 ID 重试，不直接回退。 */
  unknown_operation_ids: string[];
}

export interface SessionCatalogNavigationEvent {
  event_seq: number;
  workspace_id: string;
  operation_id: string;
  queue_seq: number;
  kind: NavigationMutationKind;
  result_state: NavigationMutationState;
  committed_catalog_revision: number | null;
  affected_node_ids: string[];
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
}

export interface SessionCatalogNavigationEventsPage {
  workspace_id: string;
  event_seq_watermark: number;
  items: SessionCatalogNavigationEvent[];
  next_cursor: string | null;
  has_more: boolean;
  cursor_gone: boolean;
}

export interface SessionCatalogSnapshot {
  workspace_id: string;
  catalog_revision: number;
  event_seq_watermark: number;
  generation: number;
}

const ENQUEUE_PATH = "/api/v1/session-catalog/operations:enqueue";
const OPERATION_STATUS_PATH = "/api/v1/session-catalog/operations";
const SNAPSHOT_PATH = "/api/v1/session-catalog/snapshot";
// navigation 事件 channel 必须与既有的 session_activity 频道
//（`/session-catalog/events`，见 api/session/sessionActivity.ts）区分开，因此不复用该前缀。
const NAVIGATION_EVENTS_PATH = "/api/v1/session-catalog/navigation-events";

/**
 * 目录写协议的唯一 HTTP 出口：一次批量入队。
 *
 * 202 只表示 durable acceptance，绝不表示目录已改变；调用方必须按返回的 receipt
 * 继续对账，不得把它当作成功。
 */
export async function enqueueSessionCatalogOperations(
  port: number,
  workspaceId: string,
  intents: readonly SessionCatalogOperationIntent[],
): Promise<SessionCatalogEnqueueResult> {
  if (intents.length === 0) {
    throw new Error("会话目录批量入队至少需要一个 intent");
  }
  return unwrapApiData(await requestJson<APIResponse<SessionCatalogEnqueueResult>>(
    port,
    ENQUEUE_PATH,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ intents }),
    },
  ));
}

/** 按精确 operation ID 查询 durable 状态；未知 ID 由调用方显式保留为 unknown。 */
export async function querySessionCatalogOperationStatus(
  port: number,
  workspaceId: string,
  operationIds: readonly string[],
): Promise<SessionCatalogOperationStatusPage> {
  if (operationIds.length === 0) {
    throw new Error("会话目录状态查询至少需要一个 operation_id");
  }
  const query = new URLSearchParams();
  for (const operationId of operationIds) {
    query.append("operation_id", operationId);
  }
  return unwrapApiData(await requestJson<APIResponse<SessionCatalogOperationStatusPage>>(
    port,
    `${OPERATION_STATUS_PATH}?${query.toString()}`,
    { headers: workspaceHeader(workspaceId) },
  ));
}

/** revision-pinned catalog snapshot：取同一已提交 revision 与事件水位。 */
export async function fetchSessionCatalogSnapshot(
  port: number,
  workspaceId: string,
): Promise<SessionCatalogSnapshot> {
  return unwrapApiData(await requestJson<APIResponse<SessionCatalogSnapshot>>(
    port,
    SNAPSHOT_PATH,
    { headers: workspaceHeader(workspaceId) },
  ));
}

/** 拉取 `event_seq > after` 的 navigation 终态事件页。 */
export async function listSessionCatalogNavigationEvents(
  port: number,
  workspaceId: string,
  options: { after: number; cursor?: string | null },
): Promise<SessionCatalogNavigationEventsPage> {
  if (!Number.isInteger(options.after) || options.after < 0) {
    throw new Error(`navigation 事件 cursor 必须是非负整数: ${options.after}`);
  }
  const query = new URLSearchParams({ after: String(options.after) });
  if (options.cursor) query.set("cursor", options.cursor);
  return unwrapApiData(await requestJson<APIResponse<SessionCatalogNavigationEventsPage>>(
    port,
    `${NAVIGATION_EVENTS_PATH}?${query.toString()}`,
    { headers: workspaceHeader(workspaceId) },
  ));
}
