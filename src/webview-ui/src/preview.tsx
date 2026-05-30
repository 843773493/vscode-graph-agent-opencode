import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import type { AppState, Session, Message, TraceEvent, ActiveJob } from './types';
import './index.css';

declare global {
  interface ImportMeta {
    hot?: { accept: () => void };
  }
}

const previewSession: Session = {
  session_id: 'preview-session',
  title: '浏览器预览会话',
  status: 'running',
  agent_id: 'preview-agent',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

const previewMessages: Message[] = [
  {
    message_id: 'msg-user-1',
    session_id: previewSession.session_id,
    role: 'user',
    content: '请演示一个带按钮的前端界面，我想直接在浏览器里看看。',
    metadata: {},
    attachments: [],
    created_at: new Date().toISOString(),
  },
  {
    message_id: 'msg-assistant-1',
    session_id: previewSession.session_id,
    role: 'assistant',
    content: '当然可以。这个预览页会保留工具栏、历史栏、输入框和消息卡片，方便你无需启动 VS Code 就快速查看布局。',
    metadata: {},
    attachments: [],
    created_at: new Date().toISOString(),
  },
];

const previewTrace: TraceEvent[] = [
  { event_type: 'agent_start', data: { phase: 'render' }, timestamp: new Date().toISOString() },
  { event_type: 'tool_call_start', data: { tool_name: 'preview_button' }, timestamp: new Date().toISOString() },
  { event_type: 'tool_call_end', data: { tool_name: 'preview_button' }, timestamp: new Date().toISOString() },
];

const previewActiveJob: ActiveJob = {
  jobId: 'job-preview-1',
  sessionId: previewSession.session_id,
  status: 'running',
  messageId: 'msg-user-1',
  content: '预览模式任务运行中',
};

const previewState: Partial<AppState> = {
  workspaceRoot: 'F:/code/2026/20260126_agent/vscode-graph-agent-opencode',
  workspaceName: 'vscode-graph-agent-opencode',
  sessions: [previewSession],
  currentSession: previewSession,
  messages: previewMessages,
  traceEvents: previewTrace,
  activeJob: previewActiveJob,
  status: '预览模式已就绪',
  expandDetails: true,
  historyPanelOpen: true,
};

function injectPreviewState() {
  const boot = document.createElement('script');
  boot.id = 'graph-agent-boot';
  boot.type = 'application/json';
  boot.textContent = JSON.stringify(previewState).replaceAll('<', '\u003c');
  document.head.appendChild(boot);
}

injectPreviewState();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </React.StrictMode>,
);

if (import.meta.hot) {
  import.meta.hot.accept();
}