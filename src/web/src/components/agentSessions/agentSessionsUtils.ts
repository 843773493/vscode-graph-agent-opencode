import type { GatewayWorkspace, Session } from '../../types/backend';

export type SessionFilterMode = 'all' | 'current' | 'attachments' | 'agent' | 'named';
export type SessionSortMode = 'created' | 'updated';
export type SessionGroupingMode = 'workspace' | 'time';

export interface SessionSection {
  id: string;
  label: string;
  sessions: Session[];
  totalCount: number;
  showMoreCount: number;
}

export interface VisibleWorkspaceNode {
  workspace: GatewayWorkspace;
  depth: number;
}

export const WORKSPACE_SECTION_RECENT_LIMIT = 10;
const RECENT_SECTION_DAYS = 7;

function sessionSortTime(session: Session, sortMode: SessionSortMode): number {
  const value = sortMode === 'updated' ? session.updated_at : session.created_at;
  const time = new Date(value || '').getTime();
  return Number.isFinite(time) ? time : 0;
}

export function sortSessions(sessions: Session[], sortMode: SessionSortMode): Session[] {
  return [...sessions].sort((a, b) => sessionSortTime(b, sortMode) - sessionSortTime(a, sortMode));
}

function workspaceSectionLabel(workspaceId: string, workspaceName: string): string {
  if (workspaceName) {
    return workspaceName;
  }
  return workspaceId || 'workspace';
}

export function buildWorkspaceSections(
  sessions: Session[],
  workspaceName: string,
  capped: boolean,
): SessionSection[] {
  const groups = new Map<string, Session[]>();
  for (const session of sessions) {
    const workspaceId = session.workspace_id || 'workspace';
    groups.set(workspaceId, [...(groups.get(workspaceId) ?? []), session]);
  }

  return [...groups.entries()].map(([workspaceId, groupSessions]) => {
    const visibleSessions =
      capped && groupSessions.length > WORKSPACE_SECTION_RECENT_LIMIT
        ? groupSessions.slice(0, WORKSPACE_SECTION_RECENT_LIMIT)
        : groupSessions;
    return {
      id: `workspace:${workspaceId}`,
      label: workspaceSectionLabel(workspaceId, workspaceName),
      sessions: visibleSessions,
      totalCount: groupSessions.length,
      showMoreCount: groupSessions.length - visibleSessions.length,
    };
  });
}

export function buildTimeSections(sessions: Session[], sortMode: SessionSortMode): SessionSection[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfRecent = startOfToday - RECENT_SECTION_DAYS * 86_400_000;
  const recent: Session[] = [];
  const older: Session[] = [];

  for (const session of sessions) {
    if (sessionSortTime(session, sortMode) >= startOfRecent && recent.length < WORKSPACE_SECTION_RECENT_LIMIT) {
      recent.push(session);
    } else {
      older.push(session);
    }
  }

  const sections: SessionSection[] = [];
  if (recent.length > 0) {
    sections.push({
      id: 'time:recent',
      label: '最近',
      sessions: recent,
      totalCount: recent.length,
      showMoreCount: 0,
    });
  }
  if (older.length > 0) {
    sections.push({
      id: 'time:older',
      label: '更早',
      sessions: older,
      totalCount: older.length,
      showMoreCount: 0,
    });
  }
  return sections;
}

export function reorderWorkspaceIds(
  workspaceIds: string[],
  sourceWorkspaceId: string,
  targetWorkspaceId: string,
  position: 'before' | 'after',
): string[] {
  if (sourceWorkspaceId === targetWorkspaceId) {
    return workspaceIds;
  }
  const remainingIds = workspaceIds.filter(
    (workspaceId) => workspaceId !== sourceWorkspaceId,
  );
  const targetIndex = remainingIds.indexOf(targetWorkspaceId);
  if (targetIndex < 0) {
    throw new Error(`工作区排序目标不存在: ${targetWorkspaceId}`);
  }
  const insertIndex = position === 'after' ? targetIndex + 1 : targetIndex;
  return [
    ...remainingIds.slice(0, insertIndex),
    sourceWorkspaceId,
    ...remainingIds.slice(insertIndex),
  ];
}

export function buildVisibleWorkspaceTree(
  workspaces: GatewayWorkspace[],
  collapsedWorkspaceIds: Set<string>,
): VisibleWorkspaceNode[] {
  const workspaceIds = new Set(
    workspaces.map((workspace) => workspace.workspace_id),
  );
  const childrenByParent = new Map<string, GatewayWorkspace[]>();
  const roots: GatewayWorkspace[] = [];
  for (const workspace of workspaces) {
    const parentWorkspaceId = workspace.parent_workspace_id ?? null;
    if (!parentWorkspaceId || !workspaceIds.has(parentWorkspaceId)) {
      roots.push(workspace);
      continue;
    }
    childrenByParent.set(parentWorkspaceId, [
      ...(childrenByParent.get(parentWorkspaceId) ?? []),
      workspace,
    ]);
  }

  const result: VisibleWorkspaceNode[] = [];
  const visited = new Set<string>();
  const visit = (
    workspace: GatewayWorkspace,
    depth: number,
    visible: boolean,
  ) => {
    if (visited.has(workspace.workspace_id)) {
      throw new Error(
        `工作区父子关系形成循环: ${workspace.workspace_id}`,
      );
    }
    visited.add(workspace.workspace_id);
    if (visible) {
      result.push({ workspace, depth });
    }
    const childrenVisible =
      visible && !collapsedWorkspaceIds.has(workspace.workspace_id);
    for (const child of childrenByParent.get(workspace.workspace_id) ?? []) {
      visit(child, depth + 1, childrenVisible);
    }
  };

  for (const root of roots) {
    visit(root, 0, true);
  }
  if (visited.size !== workspaces.length) {
    const unresolved = workspaces.find(
      (workspace) => !visited.has(workspace.workspace_id),
    );
    throw new Error(
      `工作区父子关系无法解析: ${unresolved?.workspace_id ?? "unknown"}`,
    );
  }
  return result;
}
