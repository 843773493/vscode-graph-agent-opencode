import type { Session } from '../../types/backend';

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
