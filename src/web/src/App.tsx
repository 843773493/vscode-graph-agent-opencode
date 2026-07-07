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
import { useState } from "react";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";

type SessionNameDialogState = { sessionId: string; initialTitle: string };

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
  const activeSession = state.currentSession;
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

  return (
    <div
      className={`app-shell ${historyVisible ? "history-open" : "history-closed"}`}
      data-history-open={String(historyVisible)}
    >
      <Toolbar
        workspaceName={state.workspaceName}
        workspaceRoot={state.workspaceRoot}
        status={state.status}
        agentId={state.currentSession?.current_agent_id ?? "default"}
        onCreateSession={handleCreateSession}
      />
      <main className="content">
        {state.error ? (
          <div className="empty-state error-state">
            <div className="error-title">前端初始化失败</div>
            <div className="error-message">{state.error}</div>
          </div>
        ) : state.isBootstrapping ? (
          <BootstrapState />
        ) : null}
        <div className="content-layout">
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
          />
          <section className="chat-panel">
            {state.contentView === "agent" ? (
              <AgentStatePanel
                jsonl={state.agentStateJsonl}
                messageCount={state.agentStateMessageCount}
                loadedAt={state.agentStateLoadedAt}
                loading={state.agentStateLoading}
                error={state.agentStateError}
              />
            ) : state.contentView === "events" ? (
              <EventQueuePanel
                items={receivedEvents}
                limit={FRONTEND_EVENT_QUEUE_LIMIT}
                sessionId={activeSession?.session_id ?? ""}
              />
            ) : state.contentView === "requests" ? (
              <RequestLogPanel
                logs={state.llmRequestLogs}
                loading={state.llmRequestLogsLoading}
                error={state.llmRequestLogsError}
                loadedAt={state.llmRequestLogsLoadedAt}
                sessionId={activeSession?.session_id ?? ""}
              />
            ) : state.contentView === "resources" ? (
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
            ) : (
              <ChatPanel
                conversations={conversations}
                expandDetails={state.expandDetails}
              />
            )}
          </section>
        </div>
      </main>
      <Composer />
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
