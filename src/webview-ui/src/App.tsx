import React from 'react';
import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';
import HistoryPanel from './components/HistoryPanel';
import { getTurnsForSession, useAppState } from './hooks';

type LayoutMode = 'docked' | 'drawer' | 'hidden';

function resolveLayoutMode(width: number): LayoutMode {
  if (width >= 960) return 'docked';
  if (width >= 600) return 'drawer';
  return 'hidden';
}

function Toolbar({ workspaceName, workspaceRoot, status, layoutMode }: { workspaceName: string; workspaceRoot: string; status: string; layoutMode: LayoutMode }) {
  const { createSession, toggleHistoryPanel } = useAppState();
  const wsLabel = workspaceRoot || workspaceName;
  const canToggleHistory = layoutMode !== 'hidden';

  return (
    <header className="toolbar">
      <div className="toolbar-group toolbar-group-left">
        <button type="button" className="toolbar-icon-button toolbar-icon-primary" title="新建会话" onClick={() => createSession('新会话')}>＋</button>
        <button type="button" className="toolbar-icon-button" title={canToggleHistory ? '历史记录' : '当前宽度下隐藏历史栏'} onClick={toggleHistoryPanel} disabled={!canToggleHistory}>
          {layoutMode === 'drawer' ? '抽屉' : '历史'}
        </button>
      </div>
      <div className="toolbar-center" title={wsLabel}>
        <div className="toolbar-center-stack">
          <span className="toolbar-agent">default</span>
          <span className="toolbar-subtitle">{workspaceName || 'workspace'}</span>
        </div>
      </div>
      <div className="toolbar-group toolbar-group-right">
        <span className="badge neutral toolbar-path" title={wsLabel}>{wsLabel}</span>
        <span className="badge neutral" aria-live="polite">{status}</span>
      </div>
    </header>
  );
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
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} layoutMode={layoutMode} />
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
