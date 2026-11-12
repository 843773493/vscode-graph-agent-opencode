import { formatNumber, t } from '../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { resolveCatalogSearchTarget } from "../../api/gateway/catalogSearch";
import { useState, type ReactNode } from "react";
import { useSessionResourceExplorer } from "../../hooks/session/useSessionResourceExplorer";
import type {
  GatewayWorkspace,
  Session,
  WorkspaceNavigationNode,
} from "../../types/backend";
import type {
  SessionCatalogOutboxRuntime,
  SessionCatalogOutboxSnapshot,
} from "../../hooks/session/sessionCatalogOutboxRuntime";
import type { CatalogProjectedNode } from "../../state/session/sessionCatalogProjection";
import SessionActivityIndicator from "./SessionActivityIndicator";
import {
  workspaceFailurePresentation,
  workspaceHoverTitle,
  sessionCatalogTimePresentation,
  workspaceStatusPresentation,
} from "./agentSessionsUtils";
import { sessionScopeKey } from "../../state/session/sessionScope";
import {
  sessionResourceDropTargetKey,
  type SessionResourceDropTarget,
} from "./sessionResourceDrag";
import SessionResourceOverlays, {
  type SessionFolderContextMenu,
  type SessionResourceDialog,
  type WorkspaceFolderContextMenu,
  type WorkspaceFolderEditor,
} from "./SessionResourceOverlays";
import { buildWorkspaceNavigationSyncKey } from "../../hooks/sessionResourceExplorer/resourceTreeSync";
import { useSessionResourceTreeNavigation } from "./useSessionResourceTreeNavigation";
import { errorMessage } from "../../utils/errorMessage";
import { useCatalogReveal, type CatalogRevealRequest } from "../../hooks/sessionResourceExplorer/useCatalogReveal";

interface SessionResourceExplorerProps {
  catalogReveal?: CatalogRevealRequest;
  apiPort: number;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  currentSessionId: string;
  currentSessionWorkspaceId: string | null;
  searchOpen: boolean;
  searchQuery: string;
  workspaceSwitching: boolean;
  startingWorkspaceIds: ReadonlySet<string>;
  removingWorkspaceIds: ReadonlySet<string>;
  onActivateWorkspace: (workspaceId: string) => Promise<void>;
  onSetWorkspaceParent: (
    workspaceId: string,
    parentWorkspaceId: string | null,
  ) => Promise<void>;
  onRefreshWorkspaceSessions: (workspaceId: string) => Promise<void>;
  onCreateSessionInFolder: (
    workspaceId: string,
    folderId: string,
  ) => Promise<void>;
  onSessionFolderDeleted: (
    workspaceId: string,
    deletedCurrentSession: boolean,
  ) => Promise<void>;
  catalogSyncKeys: ReadonlyMap<string, string>;
  catalogRefreshVersions: ReadonlyMap<string, number>;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime | null;
  sessionCatalogOutboxSnapshot?: SessionCatalogOutboxSnapshot;

  onSelectSession: (workspaceId: string, sessionId: string) => void | Promise<void>;
  onPinSession?: (workspaceId: string, sessionId: string) => void;
  onStatusChange: (message: string) => void;
  onOpenDiagnostic: (title: string, detail: string) => void;
  onOpenWorkspaceMenu: (workspace: GatewayWorkspace, x: number, y: number) => void;
  onOpenSessionMenu: (session: Session, workspaceId: string, x: number, y: number) => void;
  activeJobIdsBySession: ReadonlyMap<string, string>;
  unreadSessionKeys: ReadonlySet<string>;
  onRequestAddWorkspace: () => void;
  onOpenConnectionManager: () => void;
  onReconnectWorkspace: (workspaceId: string) => Promise<void>;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
}

function Chevron({ expanded }: { expanded: boolean }) {
  return (
    <span
      className={`codicon ${expanded ? "codicon-chevron-down" : "codicon-chevron-right"}`}
      aria-hidden="true"
    />
  );
}

export default function SessionResourceExplorer({
  catalogReveal,
  apiPort,
  workspaces,
  activeWorkspaceId,
  currentSessionId,
  currentSessionWorkspaceId,
  searchOpen,
  searchQuery,
  workspaceSwitching,
  startingWorkspaceIds,
  removingWorkspaceIds,
  onActivateWorkspace,
  onSetWorkspaceParent,
  onRefreshWorkspaceSessions,
  onCreateSessionInFolder,
  onSessionFolderDeleted,
  catalogSyncKeys,
  catalogRefreshVersions,
  sessionCatalogOutboxRuntime,
  sessionCatalogOutboxSnapshot,

  onSelectSession,
  onPinSession,
  onStatusChange,
  onOpenDiagnostic,
  onOpenWorkspaceMenu,
  onOpenSessionMenu,
  activeJobIdsBySession,
  unreadSessionKeys,
  onRequestAddWorkspace,
  onOpenConnectionManager,
  onReconnectWorkspace,
  onStartWorkspace,
}: SessionResourceExplorerProps): ReactNode {
  const [actionError, setActionError] = useState<{
    title: string;
    detail: string;
    workspaceId?: string;
    retryActivation?: boolean;
  } | null>(null);
  const [folderMenu, setFolderMenu] = useState<SessionFolderContextMenu | null>(null);
  const [workspaceFolderMenu, setWorkspaceFolderMenu] =
    useState<WorkspaceFolderContextMenu | null>(null);
  const [workspaceFolderEditor, setWorkspaceFolderEditor] =
    useState<WorkspaceFolderEditor | null>(null);
  const [resourceDialog, setResourceDialog] = useState<SessionResourceDialog | null>(null);
  const [recoveringWorkspaceIds, setRecoveringWorkspaceIds] =
    useState<Set<string>>(new Set());
  const explorer = useSessionResourceExplorer({
    apiPort,
    workspaces,
    activeWorkspaceId,
    searchOpen,
    searchQuery,
    currentSessionId,
    currentSessionWorkspaceId,
    workspaceNavigationSyncKey: buildWorkspaceNavigationSyncKey(workspaces),
    catalogSyncKeys,
    catalogRefreshVersions,
    sessionCatalogOutboxRuntime,
    sessionCatalogOutboxSnapshot,
  });
  const workspacesById = new Map(
    workspaces.map((workspace) => [gatewayWorkspaceKey(workspace), workspace]),
  );
  const catalogTarget = useCatalogReveal(apiPort, catalogReveal, explorer);
  const navigationNodes = explorer.navigation?.nodes ?? [];
  // 导航已加载但没有任何节点（也没有内联编辑器在创建）时，空树会看起来像坏掉了。
  // 存在 navigationError 时优先展示错误卡：此时旧快照可能恰好是空的，
  // 不能再并列宣称「还没有工作区」，否则用户分不清是空目录还是加载失败。
  const navigationEmpty = explorer.navigation !== null
    && explorer.navigation !== undefined
    && !explorer.navigationError
    && navigationNodes.length === 0
    && workspaceFolderEditor?.mode !== "create";

  const handleError = (
    title: string,
    error: unknown,
    scope: { workspaceId?: string; retryActivation?: boolean } = {},
  ) => {
    setActionError({ title, detail: errorMessage(error), ...scope });
    onStatusChange(t('navigation.diagnostic.openHint', title));
  };
  const openDiagnostic = (title: string, detail: string, workspaceId?: string) => {
    const workspace = workspaceId ? workspacesById.get(workspaceId) : null;
    onOpenDiagnostic(workspaceId ? `${workspace?.name ?? workspaceId} · ${title}` : title, detail);
  };

  const activateWorkspace = async (workspaceId: string) => {
    setActionError(null);
    try {
      await onActivateWorkspace(workspaceId);
    } catch (error) {
      handleError(t('navigation.workspace.switchFailed'), error, { workspaceId, retryActivation: true });
    }
  };

  const {
    navigationChildren,
    dragItem,
    dropTargetClass,
    clearDropTarget,
    startDrag,
    clearDrag,
    handleDragOver,
    handleDrop,
    openingSession,
    openSessionNode,
  } = useSessionResourceTreeNavigation({
    explorer,
    navigationNodes,
    handleError,
    onSetWorkspaceParent,
    onRefreshWorkspaceSessions,
    onStatusChange,
    onSelectSession,
  });

  const refreshSessionCatalogBranchInBackground = (
    workspaceId: string,
    parentNodeId: string | null,
  ) => {
    void explorer.loadBranch(workspaceId, parentNodeId).catch(() => {
      // loadBranch 已将失败写入对应分支，恢复入口与详情由分支统一展示。
    });
  };

  const refreshWorkspaceNavigationInBackground = () => {
    void explorer.refreshNavigation().catch(() => {
      // 导航加载状态已有错误出口，不能再弹出一份操作错误。
    });
  };
  const refreshResourceTreeInBackground = () => {
    void explorer.refreshResourceTree().catch(() => {
      // 导航与目录请求各自保存失败状态，由对应节点提供重试与诊断入口。
      onStatusChange(t('navigation.catalog.refreshNodesFailed'));
    });
  };

  const workspaceRefsByWorkspaceId = new Map(
    navigationNodes
      .filter((node) => node.kind === "workspace_ref" && node.workspace)
      .map((node) => [gatewayWorkspaceKey(node.workspace!), node]),
  );
  const workspaceChildren = new Map<string, WorkspaceNavigationNode[]>();
  for (const workspace of workspaces) {
    const parentWorkspaceId = nullableGatewayWorkspaceKey(workspace.parent_workspace);
    const reference = workspaceRefsByWorkspaceId.get(gatewayWorkspaceKey(workspace));
    if (!parentWorkspaceId || !reference || !workspaceRefsByWorkspaceId.has(parentWorkspaceId)) {
      continue;
    }
    workspaceChildren.set(parentWorkspaceId, [
      ...(workspaceChildren.get(parentWorkspaceId) ?? []),
      reference,
    ]);
  }
  for (const children of workspaceChildren.values()) {
    children.sort((left, right) => left.position - right.position || left.name.localeCompare(right.name));
  }

  const submitWorkspaceFolderEditor = (editor: WorkspaceFolderEditor) => {
    const name = editor.value.trim();
    if (!name) {
      handleError(t('navigation.workspace.saveFolderFailed'), new Error(t('navigation.name.required')));
      return;
    }
    setWorkspaceFolderEditor(null);
    const operation = editor.mode === "create"
      ? explorer.createWorkspaceFolder(name, editor.parentNodeId)
      : explorer.renameWorkspaceFolder(editor.nodeId as string, name);
    void operation
      .then(() => onStatusChange(editor.mode === "create" ? t('navigation.workspace.folderCreated', name) : t('navigation.workspace.folderRenamed', name)))
      .catch((error) => {
        setWorkspaceFolderEditor(editor);
        handleError(t('navigation.workspace.saveFolderFailed'), error);
      });
  };

  const renderWorkspaceFolderEditor = (
    editor: WorkspaceFolderEditor,
    depth: number,
  ): ReactNode => (
    <li className="session-resource-inline-editor-row">
      <div className="session-resource-row workspace-folder" style={{ paddingLeft: `${depth * 14 + 8}px` }}>
        <span className="session-resource-chevron-spacer" />
        <span className="codicon codicon-folder" aria-hidden="true" />
        <input
          autoFocus
          className="session-resource-inline-editor"
          aria-label={editor.mode === "create" ? t('navigation.workspace.newFolderName') : t('navigation.workspace.renameFolderName')}
          value={workspaceFolderEditor?.value ?? editor.value}
          onChange={(event) => setWorkspaceFolderEditor({ ...editor, value: event.target.value })}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submitWorkspaceFolderEditor({ ...editor, value: event.currentTarget.value });
            } else if (event.key === "Escape") {
              event.preventDefault();
              setWorkspaceFolderEditor(null);
            }
          }}
          onBlur={() => setWorkspaceFolderEditor(null)}
        />
      </div>
    </li>
  );

  const renderCatalogBranch = (
    workspaceId: string,
    parentNode: CatalogProjectedNode | null,
    depth: number,
    showContents = true,
  ): ReactNode => {
    const parentNodeId = parentNode?.node_id ?? null;
    const key = `${workspaceId}:${parentNodeId ?? "root"}`;
    const branch = explorer.branches.get(key);
    const hasSnapshot = branch?.catalog_revision !== undefined || branch?.local_pending === true;
    const workspaceActionError = parentNodeId === null && actionError?.workspaceId === workspaceId
      ? actionError : null;
    if (!branch && !workspaceActionError) {
      return <div className="session-resource-state">{t('navigation.catalog.waiting')}</div>;
    }
    const workspace = workspacesById.get(workspaceId);
    const recoverWorkspace = async () => {
      setRecoveringWorkspaceIds((previous) => new Set(previous).add(workspaceId));
      let retryActivation = workspaceActionError?.retryActivation === true;
      let title = retryActivation ? t('navigation.workspace.switchFailed') : t('navigation.workspace.recoverFailed');
      try {
        if (workspaceActionError?.retryActivation) {
          await onActivateWorkspace(workspaceId);
        } else if (workspace?.connection_kind === "remote_gateway") {
          await onReconnectWorkspace(workspaceId);
        } else if (workspace?.status === "offline" && workspace.managed) {
          await onStartWorkspace(workspaceId);
        }
        retryActivation = false;
        title = t('navigation.catalog.refreshFailed');
        await explorer.loadBranch(workspaceId, parentNodeId);
        setActionError((previous) => previous === workspaceActionError ? null : previous);
      } catch (recoveryError) {
        handleError(title, recoveryError, {
          workspaceId,
          retryActivation,
        });
      } finally {
        setRecoveringWorkspaceIds((previous) => {
          const next = new Set(previous);
          next.delete(workspaceId);
          return next;
        });
      }
    };
    const recovering = recoveringWorkspaceIds.has(workspaceId);
    const branchError = !branch?.loading
      && workspace?.status === "offline"
      && workspace.connection_error
      ? workspace.connection_error
      : branch?.error;
    const branchFailure = branchError
      ? workspaceFailurePresentation(workspace, branchError)
      : null;
    const failure = workspaceActionError
      ? {
          title: workspaceActionError.title,
          message: branchFailure?.message ?? t('navigation.workspace.checkConnection'),
          technicalDetail: [
            `${workspaceActionError.title}: ${workspaceActionError.detail}`,
            ...(branchFailure ? [`${branchFailure.title}: ${branchFailure.technicalDetail}`] : []),
          ].join("\n\n"),
        }
      : branchFailure;
    return (
      <ul className="session-resource-list" role="group" aria-busy={branch?.loading || undefined}>
        {showContents && branch?.items.map((node) => renderCatalogNode(workspaceId, node, depth))}
        {showContents && branch?.loading && !hasSnapshot ? <li className="session-resource-state">{t('navigation.catalog.loading')}</li> : null}
        {showContents && branch?.consistency_warning ? (
          <li className="session-resource-error-card" role="status">
            <strong>{t('navigation.catalog.physicalTreeInvalid')}</strong>
            <span>{t('navigation.catalog.physicalTreeExplanation')}</span>
            <button type="button" onClick={refreshResourceTreeInBackground}>
              {t('navigation.catalog.revalidate')}
            </button>
            <div className="session-resource-error-actions">
              <button type="button" onClick={() => openDiagnostic(t('navigation.catalog.physicalTreeInvalid'), branch.consistency_warning!, workspaceId)}>{t('navigation.diagnostic.viewLogs')}</button>
            </div>
          </li>
        ) : null}
        {failure ? (
          <li className="session-resource-error-card" role="alert">
            <strong>{failure.title}</strong>
            <span>{failure.message}</span>
            <div className="session-resource-error-actions">
              <button type="button" disabled={recovering} onClick={() => void recoverWorkspace()}>
                {recovering
                  ? t('navigation.workspace.recovering')
                  : workspaceActionError?.retryActivation
                    ? t('navigation.workspace.retrySwitch')
                  : workspace?.connection_kind === "remote_gateway"
                    ? t('navigation.workspace.reconnectGateway')
                    : workspace?.status === "offline" && workspace.managed
                      ? t('navigation.workspace.start')
                      : t('navigation.catalog.reload')}
              </button>
              {workspace?.connection_kind === "remote_gateway" ? (
                <button type="button" onClick={onOpenConnectionManager}>{t('navigation.workspace.connections')}</button>
              ) : null}
              <button type="button" onClick={() => openDiagnostic(failure.title, failure.technicalDetail, workspaceId)}>{t('navigation.diagnostic.viewLogs')}</button>
              {workspaceActionError ? <button type="button" onClick={() => setActionError(null)}>{t('navigation.close')}</button> : null}
            </div>
          </li>
        ) : null}
        {/* 会话子分支不插入空态行；本地移出子项也不能证明后端分支已经为空。 */}
        {showContents && hasSnapshot && !failure && parentNode?.kind !== "session"
          && branch?.total === 0 && branch.cursor === null && branch.items.length === 0
          && branch.locally_removed_node_ids.length === 0 ? (
          <li className="session-resource-state">{t('navigation.catalog.empty')}</li>
        ) : null}
        {showContents && branch?.cursor ? (
          <li>
            <button
              type="button"
              className="session-resource-load-more"
              onClick={() => void explorer.loadBranch(workspaceId, parentNodeId, true).catch(() => {
                // 追加页加载失败同样由当前分支展示。
              })}
            >
              {t('navigation.catalog.loadMore', formatNumber(branch.items.length), branch.total === null ? '' : formatNumber(branch.total))}
            </button>
          </li>
        ) : null}
      </ul>
    );
  };

  const renderCatalogNode = (
    workspaceId: string,
    node: CatalogProjectedNode,
    depth: number,
  ): ReactNode => {
    const workspace = workspacesById.get(workspaceId);
    const expansionId = `catalog:${workspaceId}:${node.node_id}`;
    const expanded = explorer.expandedIds.has(expansionId);
    const isFolder = node.kind === "folder";
    const canExpand = node.state !== "deleting" && (isFolder || node.has_children);
    const isCurrent = node.session_id === currentSessionId && workspaceId === currentSessionWorkspaceId;
    const sessionCacheKey = node.session_id
      ? sessionScopeKey(workspaceId, node.session_id)
      : null;
    const opening = sessionCacheKey !== null
      && sessionCacheKey === openingSession?.sessionKey;
    const timePresentation = isFolder
      ? null
      : sessionCatalogTimePresentation(node);
  const openNode = () => {
    if (isFolder) {
      explorer.toggleExpanded(expansionId, workspaceId, node.node_id);
      return;
    }
    if (!node.session_id || !sessionCacheKey) {
      throw new Error(t('navigation.catalog.sessionIdMissing', node.node_id));
    }
    openSessionNode(workspaceId, node.session_id, sessionCacheKey);
  };
    const target: SessionResourceDropTarget = isFolder
      ? { kind: "session_folder", nodeId: node.node_id, workspaceId }
      : {
          kind: "session",
          nodeId: node.node_id,
          sessionId: node.session_id as string,
          workspaceId,
        };
    const targetKey = sessionResourceDropTargetKey(target);
    return (
      <li key={node.node_id} role="treeitem" aria-expanded={canExpand ? expanded : undefined}>
        <div
          className={`session-resource-row${isCurrent ? " current-session" : ""}${dropTargetClass(targetKey)}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
          aria-current={isCurrent ? "true" : undefined}
          aria-grabbed={dragItem?.nodeId === node.node_id}
          draggable={node.state !== "deleting" && (isFolder ? node.folder_id !== null : true)}
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          data-testid={`catalog-node-${workspaceId}-${node.node_id}`}
          onDragStart={(event) => startDrag(
            event,
            isFolder
              ? {
                  kind: "session_folder",
                  nodeId: node.node_id,
                  workspaceId,
                  parentNodeId: node.parent_node_id ?? null,
                }
              : {
                  kind: "session",
                  nodeId: node.node_id,
                  sessionId: node.session_id as string,
                  workspaceId,
                  parentNodeId: node.parent_node_id ?? null,
                },
          )}
          onDragOver={(event) => handleDragOver(event, target)}
          onDragEnter={(event) => handleDragOver(event, target)}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              clearDropTarget();
            }
          }}
          onDrop={(event) => handleDrop(event, target)}
          onDragEnd={clearDrag}
          onContextMenu={(event) => {
            event.preventDefault();
            setWorkspaceFolderMenu(null);
            if (node.state === "deleting") return;
            if (isFolder && (node.folder_id || node.client_ref)) {
              setFolderMenu({
                workspaceId,
                nodeId: node.node_id,
                folderId: node.folder_id,
                parentNodeId: node.parent_node_id ?? null,
                name: node.name,
                x: event.clientX,
                y: event.clientY,
              });
              if (workspace?.status === "ready" && node.folder_id) {
                refreshSessionCatalogBranchInBackground(workspaceId, node.node_id);
              }
              return;
            }
            if (isFolder) {
              // 文件夹节点缺 folder_id 属于权威索引契约被破坏。旧实现直接
              // return，右键完全无反应，用户无法判断是节点损坏还是点空了。
              handleError(
                t('navigation.sessions.openMenuFailed'),
                new Error(t('navigation.catalog.folderIdMissing', node.node_id)),
              );
              return;
            }
            if (!node.session_id) {
              handleError(
                t('navigation.sessions.openMenuFailed'),
                new Error(t('navigation.catalog.sessionIdMissing', node.node_id)),
              );
              return;
            }
            setFolderMenu(null);
            const session = node.session;
            if (!session) {
              handleError(
                t('navigation.sessions.openMenuFailed'),
                new Error(t('navigation.catalog.sessionMetadataMissing', node.node_id)),
              );
              return;
            }
            if (session.session_id !== node.session_id) {
              handleError(
                t('navigation.sessions.openMenuFailed'),
                new Error(
                  t('navigation.catalog.sessionMetadataMismatch', node.node_id, session.session_id),
                ),
              );
              return;
            }
            onOpenSessionMenu(session, workspaceId, event.clientX, event.clientY);
          }}
        >
          {canExpand ? (
            <button
              type="button"
              className="session-resource-chevron"
              aria-label={t(isFolder
                ? expanded ? 'navigation.catalog.collapseFolder' : 'navigation.catalog.expandFolder'
                : expanded ? 'navigation.catalog.collapseSession' : 'navigation.catalog.expandSession', node.name)}
              onClick={() => explorer.toggleExpanded(expansionId, workspaceId, node.node_id)}
            >
              <Chevron expanded={expanded} />
            </button>
          ) : <span className="session-resource-chevron-spacer" />}
          <button
            type="button"
            className="session-resource-main-action"
            ref={workspaceId === catalogTarget.workspaceId && node.node_id === catalogTarget.nodeId ? catalogTarget.reveal : undefined}
            data-session-id={!isFolder ? node.session_id : undefined}
            title={node.name}
            aria-busy={opening || undefined}
            disabled={node.state === "deleting"}
            onClick={openNode}
            onDoubleClick={!isFolder && node.session_id && onPinSession
              ? () => onPinSession(workspaceId, node.session_id!) : undefined}
          >
            <span className={`codicon ${isFolder ? "codicon-folder" : "codicon-comment-discussion"}`} aria-hidden="true" />
            <span className="session-resource-label">{node.name}</span>
            {node.pending_state !== null || node.state === "deleting" ? (
              <span
                className="session-resource-pending-state"
                data-pending-state={node.pending_state}
                data-catalog-state={node.state ?? undefined}
                data-deletion-settlement={node.settlement ?? undefined}
                title={node.settlement_error ?? t('navigation.catalog.changeStatus', node.settlement === "pending" ? t('navigation.catalog.deleteDraining') : node.pending_state === "accepted" ? t('navigation.catalog.acceptedPending') : node.pending_state ?? node.state)}
              >
                {node.settlement_error ? t('navigation.catalog.deleteRepair') : node.settlement === "pending" ? t('navigation.catalog.deleteDraining') : node.settlement === "settled" ? t('navigation.catalog.deleteRefreshing') : node.state === "deleting" ? t('navigation.catalog.deleting') : node.pending_state === "committed" ? t('navigation.catalog.committed') : node.pending_state === "accepted" ? t('navigation.catalog.waitingToCommit') : t('navigation.catalog.syncing')}
              </span>
            ) : null}
            {timePresentation ? (
              <time
                className="session-resource-time"
                dateTime={node.updated_at ?? node.created_at ?? undefined}
                title={t('navigation.catalog.updatedAt', timePresentation.title)}
              >
                {timePresentation.label}
              </time>
            ) : null}
            {opening ? (
              <span
                className="codicon codicon-loading codicon-modifier-spin session-resource-opening-icon"
                role="img"
                aria-label={t('navigation.sessions.opening')}
              />
            ) : null}
            {!isFolder && sessionCacheKey && !opening ? (
              <SessionActivityIndicator
                running={activeJobIdsBySession.has(sessionCacheKey)}
                unread={unreadSessionKeys.has(sessionCacheKey)}
              />
            ) : null}
          </button>
        </div>
        {canExpand && expanded ? renderCatalogBranch(workspaceId, node, depth + 1) : null}
      </li>
    );
  };

  const renderWorkspace = (node: WorkspaceNavigationNode, depth: number): ReactNode => {
    const workspaceId = nullableGatewayWorkspaceKey(node.workspace);
    if (!workspaceId) {
      return null;
    }
    const workspace = workspacesById.get(workspaceId);
    const hoverTitle = workspace ? workspaceHoverTitle(workspace) : node.name;
    const expanded = explorer.expandedIds.has(`workspace:${workspaceId}`);
    const active = workspaceId === activeWorkspaceId;
    const starting = startingWorkspaceIds.has(workspaceId);
    // 删除中的工作区仍在 Gateway 列表里（删除请求在途），但这一行必须立刻失去交互能力：
    // 否则用户可以继续激活、拖拽、右键一个正在消失的工作区。
    const removing = removingWorkspaceIds.has(workspaceId);
    // 工作区已不在 Gateway 列表时 statusPresentation 会给出「已不可用」，
    // 同时必须禁用激活按钮，避免点到一个已不存在的 workspace_id。
    const statusPresentation = workspaceStatusPresentation(workspace);
    const unavailable = !workspace;
    const target: SessionResourceDropTarget = {
      kind: "workspace",
      nodeId: node.node_id,
      workspaceId,
      navigationParentNodeId: node.parent_node_id ?? null,
      parentWorkspaceId: nullableGatewayWorkspaceKey(workspace?.parent_workspace),
    };
    const targetKey = sessionResourceDropTargetKey(target);
    const childWorkspaces = workspaceChildren.get(workspaceId) ?? [];
    return (
      <li key={node.node_id} role="treeitem" aria-expanded={expanded}>
        <div
          className={`session-resource-row workspace${active ? " active-workspace" : ""}${removing ? " removing" : ""}${dropTargetClass(targetKey)}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
          aria-current={active ? "location" : undefined}
          title={hoverTitle}
          aria-grabbed={dragItem?.nodeId === node.node_id}
          draggable={!workspaceSwitching && !starting && !unavailable && !removing}
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          data-testid={`workspace-node-${workspaceId}`}
          onDragStart={(event) => startDrag(event, {
            kind: "workspace",
            nodeId: node.node_id,
            workspaceId,
            parentWorkspaceId: nullableGatewayWorkspaceKey(workspace?.parent_workspace),
            parentNodeId: node.parent_node_id ?? null,
          })}
          onDragOver={(event) => handleDragOver(event, target)}
          onDragEnter={(event) => handleDragOver(event, target)}
          onDrop={(event) => handleDrop(event, target)}
          onDragEnd={clearDrag}
          onContextMenu={(event) => {
            event.preventDefault();
            if (removing) {
              return;
            }
            setWorkspaceFolderMenu(null);
            if (workspace) {
              onOpenWorkspaceMenu(workspace, event.clientX, event.clientY);
              if (workspace.status === "ready") {
                refreshSessionCatalogBranchInBackground(workspaceId, null);
              }
            }
          }}
        >
          <button
            type="button"
            className="session-resource-chevron"
              aria-label={t(expanded ? 'navigation.workspace.collapse' : 'navigation.workspace.expand', node.name)}
            disabled={starting || removing}
            onClick={() => explorer.toggleExpanded(`workspace:${workspaceId}`, workspaceId, null)}
          >
            <Chevron expanded={expanded} />
          </button>
          <span className="codicon codicon-root-folder" aria-hidden="true" />
          <button
            type="button"
            className="session-resource-label workspace-label"
            title={hoverTitle}
            disabled={workspaceSwitching || starting || removing || unavailable || workspace?.status === "offline"}
            onClick={() => void activateWorkspace(workspaceId)}
          >
            {node.name}
          </button>
          {removing ? (
            <span
              className="session-resource-workspace-status removing"
              role="status"
              aria-label={t('navigation.workspace.deleting')}
              title={t('navigation.workspace.deleting')}
            >
              <i aria-hidden="true" />
              {t('navigation.workspace.deletingLabel')}
              </span>
          ) : starting ? (
            <span
              className="codicon codicon-loading codicon-modifier-spin session-resource-starting-icon"
              role="img"
              aria-label={t('navigation.workspace.startingAria')}
              title={t('navigation.workspace.starting')}
            />
          ) : statusPresentation ? (
            <span
              className="session-resource-workspace-status offline"
              role="status"
              aria-label={t('navigation.workspace.status', statusPresentation.label)}
              title={statusPresentation.title}
            >
              <i aria-hidden="true" />
              {statusPresentation.label}
            </span>
          ) : null}
        </div>
        {expanded && childWorkspaces.length > 0 ? (
          <ul className="session-resource-list" role="group">
            {childWorkspaces.map((child) => renderWorkspace(child, depth + 1))}
          </ul>
        ) : null}
        {(expanded || actionError?.workspaceId === workspaceId) && !starting
          ? renderCatalogBranch(workspaceId, null, depth + 1, expanded) : null}
      </li>
    );
  };

  const renderNavigationNode = (node: WorkspaceNavigationNode, depth: number): ReactNode => {
    if (node.kind === "workspace_ref") {
      const workspace = node.workspace
        ? workspacesById.get(gatewayWorkspaceKey(node.workspace))
        : undefined;
      if (
        workspace?.parent_workspace &&
        workspaceRefsByWorkspaceId.has(gatewayWorkspaceKey(workspace.parent_workspace))
      ) {
        return null;
      }
      return renderWorkspace(node, depth);
    }
    const expansionId = `navigation:${node.node_id}`;
    const expanded = explorer.expandedIds.has(expansionId);
    const children = navigationChildren.get(node.node_id) ?? [];
    const target: SessionResourceDropTarget = {
      kind: "workspace_folder",
      nodeId: node.node_id,
      parentNodeId: node.parent_node_id ?? null,
    };
    const targetKey = sessionResourceDropTargetKey(target);
    const activeEditor =
      workspaceFolderEditor?.mode === "rename" && workspaceFolderEditor.nodeId === node.node_id
        ? workspaceFolderEditor
        : null;
    return (
      <li key={node.node_id} role="treeitem" aria-expanded={expanded}>
        <div
          className={`session-resource-row workspace-folder${dropTargetClass(targetKey)}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
          aria-grabbed={dragItem?.nodeId === node.node_id}
          draggable={!activeEditor}
          data-testid={`workspace-folder-node-${node.node_id}`}
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          onDragStart={(event) => startDrag(event, {
            kind: "workspace_folder",
            nodeId: node.node_id,
            parentNodeId: node.parent_node_id ?? null,
          })}
          onDragOver={(event) => handleDragOver(event, target)}
          onDragEnter={(event) => handleDragOver(event, target)}
          onDrop={(event) => handleDrop(event, target)}
          onDragEnd={clearDrag}
          onContextMenu={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setFolderMenu(null);
            setWorkspaceFolderMenu({
              nodeId: node.node_id,
              parentNodeId: node.parent_node_id ?? null,
              name: node.name,
              x: event.clientX,
              y: event.clientY,
            });
            refreshWorkspaceNavigationInBackground();
          }}
        >
          <button type="button" className="session-resource-chevron" aria-label={t(expanded ? 'navigation.workspace.collapseFolder' : 'navigation.workspace.expandFolder', node.name)} onClick={() => explorer.toggleExpanded(expansionId)}>
            <Chevron expanded={expanded} />
          </button>
          <span className="codicon codicon-folder" aria-hidden="true" />
          {activeEditor ? (
            <input
              autoFocus
              className="session-resource-inline-editor"
              aria-label={t('navigation.workspace.renameFolderName')}
              value={activeEditor.value}
              onChange={(event) => setWorkspaceFolderEditor({ ...activeEditor, value: event.target.value })}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  submitWorkspaceFolderEditor({ ...activeEditor, value: event.currentTarget.value });
                } else if (event.key === "Escape") {
                  event.preventDefault();
                  setWorkspaceFolderEditor(null);
                }
              }}
              onBlur={() => setWorkspaceFolderEditor(null)}
            />
          ) : (
            <button type="button" className="session-resource-label" onClick={() => explorer.toggleExpanded(expansionId)}>{node.name}</button>
          )}
        </div>
        {expanded ? (
          <ul className="session-resource-list" role="group">
            {workspaceFolderEditor?.mode === "create" && workspaceFolderEditor.parentNodeId === node.node_id
              ? renderWorkspaceFolderEditor(workspaceFolderEditor, depth + 1)
              : null}
            {children.map((child) => renderNavigationNode(child, depth + 1))}
          </ul>
        ) : null}
      </li>
    );
  };

  const actionErrorCard = actionError && !actionError.workspaceId ? (
    <div className="session-resource-error-card global session-resource-error" role="alert">
      <strong>{actionError.title}</strong>
      <div className="session-resource-error-actions">
        <button type="button" onClick={() => openDiagnostic(actionError.title, actionError.detail)}>{t('navigation.diagnostic.viewLogs')}</button>
        <button type="button" onClick={() => setActionError(null)}>{t('navigation.close')}</button>
      </div>
    </div>
  ) : null;

  if (searchOpen && searchQuery.trim()) {
    return (
      <section className="session-resource-search-results" aria-label={t('navigation.search.results')} aria-busy={explorer.searching}>
        {explorer.searching ? <div className="session-resource-state">{t('navigation.search.searchingAll')}</div> : null}
        {actionErrorCard}
        {explorer.searchError ? (
          <div className="session-resource-error-card global" role="alert">
            <strong>{t('navigation.search.failed')}</strong>
            <div className="session-resource-error-actions">
              <button type="button" onClick={() => openDiagnostic(t('navigation.search.failed'), explorer.searchError!)}>{t('navigation.diagnostic.viewLogs')}</button>
            </div>
          </div>
        ) : null}
        {!explorer.searching && !explorer.searchError && explorer.searchResults.items.length === 0 ? <div className="session-resource-state">{t('navigation.search.empty')}</div> : null}
        {explorer.searchResults.items.map((item) => (
          <button
            type="button"
            className="session-resource-search-result"
            key={`${item.workspace_id}:${item.node_id}`}
            onClick={() => {
              if (item.node_kind === "workspace_folder") {
                explorer.revealWorkspaceFolder(item.breadcrumb_node_ids);
                onStatusChange(t('navigation.search.workspaceFolderRevealed', item.relative_path));
                return;
              }
              if (!item.owner) throw new Error(t('navigation.search.ownerMissing'));
              const nodeKind = item.node_kind;
              void resolveCatalogSearchTarget(apiPort, item.owner).then(async (route) => {
                if (nodeKind === "workspace") {
                  await onActivateWorkspace(route);
                } else {
                  await explorer.revealSearchResult(route, item.breadcrumb_node_ids, nodeKind);
                  if (item.session_id) await onSelectSession(route, item.session_id);
                }
              }).catch((error) => handleError(t('navigation.search.revealFailed'), error));
            }}
          >
            <strong>{item.name}</strong>
            <span title={item.relative_path}>{item.workspace_name} / {item.relative_path}</span>
            {item.node_kind === "session" && item.session_id ? (
              <SessionActivityIndicator
                running={activeJobIdsBySession.has(
                  sessionScopeKey(item.workspace_id, item.session_id),
                )}
                unread={unreadSessionKeys.has(
                  sessionScopeKey(item.workspace_id, item.session_id),
                )}
              />
            ) : null}
          </button>
        ))}
        {explorer.searchResults.limited ? <div className="session-resource-state">{t('navigation.search.limitReached')}</div> : null}
        {explorer.searchResults.workspaces.some((workspace) => workspace.status === "stale" || workspace.status === "unavailable") ? (
          <details className="session-resource-offline-summary">
            <summary>{t('navigation.search.offlineSummary')}</summary>
            {explorer.searchResults.workspaces.filter((workspace) => workspace.status === "stale" || workspace.status === "unavailable").map((workspace) => <div key={workspace.workspace_id}>{t('navigation.search.workspaceStatus', workspace.workspace_name, t(workspace.status === "stale" ? 'navigation.search.cached' : 'navigation.search.unsearchable'), workspace.error)}{workspace.source?.updated_at ? t('navigation.search.catalogUpdated', workspace.source.updated_at) : ""}</div>)}
          </details>
        ) : null}
      </section>
    );
  }

  return (
    <>
    <section className="session-resource-explorer" aria-label={t('navigation.explorer.title')}>
      <div className="session-resource-toolbar">
        <div
          className={`session-resource-root-drop-target${dropTargetClass("navigation_root")}`}
          role="heading"
          aria-level={2}
          tabIndex={0}
          title={t('navigation.workspace.folderRootHint')}
          data-testid="workspace-navigation-root-drop-target"
          onContextMenu={(event) => {
            event.preventDefault();
            setFolderMenu(null);
            setWorkspaceFolderMenu({
              nodeId: null,
              parentNodeId: null,
              name: t('navigation.workspace.folder'),
              x: event.clientX,
              y: event.clientY,
            });
            refreshWorkspaceNavigationInBackground();
          }}
          onKeyDown={(event) => {
            if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) {
              return;
            }
            event.preventDefault();
            const rect = event.currentTarget.getBoundingClientRect();
            setWorkspaceFolderMenu({
              nodeId: null,
              parentNodeId: null,
              name: t('navigation.workspace.folder'),
              x: rect.left,
              y: rect.bottom,
            });
            refreshWorkspaceNavigationInBackground();
          }}
          onDragOver={(event) => handleDragOver(event, { kind: "navigation_root" })}
          onDragEnter={(event) => handleDragOver(event, { kind: "navigation_root" })}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              clearDropTarget();
            }
          }}
          onDrop={(event) => handleDrop(event, { kind: "navigation_root" })}
        >
          <span className="codicon codicon-folder" aria-hidden="true" /> {t('navigation.workspace.folder')}</div>
        <button type="button" title={t('navigation.catalog.refreshHint')} aria-label={t('navigation.catalog.refresh')} onClick={refreshResourceTreeInBackground}>
          <span className="codicon codicon-refresh" aria-hidden="true" />
        </button>
        <button type="button" title={t('navigation.workspace.add')} aria-label={t('navigation.workspace.add')} onClick={onRequestAddWorkspace}>
          <span className="codicon codicon-add" aria-hidden="true" />
        </button>
      </div>
      {actionErrorCard}
      {explorer.navigationError ? (
        <div className="session-resource-error-card global" role="alert">
          <strong>{t('navigation.workspace.listFailed')}</strong>
          <span>{t('navigation.workspace.navigationUnavailable')}</span>
          <div className="session-resource-error-actions">
            <button type="button" onClick={() => void explorer.refreshNavigation().catch(() => undefined)}>
              {t('navigation.workspace.reloadList')}
            </button>
            <button type="button" onClick={() => openDiagnostic(t('navigation.workspace.listFailed'), explorer.navigationError!)}>{t('navigation.diagnostic.viewLogs')}</button>
          </div>
        </div>
      ) : null}
      {explorer.revealError ? (
        <div className="session-resource-error-card" role="alert">
          <strong>{t('navigation.sessions.revealCurrentFailed')}</strong>
          <span>{t('navigation.sessions.revealCurrentExplanation')}</span>
          <div className="session-resource-error-actions">
            <button type="button" onClick={() => openDiagnostic(t('navigation.sessions.revealCurrentFailed'), explorer.revealError!, activeWorkspaceId ?? undefined)}>{t('navigation.diagnostic.viewLogs')}</button>
          </div>
        </div>
      ) : null}
      {!explorer.navigation && !explorer.navigationError ? <div className="session-resource-state">{t('navigation.workspace.loadingCatalog')}</div> : null}
      {navigationEmpty ? (
        <div className="session-resource-state">
          {t('navigation.workspace.empty')}
        </div>
      ) : null}
      <ul
        className={`session-resource-list navigation-root${dropTargetClass("navigation_root")}`}
        role="tree"
        onDragOver={(event) => handleDragOver(event, { kind: "navigation_root" })}
        onDragEnter={(event) => handleDragOver(event, { kind: "navigation_root" })}
        onDrop={(event) => handleDrop(event, { kind: "navigation_root" })}
        onContextMenu={(event) => {
          if (event.target !== event.currentTarget) {
            return;
          }
          event.preventDefault();
          setFolderMenu(null);
          setWorkspaceFolderMenu({
            nodeId: null,
            parentNodeId: null,
            name: t('navigation.workspace.root'),
            x: event.clientX,
            y: event.clientY,
          });
          refreshWorkspaceNavigationInBackground();
        }}
      >
        {workspaceFolderEditor?.mode === "create" && workspaceFolderEditor.parentNodeId === null
          ? renderWorkspaceFolderEditor(workspaceFolderEditor, 0)
          : null}
        {(navigationChildren.get(null) ?? []).map((node) => renderNavigationNode(node, 0))}
      </ul>
    </section>
    <SessionResourceOverlays
      workspaceFolderMenu={workspaceFolderMenu}
      setWorkspaceFolderMenu={setWorkspaceFolderMenu}
      setWorkspaceFolderEditor={setWorkspaceFolderEditor}
      folderMenu={folderMenu}
      setFolderMenu={setFolderMenu}
      resourceDialog={resourceDialog}
      setResourceDialog={setResourceDialog}
      explorer={explorer}
      onCreateSessionInFolder={onCreateSessionInFolder}
      onSessionFolderDeleted={onSessionFolderDeleted}
      onStatusChange={onStatusChange}
      onClearError={() => setActionError(null)}
      handleError={handleError}
    />
    </>
  );
}
