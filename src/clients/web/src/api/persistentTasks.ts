import type { APIResponse } from "../types/backend";
import type { PersistentTaskChangedDTO, PersistentTaskConfigurationDTO, PersistentTaskDTO, PersistentTaskListDTO, PersistentTaskRunDTO, PersistentTaskRunSummaryDTO, PersistentTaskRunListDTO, PersistentTaskRunDetailDTO, TaskRuntimeStatusDTO, TaskThreadTargetDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { validatePersistentTaskChangedDTO, validatePersistentTaskListDTO } from "../../../../shared/sseRuntimeValidators.js";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "../sse/sseClient";
import { validateSseError } from "../sse/sseRuntimeSchemas";
import { gatewayWorkspaceRequestHeaders, requestGatewayResponse, requestJson, unwrapApiData } from "./http";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";

export type { PersistentTaskChangedDTO, PersistentTaskConfigurationDTO, PersistentTaskDTO, PersistentTaskRunDTO, PersistentTaskRunSummaryDTO, PersistentTaskRunListDTO, PersistentTaskRunDetailDTO, TaskRuntimeStatusDTO, TaskThreadTargetDTO };
export type PersistentTaskSnapshot = PersistentTaskListDTO & { runtime: TaskRuntimeStatusDTO };
export type TaskObservation = { snapshot: PersistentTaskSnapshot } | { changed: PersistentTaskChangedDTO };
export type TaskControl = "pause" | "resume" | "terminate";
const root = "/api/v1/persistent-tasks";

function snapshot(value: unknown): PersistentTaskSnapshot {
  if (!validatePersistentTaskListDTO(value)) throw new Error(`持久任务快照无效：${JSON.stringify(validatePersistentTaskListDTO.errors)}`);
  return value as PersistentTaskSnapshot;
}

async function request<T>(port: number, target: GatewayWorkspaceRequestTarget, path: string, signal: AbortSignal, init: RequestInit = {}): Promise<T> {
  return unwrapApiData(await requestJson<APIResponse<T>>(port, root + path, {
    ...init, signal, headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store",
  }));
}

export async function listPersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal, sessionId?: string) {
  return snapshot(await request<unknown>(port, target, sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "", signal));
}

export async function watchPersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, signal: AbortSignal, onSnapshot: (value: PersistentTaskSnapshot) => void, onChanged: (value: PersistentTaskChangedDTO) => void, sessionId?: string) {
  const response = await requestGatewayResponse(port, root + "/events" + (sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""), {
    signal, headers: { ...gatewayWorkspaceRequestHeaders(target), accept: "text/event-stream" }, cache: "no-store",
  });
  await consumeSseResponse(response, { signal, events: {
    snapshot: defineSseEvent((data, frame) => snapshot(decodeJsonSseData(data, frame)), onSnapshot),
    changed: defineSseEvent((data, frame) => {
      const value: unknown = decodeJsonSseData(data, frame);
      if (!validatePersistentTaskChangedDTO(value)) throw new Error(`持久任务更新无效：${JSON.stringify(validatePersistentTaskChangedDTO.errors)}`);
      return value as PersistentTaskChangedDTO;
    }, onChanged),
    error: defineSseEvent((data, frame) => validateSseError(decodeJsonSseData(data, frame)), (error) => { throw new Error(error.message); }),
  } });
  if (!signal.aborted) throw new Error("持久任务状态连接已关闭，请刷新重新连接");
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
