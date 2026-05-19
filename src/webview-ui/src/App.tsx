import React from 'react';
import { useAppState, getTurnsForSession } from './hooks';
import HistoryPanel from './components/HistoryPanel';
import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';

function Toolbar({ workspaceName, workspaceRoot, status }: { workspaceName: string; workspaceRoot: string; status: string }) {
  const { createSession, toggleHistoryPanel } = useAppState();
  const wsLabel = workspaceRoot || workspaceName;

  return (
    <header className="toolbar">
      <div className="toolbar-group toolbar-group-left">
        <button type="button" className="toolbar-icon-button" title="新建会话" onClick={() => createSession('新会话')}>+</button>
        <button type="button" className="toolbar-icon-button" title="历史记录" onClick={toggleHistoryPanel}>历史</button>
        <button type="button" className="toolbar-icon-button" title="固定会话" disabled>置顶</button>
      </div>
      <div className="toolbar-center" title={wsLabel}>
        <span className="toolbar-agent">default</span>
      </div>
      <div className="toolbar-group toolbar-group-right">
        <span className="badge neutral" title={wsLabel}>{wsLabel}</span>
        <span className="badge neutral" aria-live="polite">{status}</span>
      </div>
    </header>
  );
}

export default function AppShell() {
  const { state, selectSession } = useAppState();
  const activeSession = state.currentSession;
  const turns = activeSession ? getTurnsForSession(activeSession.session_id, state) : [];
  const sortedSessions = [...state.sessions].sort((a, b) => new Date(b.updated_at || b.created_at || '').getTime() - new Date(a.updated_at || a.created_at || '').getTime());

  return (
    <div className="app-shell">
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} />
      <main className="content">
        <HistoryPanel sessions={sortedSessions} currentSessionId={activeSession?.session_id ?? ''} onSelectSession={selectSession} isOpen={state.historyPanelOpen} />
        <section className={`chat-panel${state.historyPanelOpen ? ' with-history' : ''}`}>
          <ChatPanel turns={turns} expandDetails={state.expandDetails} />
        </section>
      </main>
      <Composer />
    </div>
  );
}
