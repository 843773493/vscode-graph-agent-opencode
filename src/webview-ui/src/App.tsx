import React from 'react';
import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';
import HistoryPanel from './components/HistoryPanel';
import Toolbar from './components/Toolbar';
import { getTurnsForSession, useAppState } from './hooks';

type LayoutMode = 'docked' | 'drawer' | 'hidden';

function resolveLayoutMode(width: number): LayoutMode {
  if (width >= 900) return 'drawer';
  return 'hidden';
}

export default function AppShell() {
  const { state, selectSession, toggleHistoryPanel } = useAppState();
  const activeSession = state.currentSession;
  const turns = activeSession ? getTurnsForSession(activeSession.session_id, state) : [];
  const sortedSessions = [...state.sessions].sort((a, b) => new Date(b.updated_at || b.created_at || '').getTime() - new Date(a.updated_at || a.created_at || '').getTime());
  const shellRef = React.useRef<HTMLDivElement | null>(null);
  const [layoutMode, setLayoutMode] = React.useState<LayoutMode>('docked');

  React.useEffect(() => {
    const el = shellRef.current;
    if (!el) {
      return;
    }

    const updateLayoutMode = () => {
      setLayoutMode(resolveLayoutMode(el.clientWidth));
    };

    updateLayoutMode();

    const observer = new ResizeObserver(() => {
      updateLayoutMode();
    });

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const historyVisible = layoutMode === 'hidden' ? false : state.historyPanelOpen;
  const historyDrawerOpen = layoutMode === 'drawer' && historyVisible;

  return (
    <div
      ref={shellRef}
      className={`app-shell layout-${layoutMode} ${historyVisible ? 'history-open' : 'history-closed'}`}
      data-layout-mode={layoutMode}
      data-history-open={String(historyVisible)}
    >
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} layoutMode={layoutMode} agentId={state.currentSession?.agent_id ?? 'default'} />
      <main className="content">
        <div className="content-layout">
          <HistoryPanel
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ''}
            onSelectSession={selectSession}
            isOpen={historyVisible}
            layoutMode={layoutMode}
            onClose={toggleHistoryPanel}
            workspaceName={state.workspaceName}
            workspaceRoot={state.workspaceRoot}
            activeSession={activeSession}
          />
          <section className="chat-panel">
            <ChatPanel turns={turns} expandDetails={state.expandDetails} />
          </section>
        </div>
        {historyDrawerOpen && (
          <button
            type="button"
            className="content-scrim"
            aria-label="关闭历史面板"
            onClick={toggleHistoryPanel}
          />
        )}
      </main>
      <Composer />
    </div>
  );
}
