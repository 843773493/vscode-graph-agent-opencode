import { useState, type DragEvent, type ReactNode } from "react";
import { useSessionResourceExplorer } from "../../hooks/useSessionResourceExplorer";
import type {
  GatewayWorkspace,
  Session,
  SessionCatalogNode,
  WorkspaceNavigationNode,
} from "../../types/backend";
import SessionGeneratorManager from "./SessionGeneratorManager";
import {
  decideSessionResourceDrop,
  sessionResourceDropTargetKey,
  type SessionResourceDragItem,
  type SessionResourceDropTarget,
} from "./sessionResourceDrag";
import SessionResourceOverlays, {
  type SessionFolderContextMenu,
  type SessionResourceDialog,
  type WorkspaceFolderContextMenu,
  type WorkspaceFolderEditor,
} from "./SessionResourceOverlays";

interface SessionResourceExplorerProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  currentSessionId: string;
  searchOpen: boolean;
  searchQuery: string;
  workspaceSwitching: boolean;
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
  catalogRefreshVersion: number;
  onSelectSession: (workspaceId: string, sessionId: string) => void | Promise<void>;
  onStatusChange: (message: string) => void;
  onOpenWorkspaceMenu: (workspace: GatewayWorkspace, x: number, y: number) => void;
  onOpenSessionMenu: (session: Session, workspaceId: string, x: number, y: number) => void;
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
  apiPort,
  workspaces,
  activeWorkspaceId,
  currentSessionId,
  searchOpen,
  searchQuery,
  workspaceSwitching,
  onActivateWorkspace,
  onSetWorkspaceParent,
  onRefreshWorkspaceSessions,
  onCreateSessionInFolder,
  onSessionFolderDeleted,
  catalogRefreshVersion,
  onSelectSession,
  onStatusChange,
  onOpenWorkspaceMenu,
  onOpenSessionMenu,
}: SessionResourceExplorerProps): ReactNode {
  const [actionError, setActionError] = useState<string | null>(null);
  const [folderMenu, setFolderMenu] = useState<SessionFolderContextMenu | null>(null);
  const [workspaceFolderMenu, setWorkspaceFolderMenu] =
    useState<WorkspaceFolderContextMenu | null>(null);
  const [workspaceFolderEditor, setWorkspaceFolderEditor] =
    useState<WorkspaceFolderEditor | null>(null);
  const [resourceDialog, setResourceDialog] = useState<SessionResourceDialog | null>(null);
  const [dragItem, setDragItem] = useState<SessionResourceDragItem | null>(null);
  const [dropTargetKey, setDropTargetKey] = useState<string | null>(null);
  const explorer = useSessionResourceExplorer({
    apiPort,
    activeWorkspaceId,
    searchOpen,
    searchQuery,
    currentSessionId,
    refreshVersion: catalogRefreshVersion,
  });
  const workspacesById = new Map(
    workspaces.map((workspace) => [workspace.workspace_id, workspace]),
  );
  const navigationNodes = explorer.navigation?.nodes ?? [];
  const navigationChildren = new Map<string | null, WorkspaceNavigationNode[]>();
  for (const node of navigationNodes) {
    const parentId = node.parent_node_id ?? null;
    navigationChildren.set(parentId, [...(navigationChildren.get(parentId) ?? []), node]);
  }
  for (const children of navigationChildren.values()) {
    children.sort((left, right) => left.position - right.position || left.name.localeCompare(right.name));
  }

  const handleError = (prefix: string, error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    const errorMessage = `${prefix}: ${message}`;
    setActionError(errorMessage);
    onStatusChange(errorMessage);
  };

  const workspaceRefsByWorkspaceId = new Map(
    navigationNodes
      .filter((node) => node.kind === "workspace_ref" && node.workspace_id)
      .map((node) => [node.workspace_id as string, node]),
  );
  const workspaceChildren = new Map<string, WorkspaceNavigationNode[]>();
  for (const workspace of workspaces) {
    const parentWorkspaceId = workspace.parent_workspace_id ?? null;
    const reference = workspaceRefsByWorkspaceId.get(workspace.workspace_id);
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

  const startDrag = (
    event: DragEvent<HTMLElement>,
    item: SessionResourceDragItem,
  ) => {
    setDragItem(item);
    setDropTargetKey(null);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-boxteam-session-resource", JSON.stringify(item));
    event.dataTransfer.setData("text/plain", item.nodeId);
  };

  const clearDrag = () => {
    setDragItem(null);
    setDropTargetKey(null);
  };

  const handleDragOver = (
    event: DragEvent<HTMLElement>,
    target: SessionResourceDropTarget,
  ) => {
    if (!dragItem) {
      return;
    }
    event.stopPropagation();
    const decision = decideSessionResourceDrop(dragItem, target);
    if (!decision.allowed) {
      event.dataTransfer.dropEffect = "none";
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDropTargetKey(sessionResourceDropTargetKey(target));
  };

  const performDrop = async (
    source: SessionResourceDragItem,
    target: SessionResourceDropTarget,
  ) => {
    const decision = decideSessionResourceDrop(source, target);
    if (!decision.allowed) {
      throw new Error(decision.reason);
    }
    if (decision.action.kind === "move_workspace_navigation") {
      await explorer.moveWorkspaceNode(source.nodeId, decision.action.parentNodeId);
      if (source.kind === "workspace" && source.parentWorkspaceId !== null) {
        try {
          await onSetWorkspaceParent(source.workspaceId, null);
        } catch (error) {
          try {
            await explorer.moveWorkspaceNode(source.nodeId, source.parentNodeId);
          } catch (rollbackError) {
            throw new Error(
              `${error instanceof Error ? error.message : String(error)}；恢复工作区虚拟位置也失败: ${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
            );
          }
          throw error;
        }
      }
      onStatusChange(
        source.kind === "workspace"
          ? "已移动工作区"
          : "已移动工作区文件夹",
      );
      return;
    }
    if (decision.action.kind === "set_workspace_parent") {
      if (source.kind !== "workspace") {
        throw new Error("拖放来源不是工作区");
      }
      await explorer.moveWorkspaceNode(
        source.nodeId,
        decision.action.navigationParentNodeId,
      );
      try {
        await onSetWorkspaceParent(
          source.workspaceId,
          decision.action.parentWorkspaceId,
        );
      } catch (error) {
        try {
          await explorer.moveWorkspaceNode(source.nodeId, source.parentNodeId);
        } catch (rollbackError) {
          throw new Error(
            `${error instanceof Error ? error.message : String(error)}；恢复工作区虚拟位置也失败: ${rollbackError instanceof Error ? rollbackError.message : String(rollbackError)}`,
          );
        }
        throw error;
      }
      onStatusChange("已设置子工作区");
      return;
    }
    if (source.kind !== "session" && source.kind !== "session_folder") {
      throw new Error("拖放来源不是会话资源");
    }
    try {
      await explorer.moveCatalogNode(
        source.workspaceId,
        source.nodeId,
        decision.action.parentNodeId,
        source.parentNodeId,
      );
    } catch (error) {
      try {
        await onRefreshWorkspaceSessions(source.workspaceId);
      } catch (reconciliationError) {
        throw new Error(
          `${error instanceof Error ? error.message : String(error)}；重新读取工作区会话也失败: ${reconciliationError instanceof Error ? reconciliationError.message : String(reconciliationError)}`,
        );
      }
      throw error;
    }
    await onRefreshWorkspaceSessions(source.workspaceId);
    onStatusChange(
      source.kind === "session" ? "已移动会话" : "已移动会话文件夹",
    );
  };

  const handleDrop = (
    event: DragEvent<HTMLElement>,
    target: SessionResourceDropTarget,
  ) => {
    if (!dragItem) {
      return;
    }
    event.stopPropagation();
    const source = dragItem;
    const decision = decideSessionResourceDrop(source, target);
    if (!decision.allowed) {
      clearDrag();
      handleError("无法拖放", new Error(decision.reason));
      return;
    }
    event.preventDefault();
    clearDrag();
    void performDrop(source, target).catch((error) => handleError("拖放失败", error));
  };

  const submitWorkspaceFolderEditor = (editor: WorkspaceFolderEditor) => {
    const name = editor.value.trim();
    if (!name) {
      handleError("保存工作区文件夹失败", new Error("名称不能为空"));
      return;
    }
    setWorkspaceFolderEditor(null);
    const operation = editor.mode === "create"
      ? explorer.createWorkspaceFolder(name, editor.parentNodeId)
      : explorer.renameWorkspaceFolder(editor.nodeId as string, name);
    void operation
      .then(() => onStatusChange(editor.mode === "create" ? `已创建工作区文件夹 ${name}` : `已重命名工作区文件夹为 ${name}`))
      .catch((error) => {
        setWorkspaceFolderEditor(editor);
        handleError("保存工作区文件夹失败", error);
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
          aria-label={editor.mode === "create" ? "新工作区文件夹名称" : "工作区文件夹新名称"}
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
    parentNodeId: string | null,
    depth: number,
  ): ReactNode => {
    const key = `${workspaceId}:${parentNodeId ?? "root"}`;
    const branch = explorer.branches.get(key);
    if (!branch) {
      return <div className="session-resource-state">等待加载…</div>;
    }
    return (
      <ul className="session-resource-list" role="group">
        {branch.items.map((node) => renderCatalogNode(workspaceId, node, depth))}
        {branch.loading ? <li className="session-resource-state">正在加载…</li> : null}
        {branch.error ? (
          <li className="session-resource-error" role="alert">
            {branch.error}
            <button type="button" onClick={() => void explorer.loadBranch(workspaceId, parentNodeId).catch(() => undefined)}>
              重试
            </button>
          </li>
        ) : null}
        {!branch.loading && !branch.error && branch.items.length === 0 ? (
          <li className="session-resource-state">空文件夹</li>
        ) : null}
        {branch.cursor ? (
          <li>
            <button
              type="button"
              className="session-resource-load-more"
              onClick={() => void explorer.loadBranch(workspaceId, parentNodeId, true).catch((error) => handleError("加载更多失败", error))}
            >
              加载更多（{branch.items.length}/{branch.total}）
            </button>
          </li>
        ) : null}
      </ul>
    );
  };

  const renderCatalogNode = (
    workspaceId: string,
    node: SessionCatalogNode,
    depth: number,
  ): ReactNode => {
    const expansionId = `catalog:${workspaceId}:${node.node_id}`;
    const expanded = explorer.expandedIds.has(expansionId);
    const isFolder = node.kind === "folder";
    const canExpand = isFolder || node.has_children;
    const isCurrent = node.session_id === currentSessionId && workspaceId === activeWorkspaceId;
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
          className={`session-resource-row${isCurrent ? " current" : ""}${dropTargetKey === targetKey ? " drop-target" : ""}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
          aria-current={isCurrent ? "true" : undefined}
          aria-grabbed={dragItem?.nodeId === node.node_id}
          draggable
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
              setDropTargetKey(null);
            }
          }}
          onDrop={(event) => handleDrop(event, target)}
          onDragEnd={clearDrag}
          onContextMenu={(event) => {
            event.preventDefault();
            setWorkspaceFolderMenu(null);
            if (isFolder && node.folder_id) {
              setFolderMenu({
                workspaceId,
                folderId: node.folder_id,
                parentNodeId: node.parent_node_id ?? null,
                name: node.name,
                x: event.clientX,
                y: event.clientY,
              });
              return;
            }
            if (!node.session_id) {
              return;
            }
            setFolderMenu(null);
            void explorer.resolveSession(workspaceId, node.session_id)
              .then((session) => onOpenSessionMenu(session, workspaceId, event.clientX, event.clientY))
              .catch((error) => handleError("打开会话菜单失败", error));
          }}
        >
          {canExpand ? (
            <button
              type="button"
              className="session-resource-chevron"
              aria-label={`${expanded ? "折叠" : "展开"}${isFolder ? "文件夹" : "会话"} ${node.name}`}
              onClick={() => explorer.toggleExpanded(expansionId, workspaceId, node.node_id)}
            >
              <Chevron expanded={expanded} />
            </button>
          ) : <span className="session-resource-chevron-spacer" />}
          <span className={`codicon ${isFolder ? "codicon-folder" : "codicon-comment-discussion"}`} aria-hidden="true" />
          <button
            type="button"
            className="session-resource-label"
            title={node.storage_relative_path || node.name}
            onClick={() => {
              if (isFolder) {
                explorer.toggleExpanded(expansionId, workspaceId, node.node_id);
              } else if (node.session_id) {
                void Promise.resolve(onSelectSession(workspaceId, node.session_id)).catch((error) => handleError("打开会话失败", error));
              }
            }}
          >
            {node.name}
          </button>
        </div>
        {canExpand && expanded ? renderCatalogBranch(workspaceId, node.node_id, depth + 1) : null}
      </li>
    );
  };

  const renderWorkspace = (node: WorkspaceNavigationNode, depth: number): ReactNode => {
    const workspaceId = node.workspace_id;
    if (!workspaceId) {
      return null;
    }
    const workspace = workspacesById.get(workspaceId);
    const expanded = explorer.expandedIds.has(`workspace:${workspaceId}`);
    const active = workspaceId === activeWorkspaceId;
    const target: SessionResourceDropTarget = {
      kind: "workspace",
      nodeId: node.node_id,
      workspaceId,
      navigationParentNodeId: node.parent_node_id ?? null,
    };
    const targetKey = sessionResourceDropTargetKey(target);
    const childWorkspaces = workspaceChildren.get(workspaceId) ?? [];
    return (
      <li key={node.node_id} role="treeitem" aria-expanded={expanded}>
        <div
          className={`session-resource-row workspace${active ? " current" : ""}${dropTargetKey === targetKey ? " drop-target" : ""}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
          aria-grabbed={dragItem?.nodeId === node.node_id}
          draggable={!workspaceSwitching}
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
          data-testid={`workspace-node-${workspaceId}`}
          onDragStart={(event) => startDrag(event, {
            kind: "workspace",
            nodeId: node.node_id,
            workspaceId,
            parentWorkspaceId: workspace?.parent_workspace_id ?? null,
            parentNodeId: node.parent_node_id ?? null,
          })}
          onDragOver={(event) => handleDragOver(event, target)}
          onDragEnter={(event) => handleDragOver(event, target)}
          onDrop={(event) => handleDrop(event, target)}
          onDragEnd={clearDrag}
          onContextMenu={(event) => {
            event.preventDefault();
            setWorkspaceFolderMenu(null);
            if (workspace) {
              onOpenWorkspaceMenu(workspace, event.clientX, event.clientY);
            }
          }}
        >
          <button
            type="button"
            className="session-resource-chevron"
            aria-label={`${expanded ? "折叠" : "展开"}工作区 ${node.name}`}
            onClick={() => explorer.toggleExpanded(`workspace:${workspaceId}`, workspaceId, null)}
          >
            <Chevron expanded={expanded} />
          </button>
          <span className="codicon codicon-root-folder" aria-hidden="true" />
          <button
            type="button"
            className="session-resource-label workspace-label"
            disabled={workspaceSwitching}
            onClick={() => void onActivateWorkspace(workspaceId).catch((error) => handleError("切换工作区失败", error))}
          >
            {node.name}
          </button>
          {workspace?.status === "offline" ? <span className="session-resource-badge">离线</span> : null}
        </div>
        {expanded && childWorkspaces.length > 0 ? (
          <ul className="session-resource-list" role="group">
            {childWorkspaces.map((child) => renderWorkspace(child, depth + 1))}
          </ul>
        ) : null}
        {expanded ? renderCatalogBranch(workspaceId, null, depth + 1) : null}
      </li>
    );
  };

  const renderNavigationNode = (node: WorkspaceNavigationNode, depth: number): ReactNode => {
    if (node.kind === "workspace_ref") {
      const workspace = node.workspace_id
        ? workspacesById.get(node.workspace_id)
        : undefined;
      if (
        workspace?.parent_workspace_id &&
        workspaceRefsByWorkspaceId.has(workspace.parent_workspace_id)
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
    };
    const targetKey = sessionResourceDropTargetKey(target);
    const activeEditor =
      workspaceFolderEditor?.mode === "rename" && workspaceFolderEditor.nodeId === node.node_id
        ? workspaceFolderEditor
        : null;
    return (
      <li key={node.node_id} role="treeitem" aria-expanded={expanded}>
        <div
          className={`session-resource-row workspace-folder${dropTargetKey === targetKey ? " drop-target" : ""}${dragItem?.nodeId === node.node_id ? " dragging" : ""}`}
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
          }}
        >
          <button type="button" className="session-resource-chevron" aria-label={`${expanded ? "折叠" : "展开"}工作区文件夹 ${node.name}`} onClick={() => explorer.toggleExpanded(expansionId)}>
            <Chevron expanded={expanded} />
          </button>
          <span className="codicon codicon-folder" aria-hidden="true" />
          {activeEditor ? (
            <input
              autoFocus
              className="session-resource-inline-editor"
              aria-label="工作区文件夹新名称"
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

  if (searchOpen && searchQuery.trim()) {
    return (
      <section className="session-resource-search-results" aria-label="跨工作区搜索结果" aria-busy={explorer.searching}>
        {explorer.searching ? <div className="session-resource-state">正在搜索所有工作区…</div> : null}
        {explorer.searchError ? <div className="session-resource-error" role="alert">{explorer.searchError}</div> : null}
        {!explorer.searching && !explorer.searchError && explorer.searchResults.items.length === 0 ? <div className="session-resource-state">没有匹配结果</div> : null}
        {explorer.searchResults.items.map((item) => (
          <button
            type="button"
            className="session-resource-search-result"
            key={`${item.workspace_id}:${item.node_id}`}
            onClick={() => {
              if (item.node_kind === "workspace_folder") {
                explorer.revealWorkspaceFolder(item.breadcrumb_node_ids);
                onStatusChange(`已定位工作区文件夹 ${item.relative_path}`);
                return;
              }
              if (item.node_kind === "workspace") {
                void onActivateWorkspace(item.workspace_id)
                  .catch((error) => handleError("打开搜索到的工作区失败", error));
                return;
              }
              void explorer.revealSearchResult(item.workspace_id, item.breadcrumb_node_ids, item.node_kind)
                .then(() => item.session_id ? onSelectSession(item.workspace_id, item.session_id) : undefined)
                .catch((error) => handleError("定位搜索结果失败", error));
            }}
          >
            <strong>{item.name}</strong>
            <span title={item.storage_relative_path || item.relative_path}>{item.workspace_name} / {item.relative_path}</span>
          </button>
        ))}
        {explorer.searchResults.workspaces.some((workspace) => workspace.status !== "available") ? (
          <details className="session-resource-offline-summary">
            <summary>部分工作区结果来自缓存或不可用</summary>
            {explorer.searchResults.workspaces.filter((workspace) => workspace.status !== "available").map((workspace) => <div key={workspace.workspace_id}>{workspace.workspace_name}（{workspace.status === "stale" ? "缓存结果" : "不可搜索"}）: {workspace.error}</div>)}
          </details>
        ) : null}
      </section>
    );
  }

  return (
    <>
    <section className="session-resource-explorer" aria-label="工作区和会话资源管理器">
      <div className="session-resource-toolbar">
        <div
          className={`session-resource-root-drop-target${dropTargetKey === "navigation_root" ? " drop-target" : ""}`}
          role="heading"
          aria-level={2}
          tabIndex={0}
          title="右击管理工作区文件夹；也可将工作区或工作区文件夹拖到此处移回根级"
          data-testid="workspace-navigation-root-drop-target"
          onContextMenu={(event) => {
            event.preventDefault();
            setFolderMenu(null);
            setWorkspaceFolderMenu({
              nodeId: null,
              parentNodeId: null,
              name: "工作区文件夹",
              x: event.clientX,
              y: event.clientY,
            });
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
              name: "工作区文件夹",
              x: rect.left,
              y: rect.bottom,
            });
          }}
          onDragOver={(event) => handleDragOver(event, { kind: "navigation_root" })}
          onDragEnter={(event) => handleDragOver(event, { kind: "navigation_root" })}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setDropTargetKey(null);
            }
          }}
          onDrop={(event) => handleDrop(event, { kind: "navigation_root" })}
        >
          <span className="codicon codicon-folder" aria-hidden="true" /> 工作区文件夹
        </div>
        <button type="button" title="重新扫描当前工作区的真实会话目录" aria-label="刷新资源树" onClick={() => void explorer.refreshResourceTree().catch((error) => handleError("刷新失败", error))}>
          <span className="codicon codicon-refresh" aria-hidden="true" />
        </button>
      </div>
      {actionError ? (
        <div className="session-resource-error" role="alert">
          <span>{actionError}</span>
          <button type="button" onClick={() => setActionError(null)}>关闭</button>
        </div>
      ) : null}
      {explorer.navigationError ? <div className="session-resource-error" role="alert">{explorer.navigationError}</div> : null}
      {!explorer.navigation && !explorer.navigationError ? <div className="session-resource-state">正在加载工作区目录…</div> : null}
      <ul
        className={`session-resource-list navigation-root${dropTargetKey === "navigation_root" ? " drop-target" : ""}`}
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
            name: "工作区根",
            x: event.clientX,
            y: event.clientY,
          });
        }}
      >
        {workspaceFolderEditor?.mode === "create" && workspaceFolderEditor.parentNodeId === null
          ? renderWorkspaceFolderEditor(workspaceFolderEditor, 0)
          : null}
        {(navigationChildren.get(null) ?? []).map((node) => renderNavigationNode(node, 0))}
      </ul>
      <SessionGeneratorManager
        explorer={explorer}
        workspaces={workspaces}
        activeWorkspaceId={activeWorkspaceId}
        currentSessionId={currentSessionId}
        onStatusChange={onStatusChange}
      />
    </section>
    <SessionResourceOverlays
      workspaceFolderMenu={workspaceFolderMenu}
      setWorkspaceFolderMenu={setWorkspaceFolderMenu}
      folderMenu={folderMenu}
      setFolderMenu={setFolderMenu}
      resourceDialog={resourceDialog}
      setResourceDialog={setResourceDialog}
      explorer={explorer}
      onCreateSessionInFolder={onCreateSessionInFolder}
      onSessionFolderDeleted={onSessionFolderDeleted}
      onStatusChange={onStatusChange}
      setActionError={setActionError}
      handleError={handleError}
    />
    </>
  );
}
