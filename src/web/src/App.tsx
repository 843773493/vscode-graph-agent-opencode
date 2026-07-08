import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import Composer from "./components/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import HistoryPanel from "./components/HistoryPanel";
import RequestLogPanel from "./components/RequestLogPanel";
import ResourcePanel from "./components/ResourcePanel";
import SessionNameDialog from "./components/SessionNameDialog";
import Toolbar from "./components/Toolbar";
import WorkspaceFilePreviewArea from "./components/WorkspaceFilePreviewArea";
import WorkspaceFileTree from "./components/WorkspaceFileTree";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { DEFAULT_BACKEND_PORT, getWorkspaceFileContent } from "./api";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";
import type { WorkspaceFileContent, WorkspaceFileNode } from "./types/backend";

type SessionNameDialogState = { sessionId: string; initialTitle: string };
type AuxiliaryTab = "changes" | "files";
type LayoutResizeTarget = "history-right" | "preview-left" | "auxiliary-left";

const DEFAULT_HISTORY_PANEL_WIDTH = 300;
const MIN_HISTORY_PANEL_WIDTH = 220;
const MAX_HISTORY_PANEL_WIDTH = 420;
const DEFAULT_PREVIEW_WIDTH = 520;
const MIN_PREVIEW_WIDTH = 280;
const MAX_PREVIEW_WIDTH = 920;
const DEFAULT_AUXILIARY_WIDTH = 335;
const MIN_AUXILIARY_WIDTH = 280;
const MAX_AUXILIARY_WIDTH = 520;
const LAYOUT_RESIZING_CLASS = "is-layout-resizing";

function clampLayoutWidth(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function defaultAuxiliaryVisible(): boolean {
  if (typeof window === "undefined") {
    return true;
  }
  return window.innerWidth > 900;
}

function defaultPreviewWidth(): number {
  if (typeof window === "undefined") {
    return DEFAULT_PREVIEW_WIDTH;
  }
  return window.innerWidth <= 1180 ? 340 : DEFAULT_PREVIEW_WIDTH;
}

export default function AppShell() {
  const {
    state,
    selectSession,
    toggleHistoryPanel,
    createSession,
    renameSession,
    deleteSession,
    refreshSessionResources,
    controlSessionResource,
    switchContentView,
    setStatus,
  } = useAppState();
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [auxiliaryTab, setAuxiliaryTab] = useState<AuxiliaryTab>("changes");
  const [auxiliaryVisible, setAuxiliaryVisible] = useState(defaultAuxiliaryVisible);
  const [fileTreeSearchOpen, setFileTreeSearchOpen] = useState(false);
  const [fileTreeCollapseVersion, setFileTreeCollapseVersion] = useState(0);
  const [historyPanelWidth, setHistoryPanelWidth] = useState(DEFAULT_HISTORY_PANEL_WIDTH);
  const [previewVisible, setPreviewVisible] = useState(false);
  const [previewWidth, setPreviewWidth] = useState(defaultPreviewWidth);
  const [auxiliaryWidth, setAuxiliaryWidth] = useState(DEFAULT_AUXILIARY_WIDTH);
  const [previewTabs, setPreviewTabs] = useState<WorkspaceFileContent[]>([]);
  const [activePreviewPath, setActivePreviewPath] = useState<string | null>(null);
  const [previewLoadingPath, setPreviewLoadingPath] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const cleanupLayoutResizeRef = useRef<(() => void) | null>(null);
  const activeSession = state.currentSession;

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const mediaQuery = window.matchMedia("(max-width: 900px)");
    const syncAuxiliaryVisibility = () => {
      if (mediaQuery.matches) {
        setAuxiliaryVisible(false);
      }
    };

    syncAuxiliaryVisibility();
    mediaQuery.addEventListener("change", syncAuxiliaryVisibility);
    return () => {
      mediaQuery.removeEventListener("change", syncAuxiliaryVisibility);
    };
  }, []);

  useEffect(() => {
    setPreviewTabs([]);
    setActivePreviewPath(null);
    setPreviewLoadingPath(null);
    setPreviewError(null);
  }, [state.workspaceRoot]);

  useEffect(() => {
    return () => {
      cleanupLayoutResizeRef.current?.();
    };
  }, []);

  const conversations = activeSession
    ? getConversationsForSession(activeSession.session_id, state)
    : [];
  const receivedEvents = activeSession
    ? (state.eventQueuesBySession.get(activeSession.session_id) ?? [])
    : [];
  const sortedSessions = [...state.sessions].sort(
    (a, b) =>
      new Date(b.updated_at || b.created_at || "").getTime() -
      new Date(a.updated_at || a.created_at || "").getTime(),
  );
  const historyVisible = state.historyPanelOpen;
  const startLayoutResize = (
    target: LayoutResizeTarget,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.preventDefault();
    cleanupLayoutResizeRef.current?.();

    const startX = event.clientX;
    const startHistoryPanelWidth = historyPanelWidth;
    const startPreviewWidth = previewWidth;
    const startAuxiliaryWidth = auxiliaryWidth;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaX = moveEvent.clientX - startX;
      if (target === "history-right") {
        setHistoryPanelWidth(
          clampLayoutWidth(
            startHistoryPanelWidth + deltaX,
            MIN_HISTORY_PANEL_WIDTH,
            MAX_HISTORY_PANEL_WIDTH,
          ),
        );
        return;
      }

      if (target === "preview-left") {
        setPreviewWidth(
          clampLayoutWidth(
            startPreviewWidth - deltaX,
            MIN_PREVIEW_WIDTH,
            MAX_PREVIEW_WIDTH,
          ),
        );
        return;
      }

      setAuxiliaryWidth(
        clampLayoutWidth(
          startAuxiliaryWidth - deltaX,
          MIN_AUXILIARY_WIDTH,
          MAX_AUXILIARY_WIDTH,
        ),
      );
    };

    const finishResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.classList.remove(LAYOUT_RESIZING_CLASS);
      cleanupLayoutResizeRef.current = null;
    };

    document.body.classList.add(LAYOUT_RESIZING_CLASS);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", finishResize);
    window.addEventListener("pointercancel", finishResize);
    cleanupLayoutResizeRef.current = finishResize;
  };
  const handleCreateSession = () => {
    setNameDialog(null);
    setNameDialogError(null);
    void createSession().catch(() => {
      // 创建失败时由 createSession 写入全局状态，这里只避免未处理 Promise。
    });
  };
  const handleRenameSession = (sessionId: string, currentTitle: string) => {
    setNameDialog({
      sessionId,
      initialTitle: currentTitle || "新会话",
    });
    setNameDialogError(null);
  };
  const handleDeleteSession = (sessionId: string, title: string) => {
    const label = title || sessionId;
    const confirmed = window.confirm(
      `删除会话「${label}」？相关后台任务会一并清理。`,
    );
    if (!confirmed) {
      return;
    }
    void deleteSession(sessionId).catch(() => {
      // 删除失败时由 deleteSession 写入全局状态，这里只避免未处理 Promise。
    });
  };
  const closeNameDialog = () => {
    if (nameDialogSubmitting) {
      return;
    }
    setNameDialog(null);
    setNameDialogError(null);
  };
  const submitNameDialog = (title: string) => {
    if (!nameDialog) {
      return;
    }

    setNameDialogSubmitting(true);
    setNameDialogError(null);
    const action = renameSession(nameDialog.sessionId, title);

    void action
      .then(() => {
        setNameDialog(null);
      })
      .catch((error: unknown) => {
        setNameDialogError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        setNameDialogSubmitting(false);
      });
  };
  const findLatestResponseInConversation = (marker: Element | null) => {
    let target: Element | null = marker;
    let cursor = marker?.nextElementSibling ?? null;
    while (cursor && !cursor.classList.contains("conversation-marker")) {
      if (cursor.classList.contains("event-card-response")) {
        target = cursor;
      }
      cursor = cursor.nextElementSibling;
    }
    return target;
  };
  const showConversation = (jobId?: string) => {
    switchContentView("default");
    if (!jobId) {
      window.setTimeout(() => {
        const markers = document.querySelectorAll(".conversation-marker");
        const marker = markers.length > 0 ? markers[markers.length - 1] : null;
        const target = findLatestResponseInConversation(marker);
        if (target instanceof HTMLElement) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
        const stream = document.querySelector<HTMLElement>(".chat-stream");
        stream?.scrollTo({ top: stream.scrollHeight, behavior: "smooth" });
      }, 80);
      return;
    }

    window.setTimeout(() => {
      const escapedJobId = jobId.replace(/["\\]/g, "\\$&");
      const marker = document.querySelector(`[data-job-id="${escapedJobId}"]`);
      const target = findLatestResponseInConversation(marker);
      const targetElement = target instanceof HTMLElement ? target : null;
      targetElement?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  };

  const openWorkspaceFilePreview = (node: WorkspaceFileNode) => {
    if (node.kind !== "file" && node.kind !== "symlink" && node.kind !== "other") {
      return;
    }

    const existingTab = previewTabs.find((tab) => tab.path === node.path);
    if (existingTab) {
      setPreviewVisible(true);
      setActivePreviewPath(existingTab.path);
      setPreviewError(null);
      setStatus(`已切换预览: ${existingTab.path}`);
      return;
    }

    setPreviewVisible(true);
    setActivePreviewPath(node.path);
    setPreviewLoadingPath(node.path);
    setPreviewError(null);
    setStatus(`正在读取文件: ${node.path}`);

    void getWorkspaceFileContent(state.apiPort ?? DEFAULT_BACKEND_PORT, node.path)
      .then((content) => {
        setPreviewTabs((prev) => {
          const withoutDuplicate = prev.filter((tab) => tab.path !== content.path);
          return [...withoutDuplicate, content];
        });
        setActivePreviewPath(content.path);
        setStatus(`已打开预览: ${content.path}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setPreviewError(message);
        setStatus(`文件预览失败: ${message}`);
      })
      .finally(() => {
        setPreviewLoadingPath(null);
      });
  };

  const closeWorkspaceFilePreview = (path: string) => {
    setPreviewTabs((prev) => {
      const closedIndex = prev.findIndex((tab) => tab.path === path);
      const nextTabs = prev.filter((tab) => tab.path !== path);
      if (activePreviewPath === path) {
        const fallbackTab = nextTabs[Math.max(0, closedIndex - 1)] ?? nextTabs[0] ?? null;
        setActivePreviewPath(fallbackTab?.path ?? null);
      }
      return nextTabs;
    });
  };

  const renderContentView = () => {
    if (state.contentView === "agent") {
      return (
        <AgentStatePanel
          jsonl={state.agentStateJsonl}
          messageCount={state.agentStateMessageCount}
          loadedAt={state.agentStateLoadedAt}
          loading={state.agentStateLoading}
          error={state.agentStateError}
        />
      );
    }

    if (state.contentView === "events") {
      return (
        <EventQueuePanel
          items={receivedEvents}
          limit={FRONTEND_EVENT_QUEUE_LIMIT}
          sessionId={activeSession?.session_id ?? ""}
        />
      );
    }

    if (state.contentView === "requests") {
      return (
        <RequestLogPanel
          logs={state.llmRequestLogs}
          loading={state.llmRequestLogsLoading}
          error={state.llmRequestLogsError}
          loadedAt={state.llmRequestLogsLoadedAt}
          sessionId={activeSession?.session_id ?? ""}
        />
      );
    }

    if (state.contentView === "resources") {
      return (
        <ResourcePanel
          resources={state.sessionResources}
          loading={state.sessionResourcesLoading}
          error={state.sessionResourcesError}
          loadedAt={state.sessionResourcesLoadedAt}
          sessionId={activeSession?.session_id ?? ""}
          onRefresh={() => {
            if (activeSession) {
              void refreshSessionResources(activeSession.session_id);
            }
          }}
          onControl={controlSessionResource}
          onShowConversation={showConversation}
        />
      );
    }

    return (
      <ChatPanel
        conversations={conversations}
        expandDetails={state.expandDetails}
      />
    );
  };

  const renderAuxiliaryPanel = () => {
    if (auxiliaryTab === "changes") {
      return (
        <div className="auxiliary-view-body auxiliary-changes-body">
          <div className="auxiliary-actions-row">
            <button type="button" disabled title="等待后端提供 diff 数据">
              打开更改
            </button>
            <button
              type="button"
              onClick={() => setStatus("已刷新 Changes 视图，当前没有工作区 diff 数据")}
            >
              刷新
            </button>
          </div>
          <div className="auxiliary-change-summary auxiliary-change-summary-hero">
            <span className="auxiliary-change-stat added">+0</span>
            <span className="auxiliary-change-stat removed">-0</span>
            <span className="auxiliary-change-muted">无工作区更改</span>
          </div>
          <section className="auxiliary-tree-section">
            <header>工作区更改</header>
            <div className="auxiliary-empty-row">
              <span className="codicon-lite">⎇</span>
              <span>当前会话更改会显示在这里</span>
            </div>
          </section>
          <section className="auxiliary-tree-section">
            <header>其他文件</header>
            <div className="auxiliary-empty-row muted">
              <span className="codicon-lite">◇</span>
              <span>暂无可展示文件</span>
            </div>
          </section>
          <div className="auxiliary-service-note">
            Changes 的真实 diff 树由 VS Code Sessions 服务提供；Web 端当前保留视图结构和刷新反馈。
          </div>
        </div>
      );
    }

    return (
      <div className="auxiliary-view-body auxiliary-files-body">
        <WorkspaceFileTree
          apiPort={state.apiPort}
          workspaceName={state.workspaceName}
          workspaceRoot={state.workspaceRoot}
          activeFilePath={activePreviewPath}
          searchOpen={fileTreeSearchOpen}
          collapseVersion={fileTreeCollapseVersion}
          onOpenFile={openWorkspaceFilePreview}
          onStatusChange={setStatus}
        />
      </div>
    );
  };

  return (
    <div
      className={`app-shell agent-sessions-workbench shell-gradient-background ${historyVisible ? "history-open" : "history-closed"}`}
      data-history-open={String(historyVisible)}
    >
      <Toolbar
        workspaceName={state.workspaceName}
        workspaceRoot={state.workspaceRoot}
        status={state.status}
        agentId={state.currentSession?.current_agent_id ?? "default"}
        onCreateSession={handleCreateSession}
      />
      <main className="content sessions-workbench-grid">
        {state.error ? (
          <div className="empty-state error-state">
            <div className="error-title">前端初始化失败</div>
            <div className="error-message">{state.error}</div>
          </div>
        ) : state.isBootstrapping ? (
          <BootstrapState />
        ) : null}
        <div
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}${previewVisible ? "" : " preview-collapsed"}`}
        >
          {historyVisible ? (
            <button
              type="button"
              className="history-backdrop"
              aria-label="关闭会话侧栏"
              onClick={toggleHistoryPanel}
            />
          ) : null}
          <HistoryPanel
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ""}
            onSelectSession={selectSession}
            onRenameSession={handleRenameSession}
            onDeleteSession={handleDeleteSession}
            onStatusChange={setStatus}
            isOpen={historyVisible}
            onClose={toggleHistoryPanel}
            workspaceName={state.workspaceName ?? ""}
            workspaceRoot={state.workspaceRoot ?? ""}
            activeSession={activeSession}
            sessionAttachmentSummaries={state.sessionAttachmentSummaries}
            onCreateSession={handleCreateSession}
            width={historyPanelWidth}
          />
          {historyVisible ? (
            <button
              type="button"
              className="layout-sash layout-sash-history-right"
              title="拖拽调整会话侧栏宽度，双击还原"
              aria-label="调整会话侧栏宽度"
              onPointerDown={(event) => startLayoutResize("history-right", event)}
              onDoubleClick={() => setHistoryPanelWidth(DEFAULT_HISTORY_PANEL_WIDTH)}
            />
          ) : null}
          <section className="chat-panel sessions-part-card">
            <div className="session-view-surface">
              <div className="session-view-content">{renderContentView()}</div>
              <Composer />
            </div>
          </section>
          {previewVisible ? (
            <>
              <button
                type="button"
                className="layout-sash layout-sash-preview-left"
                title="拖拽调整文件预览区宽度，双击还原"
                aria-label="调整文件预览区宽度"
                onPointerDown={(event) => startLayoutResize("preview-left", event)}
                onDoubleClick={() => setPreviewWidth(defaultPreviewWidth())}
              />
              <WorkspaceFilePreviewArea
                width={previewWidth}
                tabs={previewTabs}
                activePath={activePreviewPath}
                loadingPath={previewLoadingPath}
                error={previewError}
                onSelectTab={(path) => {
                  setActivePreviewPath(path);
                  setPreviewError(null);
                }}
                onCloseTab={closeWorkspaceFilePreview}
                onClosePanel={() => setPreviewVisible(false)}
              />
            </>
          ) : null}
          {auxiliaryVisible ? (
            <>
              <button
                type="button"
                className="layout-sash layout-sash-auxiliary-left"
                title="拖拽调整右侧栏宽度，双击还原"
                aria-label="调整右侧栏宽度"
                onPointerDown={(event) => startLayoutResize("auxiliary-left", event)}
                onDoubleClick={() => setAuxiliaryWidth(DEFAULT_AUXILIARY_WIDTH)}
              />
              <aside
                className="auxiliary-panel"
                style={{ flexBasis: auxiliaryWidth, width: auxiliaryWidth }}
              >
              <header className="auxiliary-titlebar">
                <nav className="auxiliary-tabs" aria-label="会话详情">
                  <button
                    type="button"
                    className={auxiliaryTab === "changes" ? "active" : ""}
                    onClick={() => setAuxiliaryTab("changes")}
                  >
                    更改
                  </button>
                  <button
                    type="button"
                    className={auxiliaryTab === "files" ? "active" : ""}
                    onClick={() => setAuxiliaryTab("files")}
                  >
                    文件
                  </button>
                </nav>
                <div className="auxiliary-title-actions" aria-label="文件视图操作">
                  <button
                    type="button"
                    className={`auxiliary-icon-button${fileTreeSearchOpen ? " active" : ""}`}
                    title="搜索"
                    aria-label="搜索文件"
                    onClick={() => {
                      setAuxiliaryTab("files");
                      setFileTreeSearchOpen((open) => !open);
                    }}
                  >
                    <span className="auxiliary-action-icon search" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="auxiliary-icon-button"
                    title="全部折叠"
                    aria-label="全部折叠"
                    onClick={() => {
                      setAuxiliaryTab("files");
                      setFileTreeCollapseVersion((version) => version + 1);
                    }}
                  >
                    <span className="auxiliary-action-icon collapse-all" aria-hidden="true" />
                  </button>
                </div>
              </header>
              {renderAuxiliaryPanel()}
              </aside>
            </>
          ) : (
            <button
              type="button"
              className="auxiliary-restore-button"
              title="显示详情"
              aria-label="显示详情"
              onClick={() => setAuxiliaryVisible(true)}
            >
              ◰
            </button>
          )}
        </div>
      </main>
      <SessionNameDialog
        open={nameDialog !== null}
        title="重命名会话"
        label="会话名称"
        initialValue={nameDialog?.initialTitle ?? "新会话"}
        confirmText="保存名称"
        submitting={nameDialogSubmitting}
        error={nameDialogError}
        onCancel={closeNameDialog}
        onSubmit={submitNameDialog}
      />
    </div>
  );
}
