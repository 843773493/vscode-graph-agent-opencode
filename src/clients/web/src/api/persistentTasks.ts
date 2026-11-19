import type { APIResponse, SessionGoalSnapshotDTO } from "../types/backend";
import type { PersistentTaskChangedDTO, PersistentTaskConfigurationDTO, PersistentTaskDTO, PersistentTaskListDTO, PersistentTaskRunDTO, PersistentTaskRunSummaryDTO, PersistentTaskRunListDTO, PersistentTaskRunDetailDTO, TaskRuntimeStatusDTO, TaskThreadTargetDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { validatePersistentTaskChangedDTO, validatePersistentTaskListDTO } from "../../../../shared/sseRuntimeValidators.js";
import { decodeJsonSseData, defineSseEvent, SseProtocolError } from "../sse/sseClient";
import { consumeRealtimeSubscription } from "./stream/realtime";
import { parseSessionGoalSnapshot } from "../protocol/sessionGoal";
import { validateSseError } from "../sse/sseRuntimeSchemas";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData, HttpRequestError } from "./http";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS, sessionStreamReconnectDelay } from "../hooks/sessionEventStream/sessionEventStreamPolicy";
import { waitForReconnect } from "../hooks/sessionEventStream/waitForReconnect";

export type { PersistentTaskChangedDTO, PersistentTaskConfigurationDTO, PersistentTaskDTO, PersistentTaskRunDTO, PersistentTaskRunSummaryDTO, PersistentTaskRunListDTO, PersistentTaskRunDetailDTO, TaskRuntimeStatusDTO, TaskThreadTargetDTO };
export type PersistentTaskSnapshot = Omit<PersistentTaskListDTO, "session_goal"> & { runtime: TaskRuntimeStatusDTO; session_goal?: SessionGoalSnapshotDTO | null };
export type TaskObservation = { snapshot: PersistentTaskSnapshot } | { changed: PersistentTaskChangedDTO };
export type TaskControl = "pause" | "resume" | "terminate";
const root = "/api/v1/persistent-tasks";

function snapshot(value: unknown, sessionId?: string): PersistentTaskSnapshot {
  if (!validatePersistentTaskListDTO(value)) throw new SseProtocolError(`持久任务快照无效：${JSON.stringify(validatePersistentTaskListDTO.errors)}`);
  const result = value as PersistentTaskSnapshot;
  if (sessionId) {
    if (!result.session_goal) throw new SseProtocolError("会话持久任务快照缺少 Goal 槽位及清空版本");
    parseSessionGoalSnapshot(result.session_goal, sessionId);
  }
  return result;
}

class TaskStreamConnectionError extends Error {}

async function request<T>(port: number, target: GatewayWorkspaceRequestTarget, path: string, signal: AbortSignal, init: RequestInit = {}): Promise<T> {
  return unwrapApiData(await requestJson<APIResponse<T>>(port, root + path, {
    ...init, signal, headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store",
  }));
}

export async function listPersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal, sessionId?: string) {
  return snapshot(await request<unknown>(port, target, sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "", signal), sessionId);
}

async function streamPersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal, onSnapshot: (value: PersistentTaskSnapshot) => void, onChanged: (value: PersistentTaskChangedDTO) => void, sessionId?: string) {
  await consumeRealtimeSubscription(port, () => ({ path: root + '/events' + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''), headers: gatewayWorkspaceRequestHeaders(target) }), { signal, events: {
    snapshot: defineSseEvent((data, frame) => snapshot(decodeJsonSseData(data, frame), sessionId), onSnapshot),
    changed: defineSseEvent((data, frame) => {
      const value: unknown = decodeJsonSseData(data, frame);
      if (!validatePersistentTaskChangedDTO(value)) throw new SseProtocolError(`持久任务更新无效：${JSON.stringify(validatePersistentTaskChangedDTO.errors)}`);
      return value as PersistentTaskChangedDTO;
    }, onChanged),
    error: defineSseEvent((data, frame) => validateSseError(decodeJsonSseData(data, frame)), (error) => { throw new SseProtocolError(error.message); }),
  } });
  if (!signal.aborted) throw new TaskStreamConnectionError("持久任务状态连接已关闭");
}

/** 工作区列表和会话 Goal 使用相同恢复规则；每次重连由服务端重发权威快照。 */
export async function watchPersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal, onSnapshot: (value: PersistentTaskSnapshot) => void, onChanged: (value: PersistentTaskChangedDTO) => void, sessionId?: string) {
  let attempt = 0;
  while (!signal.aborted) {
    try {
      await streamPersistentTasks(port, target, signal, value => { onSnapshot(value); attempt = 0; }, onChanged, sessionId);
    } catch (cause) {
      if (signal.aborted) return;
      const retryable = cause instanceof TaskStreamConnectionError || cause instanceof TypeError
        || cause instanceof HttpRequestError && cause.status >= 500;
      if (!retryable || attempt >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS) throw cause;
      await waitForReconnect(signal, sessionStreamReconnectDelay(attempt++));
    }
  }
}

export function createPersistentTask(port: number, target: GatewayWorkspaceRequestTarget, configuration: PersistentTaskConfigurationDTO, idempotencyKey: string, signal: AbortSignal) {
  return request<PersistentTaskDTO>(port, target, "", signal, { method: "POST", body: JSON.stringify({ configuration, idempotency_key: idempotencyKey }) });
}

export function updatePersistentTask(port: number, target: GatewayWorkspaceRequestTarget, task: PersistentTaskDTO, configuration: PersistentTaskConfigurationDTO, signal: AbortSignal) {
  return request<PersistentTaskDTO>(port, target, `/${encodeURIComponent(task.task_id)}`, signal, { method: "PUT", body: JSON.stringify({ expected_revision: task.revision, configuration }) });
}

export function controlPersistentTask(port: number, target: GatewayWorkspaceRequestTarget, task: PersistentTaskDTO, action: TaskControl, signal: AbortSignal) {
  return request<PersistentTaskDTO>(port, target, `/${encodeURIComponent(task.task_id)}/control`, signal, { method: "POST", body: JSON.stringify({ expected_revision: task.revision, action }) });
}

export function checkPersistentTask(port: number, target: GatewayWorkspaceRequestTarget, task: PersistentTaskDTO, checkOnly: boolean, signal: AbortSignal) {
  return request<PersistentTaskRunDTO>(port, target, `/${encodeURIComponent(task.task_id)}/checks`, signal, { method: "POST", body: JSON.stringify({ expected_revision: task.revision, idempotency_key: crypto.randomUUID(), check_only: checkOnly }) });
}

export function listPersistentTaskRuns(port: number, target: GatewayWorkspaceRequestTarget, taskId: string, signal: AbortSignal, before?: string) {
  return request<PersistentTaskRunListDTO>(port, target, `/${encodeURIComponent(taskId)}/runs${before ? `?before=${encodeURIComponent(before)}` : ""}`, signal);
}

export function inspectPersistentTaskRun(port: number, target: GatewayWorkspaceRequestTarget, taskId: string, runId: string, signal: AbortSignal) {
  return request<PersistentTaskRunDetailDTO>(port, target, `/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}`, signal);
}
