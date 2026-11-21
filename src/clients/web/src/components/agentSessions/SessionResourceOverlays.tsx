import { t } from '../../../../shared/core/i18n/index.js';
import type { Dispatch, ReactNode, SetStateAction } from "react";
import {
  extractSessionIdFromClipboardText,
  isSessionId,
} from "../../state/session/sessionInformation";
import { copyTextToClipboard, readTextFromClipboard } from "../../utils/clipboard";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import WarmActionDialog from "../overlays/WarmActionDialog";
import type { SessionResourceExplorerController } from "../../hooks/session/useSessionResourceExplorer";

export interface SessionFolderContextMenu {
  workspaceId: string;
  nodeId: string;
  folderId: string | null;
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
    // 后端 canonical：folder 与 session 同表、id 一律 ses_ + 32 位小写 hex
    // （app/core/session_catalog_store.py:create_folder 调 validate_session_id），
    // 后端从不产出任何其它文件夹专用前缀。这里收敛到 ses_ 并复用唯一校验器
    // isSessionId，不再自持文件夹专用正则。
    /(?:^|[^A-Za-z0-9_-])(ses_[0-9a-f]{32})(?:$|[^A-Za-z0-9_-])/,
  );
  if (!match || !isSessionId(match[1])) {
    throw new Error(t('navigation.clipboard.folderIdRequired'));
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
  onClearError: () => void;
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
  onClearError,
  handleError,
}: SessionResourceOverlaysProps): ReactNode {
  const canonicalFolderMenu = folderMenu?.folderId
    ? { ...folderMenu, folderId: folderMenu.folderId }
    : null;
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
              value: t('navigation.folder.newName'),
            });
          }}>
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.workspace.newFolder')}</span>
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
                <span className="agent-sessions-menu-item-label">{t('navigation.rename')}</span>
              </button>
              <button type="button" role="menuitem" onClick={() => {
                const target = workspaceFolderMenu;
                setWorkspaceFolderMenu(null);
                void copyTextToClipboard(target.nodeId as string)
                  .then(() => onStatusChange(t('navigation.clipboard.workspaceFolderIdCopied', target.nodeId)))
                  .catch((error) => handleError(t('navigation.clipboard.workspaceFolderIdFailed'), error));
              }}>
                <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
                <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.copyFolderId')}</span>
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
                    .then(() => onStatusChange(t('navigation.workspace.folderMovedToRoot', target.name)))
                    .catch((error) => handleError(t('navigation.workspace.moveFolderFailed'), error));
                }}>
                  <span className="codicon codicon-root-folder agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">{t('navigation.workspace.moveToNavigationRoot')}</span>
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
                <span className="agent-sessions-menu-item-label">{t('navigation.folder.delete')}</span>
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
          {canonicalFolderMenu ? <button type="button" role="menuitem" onClick={() => {
            const target = canonicalFolderMenu;
            setFolderMenu(null);
            void onCreateSessionInFolder(target.workspaceId, target.folderId)
              .then(() => explorer.loadBranch(target.workspaceId, target.folderId))
              .then(() => onStatusChange(t('navigation.sessions.createdInFolder', target.name)))
              .catch((error) => handleError(t('navigation.sessions.createFailed'), error));
          }}>
            <span className="codicon codicon-comment-add agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.sessions.new')}</span>
          </button> : null}
          <button type="button" role="menuitem" onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            setResourceDialog({
              kind: "create_session_folder",
              workspaceId: target.workspaceId,
              folderId: target.nodeId,
              parentNodeId: target.parentNodeId,
              name: target.name,
            });
          }}>
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.folder.newSessionFolder')}</span>
          </button>
          {canonicalFolderMenu ? <button type="button" role="menuitem" onClick={() => {
            const target = canonicalFolderMenu;
            setFolderMenu(null);
            void copyTextToClipboard(target.folderId)
              .then(() => onStatusChange(t('navigation.clipboard.sessionFolderIdCopied', target.folderId)))
              .catch((error) => handleError(t('navigation.clipboard.folderIdFailed'), error));
          }}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.copyFolderId')}</span>
          </button> : null}
          <button type="button" role="menuitem" title={t('navigation.clipboard.moveSessionHint')} onClick={() => {
            const target = folderMenu;
            setFolderMenu(null);
            void readTextFromClipboard()
              .then(extractSessionIdFromClipboardText)
              .then((sessionId) => explorer.assignSessionFolder(
                target.workspaceId,
                sessionId,
                target.nodeId,
              ).then(() => sessionId))
              .then((sessionId) => onStatusChange(t('navigation.clipboard.sessionMovePending', sessionId, target.name)))
              .catch((error) => handleError(t('navigation.clipboard.moveSessionFailed'), error));
          }}>
            <span className="codicon codicon-move agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.moveSessionHere')}</span>
          </button>
          {canonicalFolderMenu ? <>
          <button type="button" role="menuitem" title={t('navigation.clipboard.moveFolderHint')} onClick={() => {
            const target = canonicalFolderMenu;
            setFolderMenu(null);
            void readTextFromClipboard()
              .then(extractFolderIdFromClipboardText)
              .then((targetFolderId) => explorer.moveSessionFolder(
                target.workspaceId,
                target.folderId,
                targetFolderId,
                target.parentNodeId,
              ))
              .then(() => onStatusChange(t('navigation.folder.movePending', target.name)))
              .catch((error) => handleError(t('navigation.folder.moveFailed'), error));
          }}>
            <span className="codicon codicon-folder-opened agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.moveToFolder')}</span>
          </button>
          {canonicalFolderMenu.parentNodeId ? (
            <button type="button" role="menuitem" onClick={() => {
              const target = canonicalFolderMenu;
              setFolderMenu(null);
              void explorer.moveSessionFolder(
                target.workspaceId,
                target.folderId,
                null,
                target.parentNodeId,
              )
                .then(() => onStatusChange(t('navigation.folder.moveToRootPending', target.name)))
                .catch((error) => handleError(t('navigation.folder.moveFailed'), error));
            }}>
              <span className="codicon codicon-root-folder agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">{t('navigation.folder.moveToRoot')}</span>
            </button>
          ) : null}
          <button type="button" role="menuitem" onClick={() => {
            const target = canonicalFolderMenu;
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
            <span className="agent-sessions-menu-item-label">{t('navigation.rename')}</span>
          </button>
          <button type="button" role="menuitem" className="danger agent-sessions-menu-item-separated" onClick={() => {
            const target = canonicalFolderMenu;
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
            <span className="agent-sessions-menu-item-label">{t('navigation.folder.deleteContents')}</span>
          </button>
          </> : <div role="status">{t('navigation.folder.syncingHint')}</div>}
        </div>
      </AnchoredOverlay>
    ) : null}
    <WarmActionDialog
      open={resourceDialog !== null}
      title={resourceDialog?.kind === "create_session_folder"
        ? t('navigation.folder.newSessionFolder')
        : resourceDialog?.kind === "rename_session_folder"
          ? t('navigation.folder.renameSessionFolder')
          : resourceDialog?.kind === "delete_workspace_folder"
            ? t('navigation.workspace.deleteFolder')
            : t('navigation.folder.permanentlyDelete')}
      description={resourceDialog?.kind === "delete_workspace_folder"
        ? t('navigation.workspace.deleteFolderDescription', resourceDialog.name)
        : resourceDialog?.kind === "delete_session_folder"
          ? t('navigation.folder.deleteDescription', resourceDialog.name)
          : resourceDialog?.kind === "create_session_folder"
            ? t('navigation.folder.createLocation', resourceDialog.name)
            : undefined}
      inputLabel={resourceDialog && (
        resourceDialog.kind === "create_session_folder"
        || resourceDialog.kind === "rename_session_folder"
      ) ? t('navigation.folder.name') : undefined}
      initialValue={resourceDialog?.kind === "rename_session_folder"
        ? resourceDialog.name
        : t('navigation.folder.newName')}
      confirmText={resourceDialog?.kind.startsWith("delete_") ? t('navigation.delete')
        : resourceDialog?.kind === "create_session_folder" ? t('navigation.create') : t('navigation.save')}
      danger={resourceDialog?.kind.startsWith("delete_") ?? false}
      onClose={() => setResourceDialog(null)}
      onConfirm={async (value) => {
        if (!resourceDialog) {
          throw new Error(t('navigation.catalog.actionTargetExpired'));
        }
        onClearError();
        if (resourceDialog.kind === "delete_workspace_folder") {
          await explorer.deleteWorkspaceFolder(resourceDialog.nodeId);
          onStatusChange(t('navigation.workspace.folderDeleted', resourceDialog.name));
          return;
        }
        if (resourceDialog.kind === "create_session_folder") {
          await explorer.createSessionFolder(
            resourceDialog.workspaceId,
            value,
            resourceDialog.folderId,
          );
          onStatusChange(t('navigation.folder.createPending', value));
          return;
        }
        if (resourceDialog.kind === "rename_session_folder") {
          await explorer.renameSessionFolder(
            resourceDialog.workspaceId,
            resourceDialog.folderId,
            value,
            resourceDialog.parentNodeId,
          );
          onStatusChange(t('navigation.folder.renamePending', value));
          return;
        }
        const { operationId, completion } = await explorer.deleteSessionFolder(
          resourceDialog.workspaceId,
          resourceDialog.folderId,
          resourceDialog.parentNodeId,
        );
        const { workspaceId, name } = resourceDialog;
        onStatusChange(t('navigation.folder.deletePending', name, operationId));
        void completion.then(async (deletedCurrentSession) => {
          await onSessionFolderDeleted(workspaceId, deletedCurrentSession);
          onStatusChange(t('navigation.folder.recursivelyDeleted', name));
        }).catch((error: unknown) => handleError(t('navigation.folder.recursiveDeleteIncomplete'), error));
      }}
    />
    </>
  );
}
