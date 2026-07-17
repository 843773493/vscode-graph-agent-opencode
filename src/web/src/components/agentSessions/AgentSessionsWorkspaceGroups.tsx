import type { ReactNode } from 'react';
import type React from 'react';
import type { GatewayWorkspace, Session } from '../../types/backend';
import AgentSessionsSessionTree from './AgentSessionsSessionTree';
import { sortSessions, type SessionSortMode } from './agentSessionsUtils';

interface WorkspaceDropTarget {
  workspaceId: string;
  position: 'before' | 'after';
}

interface AgentSessionsWorkspaceGroupsProps {
  gatewayWorkspaces: GatewayWorkspace[];
  activeGatewayWorkspaceId: string | null;
  sessionsByWorkspace: Map<string, Session[]>;
  sortMode: SessionSortMode;
  collapsedWorkspaceIds: Set<string>;
  draggingWorkspaceId: string | null;
  workspaceDropTarget: WorkspaceDropTarget | null;
  workspaceSwitching: boolean;
  removingGatewayWorkspaceIds: Set<string>;
  currentSessionId: string;
  onToggleWorkspaceSection: (workspaceId: string) => void;
  onWorkspaceDragStart: (event: React.DragEvent<HTMLElement>, workspaceId: string) => void;
  onWorkspaceDragOver: (event: React.DragEvent<HTMLElement>, workspaceId: string) => void;
  onWorkspaceDrop: (event: React.DragEvent<HTMLElement>, workspaceId: string) => void;
  onWorkspaceDragEnd: () => void;
  onOpenWorkspaceMenu: (workspace: GatewayWorkspace, x: number, y: number) => void;
  onCreateWorkspaceSession: (workspace: GatewayWorkspace) => void;
  onSelectWorkspaceSession: (workspaceId: string, sessionId: string) => void;
  onOpenSessionMenu: (
    session: Session,
    workspaceId: string,
    x: number,
    y: number,
  ) => void;
}

export function workspaceHoverTitle(workspace: GatewayWorkspace): string {
  const lines = [workspace.name, `路径：${workspace.root_path}`];
  if (workspace.connection_kind === 'ssh') {
    const { host, port, username } = workspace.remote;
    const sshTarget = host
      ? `${username ? `${username}@` : ''}${host}${port ? `:${port}` : ''}`
      : '未提供主机信息';
    lines.push(`SSH：${sshTarget}`);
  }
  return lines.join('\n');
}

export default function AgentSessionsWorkspaceGroups({
  gatewayWorkspaces,
  activeGatewayWorkspaceId,
  sessionsByWorkspace,
  sortMode,
  collapsedWorkspaceIds,
  draggingWorkspaceId,
  workspaceDropTarget,
  workspaceSwitching,
  removingGatewayWorkspaceIds,
  currentSessionId,
  onToggleWorkspaceSection,
  onWorkspaceDragStart,
  onWorkspaceDragOver,
  onWorkspaceDrop,
  onWorkspaceDragEnd,
  onOpenWorkspaceMenu,
  onCreateWorkspaceSession,
  onSelectWorkspaceSession,
  onOpenSessionMenu,
}: AgentSessionsWorkspaceGroupsProps): ReactNode {
  if (gatewayWorkspaces.length === 0) {
    return null;
  }

  return (
    <section className="agent-sessions-workspace-groups" aria-label="工作区">
      {gatewayWorkspaces.map((workspace) => {
        const active = workspace.workspace_id === activeGatewayWorkspaceId;
        const removing = removingGatewayWorkspaceIds.has(workspace.workspace_id);
        const collapsed = collapsedWorkspaceIds.has(workspace.workspace_id);
        const workspaceSessions = sortSessions(
          sessionsByWorkspace.get(workspace.workspace_id) ?? [],
          sortMode,
        );
        const hoverTitle = workspaceHoverTitle(workspace);
        return (
          <section
            className={[
              'agent-sessions-workspace-section',
              draggingWorkspaceId === workspace.workspace_id ? ' dragging' : '',
              workspaceDropTarget?.workspaceId === workspace.workspace_id
                ? ` drop-${workspaceDropTarget.position}`
                : '',
            ].join('')}
            key={workspace.workspace_id}
          >
            <div
              className={`agent-sessions-workspace-row${active ? ' active' : ''}${removing ? ' removing' : ''}`}
              title={hoverTitle}
              aria-expanded={!collapsed}
              aria-grabbed={draggingWorkspaceId === workspace.workspace_id}
              draggable={!workspaceSwitching && !removing}
              onDragStart={(event) => onWorkspaceDragStart(event, workspace.workspace_id)}
              onDragOver={(event) => onWorkspaceDragOver(event, workspace.workspace_id)}
              onDragEnter={(event) => onWorkspaceDragOver(event, workspace.workspace_id)}
              onDrop={(event) => onWorkspaceDrop(event, workspace.workspace_id)}
              onDragEnd={onWorkspaceDragEnd}
              onContextMenu={(event) => {
                event.preventDefault();
                event.stopPropagation();
                if (removing) {
                  return;
                }
                onOpenWorkspaceMenu(workspace, event.clientX, event.clientY);
              }}
            >
              <button
                type="button"
                className="agent-sessions-workspace-title-button"
                title={hoverTitle}
                aria-label={`${collapsed ? '展开' : '折叠'}工作区 ${workspace.name}`}
                aria-expanded={!collapsed}
                disabled={removing}
                onClick={() => onToggleWorkspaceSection(workspace.workspace_id)}
              >
                <span className="agent-sessions-workspace-name">{workspace.name}</span>
                {workspace.status === 'offline' ? (
                  <span
                    className="agent-sessions-workspace-status"
                    title={workspace.connection_error ?? '工作区后端离线'}
                  >
                    离线
                  </span>
                ) : null}
                {removing ? (
                  <span className="agent-sessions-workspace-status removing-status">
                    正在删除
                  </span>
                ) : null}
              </button>
              <div className="agent-sessions-workspace-actions">
                <button
                  type="button"
                  className="agent-sessions-workspace-action-button"
                  title={`在 ${workspace.name} 新建会话`}
                  aria-label={`在 ${workspace.name} 新建会话`}
                  disabled={removing || (workspaceSwitching && !active)}
                  onClick={(event) => {
                    event.stopPropagation();
                    onCreateWorkspaceSession(workspace);
                  }}
                >
                  <span className="codicon codicon-add" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="agent-sessions-workspace-action-button agent-sessions-workspace-chevron-button"
                  title={collapsed ? '展开工作区会话' : '折叠工作区会话'}
                  aria-label={collapsed ? '展开工作区会话' : '折叠工作区会话'}
                  aria-expanded={!collapsed}
                  disabled={removing}
                  onClick={(event) => {
                    event.stopPropagation();
                    onToggleWorkspaceSection(workspace.workspace_id);
                  }}
                >
                  <span
                    className={`codicon ${
                      collapsed ? 'codicon-chevron-right' : 'codicon-chevron-down'
                    } agent-sessions-workspace-chevron`}
                    aria-hidden="true"
                  />
                </button>
              </div>
            </div>
            {!collapsed ? (
              workspaceSessions.length > 0 ? (
                <div className="agent-sessions-workspace-session-list">
                  <AgentSessionsSessionTree
                    sessions={workspaceSessions}
                    sortMode={sortMode}
                    currentSessionId={currentSessionId}
                    active={active}
                    onSelectSession={(sessionId) =>
                      onSelectWorkspaceSession(workspace.workspace_id, sessionId)
                    }
                    onOpenMenu={(session, x, y) =>
                      onOpenSessionMenu(session, workspace.workspace_id, x, y)
                    }
                  />
                </div>
              ) : (
                <div className="agent-sessions-workspace-empty">No chats</div>
              )
            ) : null}
          </section>
        );
      })}
    </section>
  );
}
