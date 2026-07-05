import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import Composer from "./components/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import HistoryPanel from "./components/HistoryPanel";
import SessionNameDialog from "./components/SessionNameDialog";
import Toolbar from "./components/Toolbar";
import { useState } from "react";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";

type SessionNameDialogState =
  | { mode: "create"; initialTitle: string }
  | { mode: "rename"; sessionId: string; initialTitle: string };

export default function AppShell() {
  const {
    state,
    selectSession,
    toggleHistoryPanel,
    createSession,
    renameSession,
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
  const openCreateSessionDialog = () => {
    setNameDialog({ mode: "create", initialTitle: "新会话" });
    setNameDialogError(null);
  };
  const handleRenameSession = (sessionId: string, currentTitle: string) => {
    setNameDialog({
      mode: "rename",
      sessionId,
      initialTitle: currentTitle || "新会话",
    });
    setNameDialogError(null);
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
    const action =
      nameDialog.mode === "create"
        ? createSession(title)
        : renameSession(nameDialog.sessionId, title);

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
        onCreateSession={openCreateSessionDialog}
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
        title={nameDialog?.mode === "rename" ? "重命名会话" : "新建会话"}
        label="会话名称"
        initialValue={nameDialog?.initialTitle ?? "新会话"}
        confirmText={nameDialog?.mode === "rename" ? "保存名称" : "创建会话"}
        submitting={nameDialogSubmitting}
        error={nameDialogError}
        onCancel={closeNameDialog}
        onSubmit={submitNameDialog}
      />
    </div>
  );
}
