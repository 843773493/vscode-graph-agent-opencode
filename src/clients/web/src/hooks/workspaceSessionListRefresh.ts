import { getSession, listSessions } from "../api";
import { listSessionCatalogChildren } from "../api/sessionCatalog";
import type { Session } from "../types/backend";

const generations = new Map<string, number>();
const inFlightRequests = new Map<string, Promise<WorkspaceSessionListSnapshot>>();

function workspaceSessionScopeKey(apiPort: number, workspaceId: string): string {
  return `${apiPort}:${workspaceId}`;
}

export interface WorkspaceSessionListSnapshot {
  apiPort: number;
  workspaceId: string;
  generation: number;
  sessions: Session[];
}

export async function fetchWorkspaceSessionListSnapshot(
  apiPort: number,
  workspaceId: string,
  options: { force?: boolean } = {},
): Promise<WorkspaceSessionListSnapshot> {
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
    if (page.items.length > 0) {
      return { apiPort, workspaceId, generation, sessions: page.items };
    }

    // 会话目录索引是会话位置的权威来源。工作区后端刚恢复或旧版本迁移后，
    // /sessions 可能暂时返回空页，但目录索引已经可读；此时回填根级会话的
    // 完整 DTO，避免刷新后把有效导航误投影成“暂无会话”。
    const catalogPage = await listSessionCatalogChildren(apiPort, workspaceId);
    const catalogSessionIds = catalogPage.items
      .filter((node) => node.kind === "session" && node.session_id)
      .map((node) => node.session_id as string);
    const catalogSessions = await Promise.all(
      catalogSessionIds.map((sessionId) => getSession(apiPort, sessionId, workspaceId)),
    );
    return { apiPort, workspaceId, generation, sessions: catalogSessions };
  })();
  const trackedRequest = request.finally(() => {
    if (inFlightRequests.get(scopeKey) === trackedRequest) {
      inFlightRequests.delete(scopeKey);
    }
  });
  inFlightRequests.set(scopeKey, trackedRequest);
  return await trackedRequest;
}

export function isCurrentWorkspaceSessionListSnapshot(
  snapshot: WorkspaceSessionListSnapshot,
): boolean {
  return generations.get(workspaceSessionScopeKey(snapshot.apiPort, snapshot.workspaceId))
    === snapshot.generation;
}
