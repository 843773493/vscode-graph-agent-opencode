import React from 'react';
import { useAppState } from '../hooks';

export default function Toolbar() {
  const { state, createSession, toggleHistoryPanel } = useAppState();
  const wsLabel = state.workspaceRoot || state.workspaceName;

  return (
    <header className="toolbar">
      <div className="toolbar-group toolbar-group-left">
        <button type="button" className="toolbar-icon-button" title="新建会话" onClick={() => createSession('新会话')}>+</button>
        <button type="button" className="toolbar-icon-button" title="历史记录" onClick={toggleHistoryPanel}>历史</button>
        <button type="button" className="toolbar-icon-button" title="固定会话" disabled>置顶</button>
      </div>
      <div className="toolbar-center" title={state.currentSession?.agent_id || 'default'}>
        <span className="toolbar-agent">{state.currentSession?.agent_id || 'default'}</span>
      </div>
      <div className="toolbar-group toolbar-group-right">
        <span className="badge neutral" title={wsLabel}>{wsLabel}</span>
        <span className="badge neutral" aria-live="polite">{state.status}</span>
      </div>
    </header>
  );
}
