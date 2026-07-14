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
  onRemoveWorkspace: (workspaceId: string, name: string) => void;
  onStatusChange: (message: string) => void;
}

let lastCopiedSessionId: string | null = null;

function fallbackCopyText(text: string): void {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  // TODO: 兼容非安全上下文下 Clipboard API 不可用的浏览器，后续全站 HTTPS 后移除。
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("浏览器拒绝复制会话 ID");
  }
}

async function copyTextToClipboard(text: string): Promise<void> {
  let clipboardError: unknown = null;
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      clipboardError = error;
    }
  }

  try {
    fallbackCopyText(text);
  } catch (fallbackError) {
    if (clipboardError) {
      const clipboardMessage = clipboardError instanceof Error ? clipboardError.message : String(clipboardError);
      const fallbackMessage = fallbackError instanceof Error ? fallbackError.message : String(fallbackError);
      throw new Error(`Clipboard API 失败：${clipboardMessage}；兼容复制失败：${fallbackMessage}`);
    }
    throw fallbackError;
  }
}

async function readSessionIdFromClipboard(): Promise<string> {
  let clipboardError: unknown = null;
  if (navigator.clipboard?.readText) {
    try {
      const clipboardText = (await navigator.clipboard.readText()).trim();
      if (clipboardText) {
        return clipboardText;
      }
    } catch (error) {
      clipboardError = error;
    }
  }
  if (lastCopiedSessionId) {
    return lastCopiedSessionId;
  }
  if (clipboardError) {
    const message = clipboardError instanceof Error ? clipboardError.message : String(clipboardError);
    throw new Error(`浏览器拒绝读取剪贴板，且应用内没有最近复制的会话 ID: ${message}`);
  }
  throw new Error("剪贴板中没有会话 ID，且应用内没有最近复制的会话 ID");
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
        lastCopiedSessionId = target.sessionId;
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
    void readSessionIdFromClipboard()
      .then((childSessionId) => {
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
          <button type="button" role="menuitem" title="将剪贴板中的会话 ID 绑定为当前会话的子会话" onClick={handleBindClipboardSession}>
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
          {workspaceMenu.removable ? (
            <button
              type="button"
              role="menuitem"
              className="danger"
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
