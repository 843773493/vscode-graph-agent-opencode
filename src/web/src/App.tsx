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
import { useEffect, useState } from "react";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";

type SessionNameDialogState = { sessionId: string; initialTitle: string };
type AuxiliaryTab = "changes" | "files";

function defaultAuxiliaryVisible(): boolean {
  if (typeof window === "undefined") {
    return true;
  }
  return window.innerWidth > 900;
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
        <div className="files-tree-root">
          <button
            type="button"
            className="files-tree-item root"
            onClick={() => setStatus(`当前工作区: ${state.workspaceRoot || state.workspaceName || "workspace"}`)}
          >
            <span className="codicon-lite">▾</span>
            <span className="file-icon">▣</span>
            <span className="file-label">{state.workspaceName || "workspace"}</span>
          </button>
          <div className="files-tree-item muted">
            <span className="codicon-lite" />
            <span className="file-icon">◇</span>
            <span className="file-label">VS Code Explorer 文件树服务尚未暴露给 Web</span>
          </div>
        </div>
        <button
          type="button"
          className="auxiliary-inline-action"
          onClick={() => setStatus("文件视图已同步，当前 Web 端仅显示工作区根")}
        >
          同步更改
        </button>
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
        <div className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}`}>
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
          />
          <section className="chat-panel sessions-part-card">
            <div className="session-view-surface">
              <div className="session-view-content">{renderContentView()}</div>
              <Composer />
            </div>
          </section>
          {auxiliaryVisible ? (
            <aside className="auxiliary-panel">
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
                <button
                  type="button"
                  className="auxiliary-icon-button"
                  title="折叠详情"
                  onClick={() => setAuxiliaryVisible(false)}
                >
                  ◱
                </button>
              </header>
              {renderAuxiliaryPanel()}
            </aside>
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
