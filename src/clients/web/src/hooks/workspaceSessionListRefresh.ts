import { getSession, listSessions } from "../api";
import { listSessionCatalogChildren } from "../api/sessionCatalog";
import type { Session } from "../types/backend";

const generations = new Map<string, number>();

export interface WorkspaceSessionListSnapshot {
  workspaceId: string;
  generation: number;
  sessions: Session[];
}

export async function fetchWorkspaceSessionListSnapshot(
  apiPort: number,
  workspaceId: string,
): Promise<WorkspaceSessionListSnapshot> {
  const generation = (generations.get(workspaceId) ?? 0) + 1;
  generations.set(workspaceId, generation);
  const page = await listSessions(apiPort, workspaceId);
  if (page.items.length > 0) {
    return { workspaceId, generation, sessions: page.items };
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
  return { workspaceId, generation, sessions: catalogSessions };
}

export function isCurrentWorkspaceSessionListSnapshot(
  snapshot: WorkspaceSessionListSnapshot,
): boolean {
  return generations.get(snapshot.workspaceId) === snapshot.generation;
}
