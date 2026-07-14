import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';
import HistoryPanel from './components/HistoryPanel';
import Toolbar from './components/Toolbar';
import { getConversationsForSession, useAppState } from './hooks';

export default function AppShell() {
  const { state, selectSession, setSessionParent, toggleHistoryPanel } = useAppState();
  const activeSession = state.currentSession;
  const conversations = activeSession ? getConversationsForSession(activeSession.session_id, state) : [];
  const sortedSessions = [...state.sessions].sort((a, b) => new Date(b.updated_at || b.created_at || '').getTime() - new Date(a.updated_at || a.created_at || '').getTime());
  const historyVisible = state.historyPanelOpen;

  return (
    <div
      className={`app-shell ${historyVisible ? 'history-open' : 'history-closed'}`}
      data-history-open={String(historyVisible)}
    >
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} agentId={state.currentSession?.current_agent_id ?? state.currentSession?.agent_id ?? 'default'} />
      <main className="content">
        <div className="content-layout">
          <HistoryPanel
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ''}
            onSelectSession={selectSession}
            onSetSessionParent={setSessionParent}
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
