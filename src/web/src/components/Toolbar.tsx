import React from 'react';
import { useAppState } from '../hooks';

function shortLabel(value: string | null | undefined): string {
  const trimmed = String(value ?? '').trim();
  if (!trimmed) {
    return 'workspace';
  }

  const parts = trimmed.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || trimmed;
}

function Icon({ children }: { children: React.ReactNode }) {
  return <span className="toolbar-button-icon" aria-hidden="true">{children}</span>;
}

export default function Toolbar({ workspaceName, workspaceRoot, status, agentId }: { workspaceName: string | null | undefined; workspaceRoot: string | null | undefined; status: string; agentId: string | null | undefined }) {
  const { createSession, toggleHistoryPanel } = useAppState();
  const wsLabel = workspaceRoot || workspaceName || undefined;
  const wsShort = shortLabel(wsLabel);
  const agentLabel = agentId || 'default';

  return (
    <header className="toolbar">
      <div className="toolbar-group toolbar-group-left">
        <button type="button" className="toolbar-icon-button toolbar-icon-primary" title="新建会话" onClick={() => void createSession('新会话')}>
          <Icon>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>
          </Icon>
        </button>
        <button type="button" className="toolbar-icon-button" title="历史记录" onClick={toggleHistoryPanel}>
          <Icon>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 2a6 6 0 1 0 6 6" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/><path d="M8 4v5l3 2" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </Icon>
        </button>
      </div>
      <div className="toolbar-center" title={agentLabel}>
        <div className="toolbar-center-stack">
          <span className="toolbar-agent">
            <Icon>
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 2.5a2 2 0 0 1 2 2V5h1.5A2.5 2.5 0 0 1 14 7.5v4A2.5 2.5 0 0 1 11.5 14h-7A2.5 2.5 0 0 1 2 11.5v-4A2.5 2.5 0 0 1 4.5 5H6v-.5a2 2 0 0 1 2-2Zm-1 4.5h2v1h-2v-1Zm0 2.5h2v1h-2v-1Z" fill="currentColor"/></svg>
            </Icon>
            <span>{agentLabel}</span>
          </span>
          <span className="toolbar-subtitle">{workspaceName || 'workspace'}</span>
        </div>
      </div>
      <div className="toolbar-group toolbar-group-right">
        <span className="badge neutral toolbar-path" title={wsLabel} aria-label={wsLabel}>
          <Icon>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M2.5 5A1.5 1.5 0 0 1 4 3.5h2.8l1.4 1.5H12A1.5 1.5 0 0 1 13.5 6.5v5A1.5 1.5 0 0 1 12 13H4A1.5 1.5 0 0 1 2.5 11.5z" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/></svg>
          </Icon>
          <span>{wsShort}</span>
        </span>
        <span className="badge neutral" aria-live="polite" title={status} aria-label={status}>
          <Icon>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="5" fill="none" stroke="currentColor" strokeWidth="1.2"/><path d="M8 5v3l2 1" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </Icon>
          <span>{status}</span>
        </span>
      </div>
    </header>
  );
}
