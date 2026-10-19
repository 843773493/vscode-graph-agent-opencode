import { listSessions } from "../../api";
import type { Session } from "../../types/backend";
import { trackInFlightRequest } from "../runtime/inFlightRequests";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";

const generations = new Map<string, number>();
const inFlightRequests = new Map<string, Promise<WorkspaceSessionListSnapshot>>();

function workspaceSessionScopeKey(apiPort: number, workspaceId: string): string {
  return `${apiPort}:${workspaceId}`;
}

/** 已提交删除使在途旧列表失效；后续读取必须建立新快照，不能复用删除前请求。 */
export function invalidateWorkspaceSessionListSnapshot(apiPort: number, workspaceId: string): void {
  const scopeKey = workspaceSessionScopeKey(apiPort, workspaceId);
  generations.set(scopeKey, (generations.get(scopeKey) ?? 0) + 1);
  inFlightRequests.delete(scopeKey);
}

export interface WorkspaceSessionListSnapshot {
  apiPort: number;
  workspaceId: string;
  generation: number;
  sessions: Session[];
  complete: boolean;
}

export async function fetchWorkspaceSessionListSnapshot(
  apiPort: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  options: { force?: boolean } = {},
): Promise<WorkspaceSessionListSnapshot> {
  const workspaceId = workspaceTarget.workspace_id;
  const scopeKey = workspaceSessionScopeKey(apiPort, workspaceId);
  const inFlight = inFlightRequests.get(scopeKey);
  if (inFlight) {
    if (!options.force) {
      return await inFlight;
    }
    // 显式刷新必须读取突变后的快照，但不能和已经在路上的普通刷新并发。
    // 等待它结束后再启动一次；多个 force 调用也会共享这次后续刷新。
    await inFlight;
    const replacement = inFlightRequests.get(scopeKey);
    if (replacement) {
      return await replacement;
    }
  }
  const generation = (generations.get(scopeKey) ?? 0) + 1;
  generations.set(scopeKey, generation);
  const request = (async (): Promise<WorkspaceSessionListSnapshot> => {
    const page = await listSessions(apiPort, workspaceId);
    if (typeof page.has_more !== "boolean") {
      throw new Error("会话列表响应缺少 has_more，无法判断本页是否完整");
    }
    return { apiPort, workspaceId, generation, sessions: page.items, complete: !page.has_more };
  })();
  trackInFlightRequest(inFlightRequests, scopeKey, request);
  return await request;
}

export function isCurrentWorkspaceSessionListSnapshot(
  snapshot: WorkspaceSessionListSnapshot,
): boolean {
  return generations.get(workspaceSessionScopeKey(snapshot.apiPort, snapshot.workspaceId))
    === snapshot.generation;
}
