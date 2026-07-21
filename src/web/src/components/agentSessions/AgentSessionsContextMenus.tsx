import { extractSessionIdFromClipboardText } from "../../state/session/sessionInformation";
import {
  copyTextToClipboard,
  readTextFromClipboard,
} from "../../utils/clipboard";
import { extractWorkspaceIdFromClipboardText } from "../../state/workspaceInformation";

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
  x: number;
  y: number;
}

interface AgentSessionsContextMenusProps {
  sessionMenu: SessionContextMenu | null;
  workspaceMenu: WorkspaceContextMenu | null;
  onCloseSessionMenu: () => void;
  onCloseWorkspaceMenu: () => void;
  onRenameSession: (sessionId: string, title: string) => void;
  onDeleteSession: (sessionId: string, title: string) => void;
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
  onCopySessionInformation: (
    workspaceId: string,
    sessionId: string,
  ) => Promise<void>;
  onRenameWorkspace: (workspaceId: string) => void;
  onUnbindWorkspace: (workspaceId: string) => Promise<void>;
  onBindClipboardWorkspace: (
    workspaceId: string,
    parentWorkspaceId: string,
  ) => Promise<void>;
  onCopyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  onRemoveWorkspace: (workspaceId: string, name: string) => void;
  onStatusChange: (message: string) => void;
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
  onCopySessionInformation,
  onRenameWorkspace,
  onUnbindWorkspace,
  onBindClipboardWorkspace,
  onCopyWorkspaceInformation,
  onRemoveWorkspace,
  onStatusChange,
}: AgentSessionsContextMenusProps) {
  const handleCopySessionId = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    void copyTextToClipboard(target.sessionId)
      .then(() => {
        onStatusChange(`已复制会话 ID: ${target.sessionId}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`复制会话 ID 失败: ${message}`);
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
        onStatusChange(`已将 ${childSessionId} 绑定到 ${target.sessionId}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`绑定剪贴板会话失败: ${message}`);
      });
  };

  const handleCopySessionInformation = () => {
    if (!sessionMenu) {
      return;
    }
    const target = sessionMenu;
    onCloseSessionMenu();
    onStatusChange(`正在读取会话信息: ${target.sessionId}`);
    void onCopySessionInformation(target.workspaceId, target.sessionId)
      .then(() => {
        onStatusChange(`已复制会话信息: ${target.sessionId}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`复制会话信息失败: ${message}`);
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
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`从上下文创建子会话失败: ${message}`);
      },
    );
  };

  const handleCopyWorkspaceInformation = () => {
    if (!workspaceMenu) {
      return;
    }
    const target = workspaceMenu;
    onCloseWorkspaceMenu();
    onStatusChange(`正在读取工作区信息: ${target.workspaceId}`);
    void onCopyWorkspaceInformation(target.workspaceId)
      .then(() => {
        onStatusChange(`已复制工作区信息: ${target.workspaceId}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`复制工作区信息失败: ${message}`);
      });
  };

  const handleBindClipboardWorkspace = () => {
    if (!workspaceMenu) {
      return;
    }
    const target = workspaceMenu;
    onCloseWorkspaceMenu();
    void readTextFromClipboard()
      .then((clipboardText) =>
        extractWorkspaceIdFromClipboardText(clipboardText),
      )
      .then((childWorkspaceId) =>
        onBindClipboardWorkspace(childWorkspaceId, target.workspaceId).then(
          () => childWorkspaceId,
        ),
      )
      .then((childWorkspaceId) => {
        onStatusChange(
          `已将 ${childWorkspaceId} 绑定到 ${target.workspaceId}`,
        );
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`绑定剪贴板工作区失败: ${message}`);
      });
  };

  return (
    <>
      {sessionMenu ? (
        <div
          className="agent-sessions-session-menu"
          style={{ left: sessionMenu.x, top: sessionMenu.y }}
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <button type="button" role="menuitem" title="复制当前会话 ID" onClick={handleCopySessionId}>
            <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">复制 ID</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title="复制可供 Agent 和软件解析的通用会话信息"
            onClick={handleCopySessionInformation}
          >
            <span className="codicon codicon-info agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">复制会话信息</span>
          </button>
          <button type="button" role="menuitem" title="从剪贴板会话 ID 或通用会话信息中识别并绑定子会话" onClick={handleBindClipboardSession}>
            <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">粘贴为子会话</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title="仅复制当前 Agent 上下文状态，并创建为当前会话的子会话"
            onClick={handleForkSessionContext}
          >
            <span className="codicon codicon-git-branch agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">从上下文创建子会话</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              const target = sessionMenu;
              onCloseSessionMenu();
              onRenameSession(target.sessionId, target.title);
            }}
          >
            <span className="codicon codicon-edit agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">重命名</span>
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
              <span className="agent-sessions-menu-item-label">移出父会话</span>
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            className="danger agent-sessions-menu-item-separated"
            onClick={() => {
              const target = sessionMenu;
              onCloseSessionMenu();
              onDeleteSession(target.sessionId, target.title);
            }}
          >
            <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">删除会话</span>
          </button>
        </div>
      ) : null}
      {workspaceMenu ? (
        <div
          className="agent-sessions-session-menu agent-sessions-workspace-menu"
          style={{ left: workspaceMenu.x, top: workspaceMenu.y }}
          role="menu"
          onPointerDown={(event) => event.stopPropagation()}
        >
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
            <span className="agent-sessions-menu-item-label">重命名</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title="复制可供 Agent 和软件解析的通用工作区信息"
            onClick={handleCopyWorkspaceInformation}
          >
            <span className="codicon codicon-info agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">复制工作区信息</span>
          </button>
          <button
            type="button"
            role="menuitem"
            title="从剪贴板工作区 ID 或通用工作区信息中识别并绑定子工作区"
            onClick={handleBindClipboardWorkspace}
          >
            <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">粘贴为子工作区</span>
          </button>
          {workspaceMenu.parentWorkspaceId ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                const target = workspaceMenu;
                onCloseWorkspaceMenu();
                void onUnbindWorkspace(target.workspaceId).catch(
                  (error: unknown) => {
                    const message =
                      error instanceof Error ? error.message : String(error);
                    onStatusChange(`移出父工作区失败: ${message}`);
                  },
                );
              }}
            >
              <span className="codicon codicon-debug-disconnect agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">移出父工作区</span>
            </button>
          ) : null}
          {workspaceMenu.removable ? (
            <button
              type="button"
              role="menuitem"
              className="danger agent-sessions-menu-item-separated"
              onClick={() => {
                const target = workspaceMenu;
                onCloseWorkspaceMenu();
                onRemoveWorkspace(target.workspaceId, target.name);
              }}
            >
              <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">删除工作区</span>
            </button>
          ) : (
            <div className="agent-sessions-menu-disabled-note">
              默认工作区不能删除
            </div>
          )}
        </div>
      ) : null}
    </>
  );
}
