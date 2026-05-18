import React from 'react';
import { useAppState, getTurnsForSession } from './hooks';
import { postDebug } from './vscode';
import HistoryPanel from './components/HistoryPanel';
import ChatPanel from './components/ChatPanel';
import Composer from './components/Composer';
import type { PendingTurn } from './types';

export default function AppShell() {
  const { state, createSession, selectSession } = useAppState();
  const activeSession = state.currentSession;
  const sessionId = activeSession?.session_id;
  const turns: PendingTurn[] = sessionId ? getTurnsForSession(sessionId, state) as PendingTurn[] : [];
  const sortedSessions = [...state.sessions].sort(
    (a, b) => new Date(b.updated_at || b.created_at || '').getTime()
       - new Date(a.updated_at || a.created_at || '').getTime()
  );

  return (
    <div className="app-shell">
      <Toolbar workspaceName={state.workspaceName} workspaceRoot={state.workspaceRoot} status={state.status} />
      <div className="info-row">
        <div className="info-chip">
          <strong id="workspace">{state.workspaceName || 'workspace'}</strong>
          <span id="workspaceStatus">{activeSession?.title || 'No active session'}</span>
        </div>
        <div className="info-chip">
          <span id="status" aria-live="polite">{state.status || '准备就绪'}</span>
        </div>
        <div
          id="agentNameDisplay"
          style={{ fontSize: 12, color: 'var(--vscode-descriptionForeground)', opacity: 0.7, marginLeft: 8, userSelect: 'text', cursor: 'text', fontFamily: 'var(--vscode-editor-font-family)' }}
        >
          {activeSession?.agent_id || 'default'}
        </div>
      </div>

      <main className="content">
        <HistoryPanel
          sessions={sortedSessions}
          currentSessionId={activeSession?.session_id ?? ''}
          onSelectSession={selectSession}
          isOpen={state.historyPanelOpen}
        />

        <section className={`chat-panel${state.historyPanelOpen ? ' with-history' : ''}`}>
          <ChatPanel
            turns={turns}
            expandDetails={state.expandDetails}
            activeJob={state.activeJob}
          />
        </section>
      </main>

      <Composer />
    </div>
  );
}

function Toolbar(props: { workspaceName: string; workspaceRoot: string; status: string }) {
  const { createSession, toggleHistoryPanel } = useAppState();
  const wsLabel = props.workspaceRoot || props.workspaceName;

  return (
    <header className="toolbar">
      <div className="toolbar-group">
        <button id="newSessionButton" type="button" title="新建聊天" onClick={() => createSession('新会话')}>
          <svg viewBox="0 0 16 16"><path d="M14 2H2C1.44772 2 1 2.44772 1 3V13C1 13.5523 1.44772 14 2 14H8V12H3V4H13V8H15V3C15 2.44772 14.5523 2 14 2ZM10 10V13H13L10 16V14H7V10H10Z"/></svg>
        </button>
        <button id="historyButton" type="button" title="历史记录" onClick={toggleHistoryPanel}>
          <svg viewBox="0 0 16 16"><path d="M8 1C4.13401 1 1 4.13401 1 8C1 11.866 4.13401 15 8 15C11.866 15 15 11.866 15 8H13C13 10.7614 10.7614 13 8 13C5.23858 13 3 10.7614 3 8C3 5.23858 5.23858 3 8 3C9.90213 3 11.576 4.01486 12.5355 5.5H10V7H15V2H13V4.25736C11.8234 2.27593 10.0523 1 8 1ZM7 5V9L10.5 11.1L11 10.4L8 8.6V5H7Z"/></svg>
        </button>
        <button id="pinButton" type="button" title="固定会话">
          <svg viewBox="0 0 16 16"><path d="M10 1V2H11V6L13 8V9H9V14H7V9H3V8L5 6V2H6V1H10ZM6 3V6.5L4.5 8H11.5L10 6.5V3H6Z"/></svg>
        </button>
        <button id="viewToggleButton" type="button" title="视图切换">
          <svg viewBox="0 0 16 16"><path d="M2 2H7V7H2V2ZM3 3V6H6V3H3ZM9 2H14V7H9V2ZM10 3V6H13V3H10ZM2 9H7V14H2V9ZM3 10V13H6V10H3ZM9 9H14V14H9V9ZM10 10V13H13V10H10Z"/></svg>
        </button>
      </div>
      <div className="toolbar-group">
        <span className="badge neutral" title={wsLabel}>{wsLabel}</span>
      </div>
      <div className="toolbar-group">
        <button id="contextButton" type="button" title="上下文设置">
          <svg viewBox="0 0 16 16"><path d="M8 1C5.79086 1 4 2.79086 4 5C4 6.8625 5.275 8.425 7 8.875V15H9V8.875C10.725 8.425 12 6.8625 12 5C12 2.79086 10.2091 1 8 1ZM8 2C9.65685 2 11 3.34315 11 5C11 6.65685 9.65685 8 8 8C6.34315 8 5 6.65685 5 5C5 3.34315 6.34315 2 8 2Z"/></svg>
        </button>
        <button id="helpButton" type="button" title="帮助">
          <svg viewBox="0 0 16 16"><path d="M8 1C4.13401 1 1 4.13401 1 8C1 11.866 4.13401 15 8 15C11.866 15 15 11.866 15 8C15 4.13401 11.866 1 8 1ZM8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8C14 11.3137 11.3137 14 8 14ZM7 11H9V13H7V11ZM8 4C6.34315 4 5 5.34315 5 7H7C7 6.44772 7.44772 6 8 6C8.55228 6 9 6.44772 9 7C9 8 7 7.75 7 10H9C9 8.75 11 8.5 11 7C11 5.34315 9.65685 4 8 4Z"/></svg>
        </button>
        <button id="settingsButton" type="button" title="设置">
          <svg viewBox="0 0 16 16"><path d="M7.5 0L7 2H9L8.5 0H7.5ZM12.1924 1.3934L10.8284 2.75736L12.2426 4.17157L13.6066 2.80761L12.1924 1.3934ZM14 7V7.5H16V8.5H14V9H14V7ZM13.6066 13.1924L12.2426 11.8284L10.8284 13.2426L12.1924 14.6066L13.6066 13.1924ZM8.5 16H7.5L7 14H9L8.5 16ZM2.80761 14.6066L4.17157 13.2426L2.75736 11.8284L1.3934 13.1924L2.80761 14.6066ZM2 9V8.5H0V7.5H2V7H2V9ZM1.3934 2.80761L2.75736 4.17157L4.17157 2.75736L2.80761 1.3934L1.3934 2.80761ZM8 5C6.34315 5 5 6.34315 5 8C5 9.65685 6.34315 11 8 11C9.65685 11 11 9.65685 11 8C11 6.34315 9.65685 5 8 5ZM8 6C9.10457 6 10 6.89543 10 8C10 9.10457 9.10457 10 8 10C6.89543 10 6 9.10457 6 8C6 6.89543 6.89543 6 8 6Z"/></svg>
        </button>
      </div>
    </header>
  );
}
