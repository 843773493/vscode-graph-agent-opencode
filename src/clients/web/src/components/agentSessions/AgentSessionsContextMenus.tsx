import { t } from '../../../../shared/core/i18n/index.js';
import { extractSessionIdFromClipboardText } from "../../state/session/sessionInformation";
import {
  copyTextToClipboard,
  readTextFromClipboard,
} from "../../utils/clipboard";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import { errorMessage, isErrorReported } from "../../utils/errorMessage";

export interface SessionContextMenu {
  sessionId: string;
  workspaceId: string;
  title: string;
  parentSessionId: string | null;
  x: number;
  y: number;
}

export interface WorkspaceContextMenu {
  workspaceId: string;
  name: string;
  parentWorkspaceId: string | null;
  removable: boolean;
  managed: boolean;
  systemDefault: boolean;
  status: "ready" | "offline";
  x: number;
  y: number;
}

interface AgentSessionsContextMenusProps {
  sessionMenu: SessionContextMenu | null;
  workspaceMenu: WorkspaceContextMenu | null;
  onCloseSessionMenu: () => void;
  onCloseWorkspaceMenu: () => void;
  onRenameSession: (sessionId: string, title: string, workspaceId: string) => void;
  onDeleteSession: (sessionId: string, title: string, workspaceId: string) => void;
  onUnbindSession: (sessionId: string, workspaceId: string) => void;
  onBindClipboardSession: (
    sessionId: string,
    parentSessionId: string,
    workspaceId: string,
  ) => Promise<void>;
  onForkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  onCreateWorkspaceSession: (workspaceId: string, workspaceName: string) => Promise<void>;
  onRequestCreateSessionFolder: (
    workspaceId: string,
    parentNodeId: string | null,
    locationName: string,
  ) => void;
  onCopySessionInformation: (
    workspaceId: string,
    sessionId: string,
  ) => Promise<void>;
  onRenameWorkspace: (workspaceId: string) => void;
  onOpenWorkspaceDetails: (workspaceId: string) => Promise<void>;
  onCopyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  onRemoveWorkspace: (workspaceId: string, name: string) => void;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
  onStopWorkspace: (workspaceId: string) => Promise<void>;
  startingWorkspaceIds: ReadonlySet<string>;
  onStatusChange: (message: string) => void;
  onError: (message: string) => void;
}

export default function AgentSessionsContextMenus({
  sessionMenu,
  workspaceMenu,
  onCloseSessionMenu,
  onCloseWorkspaceMenu,
  onRenameSession,
  onDeleteSession,
  onUnbindSession,
  onBindClipboardSession,
  onForkSessionContext,
  onCreateWorkspaceSession,
  onRequestCreateSessionFolder,
  onCopySessionInformation,
  onRenameWorkspace,
  onOpenWorkspaceDetails,
  onCopyWorkspaceInformation,
  onRemoveWorkspace,
  onStartWorkspace,
  onStopWorkspace,
  startingWorkspaceIds,
  onStatusChange,
  onError,
}: AgentSessionsContextMenusProps) {

  const handleCopySessionId = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    void copyTextToClipboard(target.sessionId)
      .then(() => {
        onStatusChange(t('navigation.clipboard.sessionIdCopied', target.sessionId));
      })
      .catch((error: unknown) => {
        if (isErrorReported(error)) return;
        const message = errorMessage(error);
        onError(t('navigation.clipboard.sessionIdFailed', target.workspaceId, target.sessionId, message));
      });
  };

  const handleBindClipboardSession = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    void readTextFromClipboard()
      .then((clipboardText) => {
        const childSessionId = extractSessionIdFromClipboardText(clipboardText);
        return onBindClipboardSession(
          childSessionId,
          target.sessionId,
          target.workspaceId,
        ).then(() => childSessionId);
      })
      .then((childSessionId) => {
        onStatusChange(t('navigation.sessions.bound', childSessionId, target.sessionId));
      })
      .catch((error: unknown) => {
        if (isErrorReported(error)) return;
        const message = errorMessage(error);
        onError(t('navigation.clipboard.bindSessionFailed', target.workspaceId, target.sessionId, message));
      });
  };

  const handleCopySessionInformation = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    onStatusChange(t('navigation.clipboard.readingSession', target.sessionId));
    void onCopySessionInformation(target.workspaceId, target.sessionId)
      .then(() => {
        onStatusChange(t('navigation.clipboard.sessionCopied', target.sessionId));
      })
      .catch((error: unknown) => {
        if (isErrorReported(error)) return;
        const message = errorMessage(error);
        onError(t('navigation.clipboard.sessionFailed', target.workspaceId, target.sessionId, message));
      });
  };

  const handleForkSessionContext = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    void onForkSessionContext(target.workspaceId, target.sessionId).catch(
      (error: unknown) => {
        if (isErrorReported(error)) return;
        const message = errorMessage(error);
        onError(t('navigation.fork.createOwnerFailed', target.workspaceId, target.sessionId, message));
      },
    );
  };

  const handleCopyWorkspaceInformation = () => {
    if (!workspaceMenu) {
      return;
    }
    const target = workspaceMenu;
    onCloseWorkspaceMenu();
    onStatusChange(t('navigation.clipboard.readingWorkspace', target.workspaceId));
    void onCopyWorkspaceInformation(target.workspaceId)
      .then(() => {
        onStatusChange(t('navigation.clipboard.workspaceCopied', target.workspaceId));
      })
      .catch((error: unknown) => {
        if (isErrorReported(error)) return;
        const message = errorMessage(error);
        onError(t('navigation.clipboard.workspaceFailed', target.workspaceId, message));
      });
  };

  const handleStopWorkspace = () => {
    if (!workspaceMenu) {
      return;
    }
    const target = workspaceMenu;
    onCloseWorkspaceMenu();
    void onStopWorkspace(target.workspaceId).catch((error: unknown) => {
      if (isErrorReported(error)) return;
      onError(
        t('navigation.workspace.stopFailed', target.workspaceId, errorMessage(error)),
      );
    });
  };

  const canStartWorkspace = Boolean(
    workspaceMenu?.managed
    && !workspaceMenu.systemDefault
    && workspaceMenu.status === "offline",
  );
  const canStopWorkspace = Boolean(
    workspaceMenu?.managed
    && !workspaceMenu.systemDefault
    && workspaceMenu.status === "ready",
  );

  return (
    <>
      {sessionMenu ? (
        <AnchoredOverlay
          open
          point={sessionMenu}
          placement="bottom-start"
          offset={2}
          onClose={onCloseSessionMenu}
        >
        <div
          className="agent-sessions-session-menu"
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              const target = sessionMenu;
              onCloseSessionMenu();
              onRequestCreateSessionFolder(
                target.workspaceId,
                target.sessionId,
                target.title || target.sessionId,
              );
            }}
          >
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.folder.newChild')}</span>
          </button>
          <button type="button" role="menuitem" title={t('navigation.clipboard.copySessionIdHint')} onClick={handleCopySessionId}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.copyId')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title={t('navigation.clipboard.copySessionHint')}
            onClick={handleCopySessionInformation}
          >
            <span className="codicon codicon-info agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.copySession')}</span>
          </button>
          <button type="button" role="menuitem" title={t('navigation.clipboard.bindSessionHint')} onClick={handleBindClipboardSession}>
            <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.sessions.bindChild')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title={t('navigation.fork.createHint')}
            onClick={handleForkSessionContext}
          >
            <span className="codicon codicon-git-branch agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.fork.fromContext')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              const target = sessionMenu;
              onCloseSessionMenu();
              onRenameSession(target.sessionId, target.title, target.workspaceId);
            }}
          >
            <span className="codicon codicon-edit agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.rename')}</span>
          </button>
          {sessionMenu.parentSessionId ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                const target = sessionMenu;
                onCloseSessionMenu();
                onUnbindSession(target.sessionId, target.workspaceId);
              }}
            >
              <span className="codicon codicon-debug-disconnect agent-sessions-menu-item-icon" aria-hidden="true" />
                <span className="agent-sessions-menu-item-label">{t('navigation.sessions.unbindParent')}</span>
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            className="danger agent-sessions-menu-item-separated"
            onClick={() => {
              const target = sessionMenu;
              onCloseSessionMenu();
              onDeleteSession(target.sessionId, target.title, target.workspaceId);
            }}
          >
            <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.sessions.delete')}</span>
          </button>
        </div>
        </AnchoredOverlay>
      ) : null}
      {workspaceMenu ? (
        <AnchoredOverlay
          open
          point={workspaceMenu}
          placement="bottom-start"
          offset={2}
          onClose={onCloseWorkspaceMenu}
        >
        <div
          className="agent-sessions-session-menu agent-sessions-workspace-menu"
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <button type="button" role="menuitem" onClick={() => {
            const target = workspaceMenu;
            onCloseWorkspaceMenu();
            void onOpenWorkspaceDetails(target.workspaceId).catch((error: unknown) => {
              if (!isErrorReported(error)) onError(t('gateway.workspaceDetails.openFailed', target.workspaceId, errorMessage(error)));
            });
          }}>
            <span className="codicon codicon-info agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('gateway.workspaceDetails.title')}</span>
          </button>
          {canStartWorkspace ? (
            <button
              type="button"
              role="menuitem"
              disabled={startingWorkspaceIds.has(workspaceMenu.workspaceId)}
              onClick={() => {
                const target = workspaceMenu;
                onCloseWorkspaceMenu();
                void onStartWorkspace(target.workspaceId).catch((error: unknown) => {
                  if (isErrorReported(error)) return;
                  onError(
                    t('navigation.workspace.startFailed', target.workspaceId, errorMessage(error)),
                  );
                });
              }}
            >
              <span className={`codicon ${startingWorkspaceIds.has(workspaceMenu.workspaceId) ? "codicon-loading codicon-modifier-spin" : "codicon-play"} agent-sessions-menu-item-icon`} aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">
                {startingWorkspaceIds.has(workspaceMenu.workspaceId)
                  ? t('navigation.workspace.startingShort')
                  : t('navigation.workspace.start')}
              </span>
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            disabled={
              workspaceMenu.status === "offline"
              || startingWorkspaceIds.has(workspaceMenu.workspaceId)
            }
            onClick={() => {
              const target = workspaceMenu;
              onCloseWorkspaceMenu();
              void onCreateWorkspaceSession(target.workspaceId, target.name).catch(
                (error: unknown) => {
                  if (isErrorReported(error)) return;
                  onError(
                    t('navigation.sessions.createInWorkspaceFailed', target.workspaceId, errorMessage(error)),
                  );
                },
              );
            }}
          >
            <span className="codicon codicon-comment-add agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.sessions.new')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={workspaceMenu.status === "offline"}
            onClick={() => {
              const target = workspaceMenu;
              onCloseWorkspaceMenu();
              onRequestCreateSessionFolder(target.workspaceId, null, target.name);
            }}
          >
            <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.folder.newSessionFolder')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              const target = workspaceMenu;
              onCloseWorkspaceMenu();
              onRenameWorkspace(target.workspaceId);
            }}
          >
            <span className="codicon codicon-edit agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.rename')}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title={t('navigation.clipboard.copyWorkspaceHint')}
            onClick={handleCopyWorkspaceInformation}
          >
            <span className="codicon codicon-info agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">{t('navigation.clipboard.copyWorkspace')}</span>
          </button>
          {canStopWorkspace ? (
            <button
              type="button"
              role="menuitem"
              className="danger agent-sessions-menu-item-separated"
              onClick={handleStopWorkspace}
            >
              <span className="codicon codicon-debug-stop agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">{t('navigation.workspace.stop')}</span>
            </button>
          ) : null}
          {workspaceMenu.removable ? (
            <button
              type="button"
              role="menuitem"
              className={`danger${canStopWorkspace ? "" : " agent-sessions-menu-item-separated"}`}
              onClick={() => {
                const target = workspaceMenu;
                onCloseWorkspaceMenu();
                onRemoveWorkspace(target.workspaceId, target.name);
              }}
            >
              <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">{t('navigation.workspace.delete')}</span>
            </button>
          ) : (
            <div className="agent-sessions-menu-disabled-note">
              {t('navigation.workspace.defaultCannotDelete')}
            </div>
          )}
        </div>
        </AnchoredOverlay>
      ) : null}
    </>
  );
}
