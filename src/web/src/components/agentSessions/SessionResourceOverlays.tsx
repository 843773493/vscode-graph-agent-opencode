import type { Dispatch, ReactNode, SetStateAction } from "react";
import { extractSessionIdFromClipboardText } from "../../state/session/sessionInformation";
import { copyTextToClipboard, readTextFromClipboard } from "../../utils/clipboard";
import AnchoredOverlay from "../AnchoredOverlay";
import WarmActionDialog from "../WarmActionDialog";
import type { SessionResourceExplorerController } from "../../hooks/useSessionResourceExplorer";

export interface SessionFolderContextMenu {
  workspaceId: string;
  folderId: string;
  parentNodeId: string | null;
  name: string;
  x: number;
  y: number;
}

export interface WorkspaceFolderContextMenu {
  nodeId: string | null;
  parentNodeId: string | null;
  name: string;
  x: number;
  y: number;
}

export interface WorkspaceFolderEditor {
  mode: "create" | "rename";
  nodeId: string | null;
  parentNodeId: string | null;
  value: string;
}

export type SessionResourceDialog =
  | {
      kind: "create_session_folder" | "rename_session_folder" | "delete_session_folder";
      workspaceId: string;
      folderId: string;
      parentNodeId: string | null;
      name: string;
    }
  | {
      kind: "delete_workspace_folder";
      nodeId: string;
      name: string;
    };

function extractFolderIdFromClipboardText(text: string): string {
  const match = text.trim().match(
    /(?:^|[^A-Za-z0-9_-])(fld_[A-Za-z0-9_-]+)(?:$|[^A-Za-z0-9_-])/,
  );
  if (!match) {
    throw new Error("剪贴板中没有有效的会话文件夹 ID；请先右键目标文件夹并复制 ID");
  }
  return match[1];
}

interface SessionResourceOverlaysProps {
  workspaceFolderMenu: WorkspaceFolderContextMenu | null;
  setWorkspaceFolderMenu: Dispatch<SetStateAction<WorkspaceFolderContextMenu | null>>;
  setWorkspaceFolderEditor: Dispatch<SetStateAction<WorkspaceFolderEditor | null>>;
  folderMenu: SessionFolderContextMenu | null;
  setFolderMenu: Dispatch<SetStateAction<SessionFolderContextMenu | null>>;
  resourceDialog: SessionResourceDialog | null;
  setResourceDialog: Dispatch<SetStateAction<SessionResourceDialog | null>>;
  explorer: SessionResourceExplorerController;
  onCreateSessionInFolder: (workspaceId: string, folderId: string) => Promise<void>;
  onSessionFolderDeleted: (
    workspaceId: string,
    deletedCurrentSession: boolean,
  ) => Promise<void>;
  onStatusChange: (message: string) => void;
  setActionError: Dispatch<SetStateAction<string | null>>;
  handleError: (prefix: string, error: unknown) => void;
}

export default function SessionResourceOverlays({
  workspaceFolderMenu,
  setWorkspaceFolderMenu,
  setWorkspaceFolderEditor,
  folderMenu,
  setFolderMenu,
  resourceDialog,
  setResourceDialog,
  explorer,
  onCreateSessionInFolder,
  onSessionFolderDeleted,
  onStatusChange,
  setActionError,
  handleError,
}: SessionResourceOverlaysProps): ReactNode {
  return (
    <>
    {workspaceFolderMenu ? (
      <AnchoredOverlay
        open
        point={workspaceFolderMenu}
        placement="bottom-start"
        offset={2}
        onClose={() => setWorkspaceFolderMenu(null)}
      >
        <div
          className="agent-sessions-session-menu session-resource-folder-menu"
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <button type="button" role="menuitem" onClick={() => {
            const target = workspaceFolderMenu;
            setWorkspaceFolderMenu(null);
            if (target.nodeId) {
              const expansionId = `navigation:${target.nodeId}`;
              if (!explorer.expandedIds.has(expansionId)) {
                explorer.toggleExpanded(expansionId);
              }
            }
            setWorkspaceFolderEditor({
              mode: "create",
              nodeId: null,
              parentNodeId: target.nodeId,
              value: "新建文件夹",
            });
          }}>
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{workspaceFolderMenu.nodeId ? "新建子文件夹" : "新建工作区文件夹"}</span>
          </button>
          {workspaceFolderMenu.nodeId ? (
            <>
              <button type="button" role="menuitem" onClick={() => {
                const target = workspaceFolderMenu;
                setWorkspaceFolderMenu(null);
                setWorkspaceFolderEditor({
                  mode: "rename",
                  nodeId: target.nodeId,
                  parentNodeId: target.parentNodeId,
                  value: target.name,
                });
              }}>
                <span className="codicon codicon-edit agent-sessions-menu-item-icon" aria-hidden="true" />
                <span className="agent-sessions-menu-item-label">重命名</span>
              </button>
              <button type="button" role="menuitem" onClick={() => {
                const target = workspaceFolderMenu;
                setWorkspaceFolderMenu(null);
                void copyTextToClipboard(target.nodeId as string)
                  .then(() => onStatusChange(`已复制工作区文件夹 ID: ${target.nodeId}`))
                  .catch((error) => handleError("复制工作区文件夹 ID 失败", error));
              }}>
                <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
                <span className="agent-sessions-menu-item-label">复制文件夹 ID</span>
              </button>
              {workspaceFolderMenu.parentNodeId ? (
                <button type="button" role="menuitem" onClick={() => {
                  const target = workspaceFolderMenu;
                  setWorkspaceFolderMenu(null);
                  void explorer.placeWorkspaceNode(
                    target.nodeId as string,
                    null,
                    "last",
                  )
                    .then(() => onStatusChange(`已将 ${target.name} 移动到工作区导航根`))
                    .catch((error) => handleError("移动工作区文件夹失败", error));
                }}>
                  <span className="codicon codicon-root-folder agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">移动到导航根</span>
                </button>
              ) : null}
              <button type="button" role="menuitem" className="danger agent-sessions-menu-item-separated" onClick={() => {
                const target = workspaceFolderMenu;
                setWorkspaceFolderMenu(null);
                setResourceDialog({
                  kind: "delete_workspace_folder",
                  nodeId: target.nodeId as string,
                  name: target.name,
                });
              }}>
                <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
                <span className="agent-sessions-menu-item-label">删除文件夹</span>
              </button>
            </>
          ) : null}
        </div>
      </AnchoredOverlay>
    ) : null}
    {folderMenu ? (
      <AnchoredOverlay
        open
        point={folderMenu}
        placement="bottom-start"
        offset={2}
        onClose={() => setFolderMenu(null)}
      >
        <div
          className="agent-sessions-session-menu session-resource-folder-menu"
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <button type="button" role="menuitem" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            void onCreateSessionInFolder(target.workspaceId, target.folderId)
              .then(() => explorer.loadBranch(target.workspaceId, target.folderId))
              .then(() => onStatusChange(`已在 ${target.name} 中创建会话`))
              .catch((error) => handleError("创建会话失败", error));
          }}>
            <span className="codicon codicon-comment-add agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">新建会话</span>
          </button>
          <button type="button" role="menuitem" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            setResourceDialog({
              kind: "create_session_folder",
              workspaceId: target.workspaceId,
              folderId: target.folderId,
              parentNodeId: target.parentNodeId,
              name: target.name,
            });
          }}>
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">新建子文件夹</span>
          </button>
          <button type="button" role="menuitem" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            void copyTextToClipboard(target.folderId)
              .then(() => onStatusChange(`已复制会话文件夹 ID: ${target.folderId}`))
              .catch((error) => handleError("复制文件夹 ID 失败", error));
          }}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">复制文件夹 ID</span>
          </button>
          <button type="button" role="menuitem" title="先复制会话 ID 或会话信息，再在目标文件夹执行此操作" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            void readTextFromClipboard()
              .then(extractSessionIdFromClipboardText)
              .then((sessionId) => explorer.assignSessionFolder(
                target.workspaceId,
                sessionId,
                target.folderId,
              ).then(() => sessionId))
              .then((sessionId) => onStatusChange(`已将会话 ${sessionId} 移动到 ${target.name}`))
              .catch((error) => handleError("移动剪贴板会话失败", error));
          }}>
            <span className="codicon codicon-move agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">将剪贴板会话移动到此处</span>
          </button>
          <button type="button" role="menuitem" title="先复制目标文件夹 ID，再在当前文件夹执行此操作" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            void readTextFromClipboard()
              .then(extractFolderIdFromClipboardText)
              .then((targetFolderId) => explorer.moveSessionFolder(
                target.workspaceId,
                target.folderId,
                targetFolderId,
                target.parentNodeId,
              ))
              .then(() => onStatusChange(`已移动文件夹 ${target.name}`))
              .catch((error) => handleError("移动文件夹失败", error));
          }}>
            <span className="codicon codicon-folder-opened agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">移动到剪贴板文件夹</span>
          </button>
          {folderMenu.parentNodeId ? (
            <button type="button" role="menuitem" onClick={() => {
              const target = folderMenu;
              setFolderMenu(null);
              void explorer.moveSessionFolder(
                target.workspaceId,
                target.folderId,
                null,
                target.parentNodeId,
              )
                .then(() => onStatusChange(`已将 ${target.name} 移动到工作区根目录`))
                .catch((error) => handleError("移动文件夹失败", error));
            }}>
              <span className="codicon codicon-root-folder agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">移动到工作区根目录</span>
            </button>
          ) : null}
          <button type="button" role="menuitem" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            setResourceDialog({
              kind: "rename_session_folder",
              workspaceId: target.workspaceId,
              folderId: target.folderId,
              parentNodeId: target.parentNodeId,
              name: target.name,
            });
          }}>
            <span className="codicon codicon-edit agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">重命名</span>
          </button>
          <button type="button" role="menuitem" className="danger agent-sessions-menu-item-separated" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            setResourceDialog({
              kind: "delete_session_folder",
              workspaceId: target.workspaceId,
              folderId: target.folderId,
              parentNodeId: target.parentNodeId,
              name: target.name,
            });
          }}>
            <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">删除文件夹及内容</span>
          </button>
        </div>
      </AnchoredOverlay>
    ) : null}
    <WarmActionDialog
      open={resourceDialog !== null}
      title={resourceDialog?.kind === "create_session_folder"
        ? "新建子文件夹"
        : resourceDialog?.kind === "rename_session_folder"
          ? "重命名会话文件夹"
          : resourceDialog?.kind === "delete_workspace_folder"
            ? "删除工作区文件夹"
            : "永久删除会话文件夹"}
      description={resourceDialog?.kind === "delete_workspace_folder"
        ? `删除虚拟文件夹“${resourceDialog.name}”及其中的子文件夹。内部工作区会移动到上一级，不会删除工作区或会话文件。`
        : resourceDialog?.kind === "delete_session_folder"
          ? `永久删除文件夹“${resourceDialog.name}”及其中全部子文件夹和会话。\n该操作会删除消息、检查点、日志、附件、工具结果和运行资源，无法撤销。`
          : resourceDialog?.kind === "create_session_folder"
            ? `创建位置：${resourceDialog.name}`
            : undefined}
      inputLabel={resourceDialog && (
        resourceDialog.kind === "create_session_folder"
        || resourceDialog.kind === "rename_session_folder"
      ) ? "文件夹名称" : undefined}
      initialValue={resourceDialog?.kind === "rename_session_folder"
        ? resourceDialog.name
        : "新建文件夹"}
      confirmText={resourceDialog?.kind.startsWith("delete_") ? "删除" : "保存"}
      danger={resourceDialog?.kind.startsWith("delete_") ?? false}
      onClose={() => setResourceDialog(null)}
      onConfirm={async (value) => {
        if (!resourceDialog) {
          throw new Error("会话资源操作目标已失效");
        }
        setActionError(null);
        if (resourceDialog.kind === "delete_workspace_folder") {
          await explorer.deleteWorkspaceFolder(resourceDialog.nodeId);
          onStatusChange(`已删除工作区文件夹 ${resourceDialog.name}`);
          return;
        }
        if (resourceDialog.kind === "create_session_folder") {
          await explorer.createSessionFolder(
            resourceDialog.workspaceId,
            value,
            resourceDialog.folderId,
          );
          onStatusChange(`已创建文件夹 ${value}`);
          return;
        }
        if (resourceDialog.kind === "rename_session_folder") {
          await explorer.renameSessionFolder(
            resourceDialog.workspaceId,
            resourceDialog.folderId,
            value,
            resourceDialog.parentNodeId,
          );
          onStatusChange(`已重命名文件夹为 ${value}`);
          return;
        }
        const deletedCurrentSession = await explorer.deleteSessionFolder(
          resourceDialog.workspaceId,
          resourceDialog.folderId,
          resourceDialog.parentNodeId,
        );
        await onSessionFolderDeleted(resourceDialog.workspaceId, deletedCurrentSession);
        onStatusChange(`已递归删除文件夹 ${resourceDialog.name}`);
      }}
    />
    </>
  );
}
