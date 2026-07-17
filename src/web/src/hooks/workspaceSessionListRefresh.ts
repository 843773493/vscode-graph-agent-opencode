import { listSessions } from "../api";
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
  return { workspaceId, generation, sessions: page.items };
}

export function isCurrentWorkspaceSessionListSnapshot(
  snapshot: WorkspaceSessionListSnapshot,
): boolean {
  return generations.get(snapshot.workspaceId) === snapshot.generation;
}
