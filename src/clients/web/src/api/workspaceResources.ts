import type { APIResponse } from "../types/backend";
import type { ResourceThreadRef, WorkspaceResourceDTO, WorkspaceResourceListDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { requestJson, unwrapApiData, workspaceHeader } from "./http";
import { consumeRealtimeSubscription } from "./stream/realtime";
import { decodeJsonSseData, defineSseEvent } from "../sse/sseClient";
import { validateResourceChanges } from "../../../../shared/sseRuntime.js";

export type { ResourceThreadRef, WorkspaceResourceDTO };
export type ResourceAction = "promote" | "attach" | "detach" | "stop" | "delete";
export type WorkspaceConnectionKind = "terminal" | "browser";

interface TerminalObserver { changed: (ids: string[] | null) => void; finish: (error?: unknown) => void }
interface TerminalObservation { controller: AbortController; observers: Set<TerminalObserver>; connected: boolean }
const terminalObservations = new Map<string, TerminalObservation>();

/** 相同工作区共享一次终端失效订阅；它只是逻辑订阅，物理连接仍归实时入口。 */
export function observeTerminalResources(port: number, workspaceId: string, onChanged: (resourceIds: string[] | null) => void, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  const key = JSON.stringify([port, workspaceId]);
  let source = terminalObservations.get(key);
  const start = !source;
  if (!source) {
    source = { controller: new AbortController(), observers: new Set(), connected: false };
    terminalObservations.set(key, source);
  }
  const current = source;
  return new Promise<void>((resolve, reject) => {
    const observer: TerminalObserver = { changed: onChanged, finish: error => {
      if (!current.observers.delete(observer)) return;
      signal.removeEventListener('abort', abort);
      if (!current.observers.size) {
        current.controller.abort();
        if (terminalObservations.get(key) === current) terminalObservations.delete(key);
      }
      if (error === undefined) resolve(); else reject(error);
    } };
    const abort = () => observer.finish();
    current.observers.add(observer);
    signal.addEventListener('abort', abort, { once: true });
    if (current.connected) {
      try { onChanged(null); } catch (error) { observer.finish(error); }
    }
    if (start) void consumeRealtimeSubscription(port, () => ({ path: '/api/v1/resources/events', headers: workspaceHeader(workspaceId) }), {
      signal: current.controller.signal, onConnected: () => { current.connected = true; },
      events: { resources_changed: defineSseEvent((data, frame) => validateResourceChanges(decodeJsonSseData(data, frame)), ids => {
        for (const listener of [...current.observers]) {
          try { listener.changed(ids); } catch (error) { listener.finish(error); }
        }
      }) },
    }).then(() => { for (const listener of [...current.observers]) listener.finish(); },
      error => { for (const listener of [...current.observers]) listener.finish(error); });
  });
}

export async function listWorkspaceResources(port: number, workspaceId: string, kind: WorkspaceConnectionKind, signal?: AbortSignal): Promise<WorkspaceResourceListDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceListDTO>>(
    port, `/api/v1/resources?kind=${kind}`, { headers: workspaceHeader(workspaceId), signal },
  ));
}

export async function getWorkspaceResource(port: number, workspaceId: string, resourceId: string, signal?: AbortSignal): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, `/api/v1/resources/${encodeURIComponent(resourceId)}`, { headers: workspaceHeader(workspaceId), signal },
  ));
}

export async function createWorkspaceTerminal(
  port: number, workspaceId: string, thread: ResourceThreadRef,
  retentionScope: "thread" | "workspace" = "thread",
): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, "/api/v1/resources/terminals", {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ session_id: thread.session_id, thread_id: thread.thread_id, retention_scope: retentionScope, title: "用户终端" }),
    },
  ));
}

export async function createWorkspaceBrowser(
  port: number, workspaceId: string, thread: ResourceThreadRef,
  retentionScope: "thread" | "workspace" = "thread",
): Promise<WorkspaceResourceDTO> {
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, "/api/v1/resources/browsers", {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({
        session_id: thread.session_id, thread_id: thread.thread_id, retention_scope: retentionScope,
        title: "用户浏览器", url: "about:blank", viewport: { width: 1280, height: 800 },
      }),
    },
  ));
}

export async function controlWorkspaceResource(
  port: number, workspaceId: string, resource: WorkspaceResourceDTO,
  action: ResourceAction, thread?: ResourceThreadRef,
): Promise<WorkspaceResourceDTO> {
  if (action === "attach" && !thread) throw new Error("关联资源必须提供明确的 session_id 与 thread_id");
  return unwrapApiData(await requestJson<APIResponse<WorkspaceResourceDTO>>(
    port, `/api/v1/resources/${encodeURIComponent(resource.resource_id)}/${action}`, {
      method: "POST", headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ expected_revision: resource.owner_revision, thread }),
    },
  ));
}
