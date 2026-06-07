import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';
import HistoryPanel from './components/HistoryPanel';
import Toolbar from './components/Toolbar';
import { getConversationsForSession, useAppState } from './hooks';

export default function AppShell() {
  const { state, selectSession, toggleHistoryPanel } = useAppState();
  const activeSession = state.currentSession;
  const conversations = activeSession ? getConversationsForSession(activeSession.session_id, state) : [];
  const sortedSessions = [...state.sessions].sort((a, b) => new Date(b.updated_at || b.created_at || '').getTime() - new Date(a.updated_at || a.created_at || '').getTime());
  const historyVisible = state.historyPanelOpen;

  return (
    <div className={`app-shell ${historyVisible ? 'history-open' : 'history-closed'}`} data-history-open={String(historyVisible)}>
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} agentId={state.currentSession?.agent_id ?? 'default'} />
      <main className="content">
        {state.error ? (
          <div className="empty-state error-state">
            <div className="error-title">前端初始化失败</div>
            <div className="error-message">{state.error}</div>
          </div>
        ) : state.isBootstrapping ? (
          <div className="empty-state">正在加载工作区与会话…</div>
        ) : null}
        <div className="content-layout">
          <HistoryPanel
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ''}
            onSelectSession={selectSession}
            isOpen={historyVisible}
            onClose={toggleHistoryPanel}
            workspaceName={state.workspaceName}
            workspaceRoot={state.workspaceRoot}
            activeSession={activeSession}
          />
          <section className="chat-panel">
            <ChatPanel conversations={conversations} expandDetails={state.expandDetails} />
          </section>
        </div>
      </main>
      <Composer />
    </div>
  );
}
