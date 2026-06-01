import type { ActiveJob, AppState, Message, Session, TraceEvent } from './types';
import { setVsCodeState } from './vscode';

export type PreviewBackendPayload = {
  workspaceRoot: string;
  workspaceName: string;
  session: Session;
  messages: Message[];
  traceEvents: TraceEvent[];
  activeJob: ActiveJob | null;
  status: string;
};

const now = () => new Date().toISOString();

const previewSession: Session = {
  session_id: 'preview-session',
  title: '浏览器预览会话',
  status: 'running',
  agent_id: 'preview-agent',
  created_at: now(),
  updated_at: now(),
};

export const previewBackendPayload: PreviewBackendPayload = {
  workspaceRoot: 'F:/code/2026/20260126_agent/vscode-graph-agent-opencode',
  workspaceName: 'vscode-graph-agent-opencode',
  session: previewSession,
  messages: [
    {
      message_id: 'msg-user-1',
      session_id: previewSession.session_id,
      role: 'user',
      content: '请演示一个带按钮的前端界面，我想直接在浏览器里看看。',
      metadata: { source: 'backend', job_id: 'job-preview-1' },
      attachments: [],
      created_at: now(),
    },
    {
      message_id: 'msg-assistant-1',
      session_id: previewSession.session_id,
      role: 'assistant',
      content: JSON.stringify([
        { type: 'reasoning', text: '先构造一个最小可展示的界面，包含工具栏、历史面板、聊天区和输入框。' },
        { type: 'tool_call', name: 'preview_button', content: '需要检查按钮布局和点击态。' },
        { type: 'text', text: '当然可以，我会先渲染一个带按钮的预览界面，帮助你直接在浏览器里查看布局。' },
      ]),
      metadata: { source: 'backend', job_id: 'job-preview-1', format: 'assistant_segments' },
      attachments: [],
      created_at: now(),
    },
  ],
  traceEvents: [
    { event_type: 'agent_start', data: { phase: 'render', model: 'preview-model' }, timestamp: now() },
    { event_type: 'tool_call_start', data: { tool_name: 'preview_button', arguments: { label: '预览按钮' } }, timestamp: now() },
    { event_type: 'tool_call_end', data: { tool_name: 'preview_button', result: { ok: true, changed: true } }, timestamp: now() },
  ],
  activeJob: {
    jobId: 'job-preview-1',
    sessionId: previewSession.session_id,
    status: 'running',
    messageId: 'msg-user-1',
    content: '预览模式任务运行中',
  },
  status: '预览模式已就绪',
};

export type PreviewUiState = Partial<AppState>;

export function buildPreviewStateFromBackend(payload: PreviewBackendPayload): PreviewUiState {
  return {
    workspaceRoot: payload.workspaceRoot,
    workspaceName: payload.workspaceName,
    sessions: [payload.session],
    currentSession: payload.session,
    messages: payload.messages,
    traceEvents: payload.traceEvents,
    activeJob: payload.activeJob,
    status: payload.status,
    expandDetails: true,
    historyPanelOpen: true,
  };
}

export function createPreviewBootDomElement(previewState: PreviewUiState): HTMLDivElement {
  const boot = document.createElement('div');
  boot.id = 'graph-agent-boot';
  boot.dataset.preview = 'true';
  boot.dataset.workspaceName = previewBackendPayload.workspaceName;
  boot.dataset.sessionId = previewBackendPayload.session.session_id;
  boot.dataset.status = previewBackendPayload.status;
  boot.textContent = JSON.stringify(previewState);
  return boot;
}

export function initializePreviewState(): PreviewUiState {
  const previewState = buildPreviewStateFromBackend(previewBackendPayload);
  setVsCodeState({
    ...previewState,
    bootDomElement: '',
  });

  return previewState;
}
