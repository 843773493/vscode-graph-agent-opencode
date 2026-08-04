import { extractSessionIdFromClipboardText } from "../../state/session/sessionInformation";
import {
  copyTextToClipboard,
  readTextFromClipboard,
} from "../../utils/clipboard";
import AnchoredOverlay from "../AnchoredOverlay";
import { useWarmConfirm } from "../WarmConfirmProvider";

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
  onCopyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  onRemoveWorkspace: (workspaceId: string, name: string) => void;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
  onStopWorkspace: (workspaceId: string) => Promise<void>;
  startingWorkspaceIds: ReadonlySet<string>;
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
  onCreateWorkspaceSession,
  onRequestCreateSessionFolder,
  onCopySessionInformation,
  onRenameWorkspace,
  onCopyWorkspaceInformation,
  onRemoveWorkspace,
  onStartWorkspace,
  onStopWorkspace,
  startingWorkspaceIds,
  onStatusChange,
}: AgentSessionsContextMenusProps) {
  const confirm = useWarmConfirm();

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

  const handleStopWorkspace = () => {
    if (!workspaceMenu) {
      return;
    }
    const target = workspaceMenu;
    onCloseWorkspaceMenu();
    void confirm({
      title: "关闭工作区",
      message: `关闭工作区“${target.name || target.workspaceId}”的后端服务。该工作区中的会话将暂时离线，正在运行的任务和终端可能中断。稍后可以重新启动。`,
      confirmText: "关闭",
      danger: true,
    }).then(async (confirmed) => {
      if (confirmed) {
        await onStopWorkspace(target.workspaceId);
      }
    }).catch((error: unknown) => {
      onStatusChange(
        `关闭工作区失败: ${error instanceof Error ? error.message : String(error)}`,
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
            <span className="agent-sessions-menu-item-label">新建子文件夹</span>
          </button>
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
          <button type="button" role="menuitem" title="将剪贴板中的会话移动并绑定为当前会话的子会话" onClick={handleBindClipboardSession}>
            <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">绑定为子会话</span>
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
              onRenameSession(target.sessionId, target.title, target.workspaceId);
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
                <span className="agent-sessions-menu-item-label">解除父会话绑定</span>
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
            <span className="agent-sessions-menu-item-label">删除会话</span>
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
          {canStartWorkspace ? (
            <button
              type="button"
              role="menuitem"
              disabled={startingWorkspaceIds.has(workspaceMenu.workspaceId)}
              onClick={() => {
                const target = workspaceMenu;
                onCloseWorkspaceMenu();
                void onStartWorkspace(target.workspaceId).catch((error: unknown) => {
                  onStatusChange(
                    `启动工作区失败: ${error instanceof Error ? error.message : String(error)}`,
                  );
                });
              }}
            >
              <span className={`codicon ${startingWorkspaceIds.has(workspaceMenu.workspaceId) ? "codicon-loading codicon-modifier-spin" : "codicon-play"} agent-sessions-menu-item-icon`} aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">
                {startingWorkspaceIds.has(workspaceMenu.workspaceId)
                  ? "正在启动"
                  : "启动工作区"}
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
                  onStatusChange(
                    `新建工作区会话失败: ${error instanceof Error ? error.message : String(error)}`,
                  );
                },
              );
            }}
          >
            <span className="codicon codicon-comment-add agent-sessions-menu-item-icon" aria-hidden="true" />
            <span className="agent-sessions-menu-item-label">新建会话</span>
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
            <span className="agent-sessions-menu-item-label">新建会话文件夹</span>
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
          {canStopWorkspace ? (
            <button
              type="button"
              role="menuitem"
              className="danger agent-sessions-menu-item-separated"
              onClick={handleStopWorkspace}
            >
              <span className="codicon codicon-debug-stop agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">关闭工作区</span>
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
              <span className="agent-sessions-menu-item-label">删除工作区</span>
            </button>
          ) : (
            <div className="agent-sessions-menu-disabled-note">
              默认工作区不能删除
            </div>
          )}
        </div>
        </AnchoredOverlay>
      ) : null}
    </>
  );
}
