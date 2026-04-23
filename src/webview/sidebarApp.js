const bootElement = document.getElementById('graph-agent-boot');
const boot = bootElement ? JSON.parse(bootElement.textContent || '{}') : {};
const vscode = acquireVsCodeApi();

const workspaceEl = document.getElementById('workspace');
const workspaceStatusEl = document.getElementById('workspaceStatus');
const sessionListEl = document.getElementById('sessionList');
const turnListEl = document.getElementById('turnList');
const inputEl = document.getElementById('input');
const statusEl = document.getElementById('status');
const sendButton = document.getElementById('sendButton');
const newSessionButton = document.getElementById('newSessionButton');
const refreshButton = document.getElementById('refreshButton');
const expandDetailsToggle = document.getElementById('expandDetailsToggle');
const attachButton = document.getElementById('attachButton');
const mentionButton = document.getElementById('mentionButton');
const quickPromptButton = document.getElementById('quickPromptButton');
const clearInputButton = document.getElementById('clearInputButton');
const voiceInputButton = document.getElementById('voiceInputButton');
const stopButton = document.getElementById('stopButton');
const pinButton = document.getElementById('pinButton');
const historyButton = document.getElementById('historyButton');
const viewToggleButton = document.getElementById('viewToggleButton');
const modelSelectButton = document.getElementById('modelSelectButton');
const contextButton = document.getElementById('contextButton');
const helpButton = document.getElementById('helpButton');
const settingsButton = document.getElementById('settingsButton');

const persistedState = vscode.getState?.() ?? {};

const uiState = {
  workspaceRoot: boot.workspaceRoot ?? persistedState.workspaceRoot ?? '',
  workspaceName: boot.workspaceName ?? persistedState.workspaceName ?? 'workspace',
  sessions: Array.isArray(boot.sessions) ? boot.sessions : persistedState.sessions ?? [],
  currentSession: boot.session ?? persistedState.currentSession ?? null,
  messages: Array.isArray(boot.messages) ? boot.messages : persistedState.messages ?? [],
  traceEvents: Array.isArray(boot.traceEvents) ? boot.traceEvents : persistedState.traceEvents ?? [],
  activeJob: boot.activeJob ?? persistedState.activeJob ?? null,
  pendingTurns: new Map(),
  status: persistedState.status ?? '准备就绪',
  expandDetails: persistedState.expandDetails ?? true,
};

if (typeof boot.expandDetails === 'boolean') {
  uiState.expandDetails = boot.expandDetails;
}

if (Array.isArray(persistedState.pendingTurns)) {
  for (const turn of persistedState.pendingTurns) {
    if (turn?.sessionId) {
      uiState.pendingTurns.set(turn.sessionId, turn);
    }
  }
}

function persistState() {
  vscode.setState({
    workspaceRoot: uiState.workspaceRoot,
    workspaceName: uiState.workspaceName,
    sessions: uiState.sessions,
    currentSession: uiState.currentSession,
    messages: uiState.messages,
    traceEvents: uiState.traceEvents,
    activeJob: uiState.activeJob,
    status: uiState.status,
    expandDetails: uiState.expandDetails,
    pendingTurns: Array.from(uiState.pendingTurns.values()),
  });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function applyInlineMarkdown(text) {
  let value = escapeHtml(text);
  value = value.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, href) => `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${label}</a>`);
  value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
  value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return value;
}

function renderMarkdown(source) {
  const text = String(source ?? '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const output = [];
  let paragraph = [];
  let listItems = [];
  let listType = null;
  let codeLines = [];
  let codeLang = '';
  let inCode = false;

  const flushCode = () => {
    if (!codeLines.length) {
      return;
    }

    output.push(`
<div class="code-block-container">
  <div class="code-block-actions">
    <button class="code-action-btn" title="复制代码" data-code-action="copy">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
    </button>
    <button class="code-action-btn" title="插入光标处" data-code-action="insert-cursor">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polyline points="16 18 22 12 16 6"></polyline>
        <line x1="2" y1="12" x2="22" y2="12"></line>
      </svg>
    </button>
    <button class="code-action-btn" title="替换选中内容" data-code-action="replace-selection">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect>
        <line x1="9" y1="9" x2="15" y2="15"></line>
        <line x1="15" y1="9" x2="9" y2="15"></line>
      </svg>
    </button>
    <button class="code-action-btn" title="在终端执行" data-code-action="run-terminal">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polyline points="4 17 10 11 4 5"></polyline>
        <line x1="12" y1="19" x2="20" y2="19"></line>
      </svg>
    </button>
    <button class="code-action-btn" title="新建文件" data-code-action="new-file">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
        <polyline points="14 2 14 8 20 8"></polyline>
        <line x1="12" y1="18" x2="12" y2="12"></line>
        <line x1="9" y1="15" x2="15" y2="15"></line>
      </svg>
    </button>
    <button class="code-action-btn" title="查看差异" data-code-action="view-diff">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="12" y1="5" x2="12" y2="19"></line>
        <line x1="5" y1="12" x2="19" y2="12"></line>
        <circle cx="12" cy="12" r="3"></circle>
      </svg>
    </button>
  </div>
  <pre><code data-lang="${escapeHtml(codeLang)}">${escapeHtml(codeLines.join('\n'))}</code></pre>
</div>`);
    codeLines = [];
    codeLang = '';
  };

  const flushList = () => {
    if (!listItems.length) {
      return;
    }

    const tag = listType === 'ol' ? 'ol' : 'ul';
    output.push(`<${tag}>${listItems.map((item) => `<li>${applyInlineMarkdown(item)}</li>`).join('')}</${tag}>`);
    listItems = [];
    listType = null;
  };

  const flushParagraph = () => {
    if (!paragraph.length) {
      return;
    }

    output.push(`<p>${applyInlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  };

  const flushAll = () => {
    flushParagraph();
    flushList();
    flushCode();
  };

  for (const line of lines) {
    const trimmed = line.trim();

    if (trimmed.startsWith('```')) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushAll();
        inCode = true;
        codeLang = trimmed.slice(3).trim();
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (!trimmed) {
      flushAll();
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      flushAll();
      const level = Math.min(headingMatch[1].length, 6);
      output.push(`<h${level}>${applyInlineMarkdown(headingMatch[2])}</h${level}>`);
      continue;
    }

    const unorderedMatch = trimmed.match(/^[-*+]\s+(.*)$/);
    const orderedMatch = trimmed.match(/^\d+\.\s+(.*)$/);
    if (unorderedMatch || orderedMatch) {
      flushParagraph();
      const nextListType = unorderedMatch ? 'ul' : 'ol';
      if (listType && listType !== nextListType) {
        flushList();
      }
      listType = nextListType;
      listItems.push((unorderedMatch ?? orderedMatch)[1]);
      continue;
    }

    if (trimmed.startsWith('>')) {
      flushAll();
      output.push(`<blockquote>${applyInlineMarkdown(trimmed.slice(1).trim())}</blockquote>`);
      continue;
    }

    if (listItems.length) {
      flushList();
    }

    paragraph.push(trimmed);
  }

  flushAll();
  return output.join('');
}

function formatTime(value) {
  if (!value) {
    return '';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }

  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatSnippet(text, limit = 80) {
  const value = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (value.length <= limit) {
    return value;
  }

  return `${value.slice(0, Math.max(0, limit - 1))}…`;
}

function setStatus(text, isError = false) {
  uiState.status = text;
  statusEl.textContent = text;
  statusEl.classList.toggle('error', Boolean(isError));
  persistState();
}

function postDebug(detail) {
  try {
    vscode.postMessage({ type: 'debug', detail });
  } catch {
    // ignore
  }
}

// 统一的TODO按钮点击反馈
function showTodoFeedback(buttonName) {
  try {
    vscode.postMessage({ 
      type: 'showInformationMessage', 
      message: `[Graph Agent] ❌ 此功能正在开发中，敬请期待: ${buttonName}` 
    });
  } catch {
    // ignore
  }
  postDebug(`TODO按钮点击: ${buttonName}`);
}

function reportWebviewError(error) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, true);

  try {
    vscode.postMessage({ type: 'error', message });
  } catch {
    // ignore
  }
}

function normalizeMessage(message) {
  return {
    message_id: message?.message_id ?? message?.id ?? '',
    session_id: message?.session_id ?? '',
    role: message?.role ?? 'assistant',
    content: message?.content ?? '',
    metadata: message?.metadata ?? {},
    attachments: message?.attachments ?? [],
    created_at: message?.created_at ?? message?.createdAt ?? null,
  };
}

function normalizeTraceEvent(event) {
  return {
    event_type: event?.event_type ?? event?.type ?? 'event',
    data: event?.data ?? event?.payload ?? {},
    timestamp: event?.timestamp ?? null,
  };
}

function splitMessagesIntoTurns(messages) {
  const turns = [];
  let currentTurn = null;

  for (const rawMessage of messages) {
    const message = normalizeMessage(rawMessage);

    if (message.role === 'user') {
      currentTurn = {
        turnId: message.message_id || `turn_${turns.length}_${message.created_at ?? Date.now()}`,
        sessionId: message.session_id,
        userMessage: message,
        assistantMessages: [],
        events: [],
        status: 'done',
        jobId: message.metadata?.job_id ?? null,
      };
      turns.push(currentTurn);
      continue;
    }

    if (!currentTurn) {
      currentTurn = {
        turnId: message.message_id || `turn_${turns.length}_${Date.now()}`,
        sessionId: message.session_id,
        userMessage: null,
        assistantMessages: [],
        events: [],
        status: 'done',
        jobId: message.metadata?.job_id ?? null,
      };
      turns.push(currentTurn);
    }

    currentTurn.assistantMessages.push(message);
  }

  return turns;
}

function getActiveSession() {
  const currentSessionId = uiState.currentSession?.session_id;
  return uiState.sessions.find((session) => session.session_id === currentSessionId) ?? uiState.currentSession ?? null;
}

function getTurnsForSession(sessionId) {
  const backendTurns = splitMessagesIntoTurns(uiState.messages.filter((message) => message.session_id === sessionId));
  const pendingTurn = uiState.pendingTurns.get(sessionId);

  if (!pendingTurn) {
    return backendTurns;
  }

  const lastBackendTurn = backendTurns[backendTurns.length - 1];
  const pendingIsConfirmed =
    pendingTurn.status === 'done' &&
    Boolean(lastBackendTurn?.userMessage) &&
    lastBackendTurn.userMessage.content === pendingTurn.userMessage?.content &&
    lastBackendTurn.assistantMessages.length > 0;

  if (pendingIsConfirmed) {
    uiState.pendingTurns.delete(sessionId);
    persistState();
    return backendTurns;
  }

  return [...backendTurns, pendingTurn];
}

function sessionSortKey(session) {
  return new Date(session?.updated_at ?? session?.created_at ?? 0).getTime();
}

function sessionStatusBadge(session, isActive) {
  if (isActive) {
    return '<span class="badge active">Active</span>';
  }

  const status = String(session?.status ?? '').toLowerCase();
  if (status.includes('fail') || status.includes('error')) {
    return '<span class="badge danger">Failed</span>';
  }

  if (status.includes('progress') || status.includes('run')) {
    return '<span class="badge warning">Running</span>';
  }

  return '<span class="badge neutral">Ready</span>';
}

function renderSessionList() {
  if (!sessionListEl) return;
  
  const activeSessionId = uiState.currentSession?.session_id;
  const sortedSessions = [...uiState.sessions].sort((a, b) => sessionSortKey(b) - sessionSortKey(a));
  
  if (sortedSessions.length === 0) {
    sessionListEl.innerHTML = '<div class="empty-state small">暂无历史会话</div>';
    return;
  }
  
  sessionListEl.innerHTML = sortedSessions.map(session => {
    const isActive = session.session_id === activeSessionId;
    const title = session.title || '未命名会话';
    const time = formatTime(session.updated_at || session.created_at);
    
    return `
      <div class="session-item ${isActive ? 'active' : ''}" data-select-session="${escapeHtml(session.session_id)}">
        <div class="session-title">${escapeHtml(title)}</div>
        <div class="session-meta">
          ${sessionStatusBadge(session, isActive)}
          <span class="session-time">${escapeHtml(time)}</span>
          <span class="session-id" style="font-size: 11px; color: var(--vscode-descriptionForeground); opacity: 0.7; margin-left: 8px; font-family: var(--vscode-editor-font-family); user-select: text; cursor: text;">${escapeHtml(session.session_id)}</span>
        </div>
      </div>
    `;
  }).join('');
}

function renderTraceGroupTitle(eventType, payload) {
  const type = String(eventType ?? '').toLowerCase();

  if (type === 'agent_start') {
    return `开始处理：${payload?.message ? formatSnippet(payload.message, 120) : '启动 agent'}`;
  }

  if (type === 'agent_step') {
    return payload?.phase ? `阶段：${payload.phase}` : '执行中';
  }

  if (type === 'tool_call_start') {
    return `调用工具：${payload?.tool_name || 'unknown_tool'}`;
  }

  if (type === 'tool_call_end') {
    return `工具完成：${payload?.tool_name || 'unknown_tool'}`;
  }

  if (type === 'file_write') {
    return `文件写入：${payload?.path || payload?.file_path || 'unknown path'}`;
  }

  if (type === 'llm_request' || type === 'model_call') {
    return `模型调用：${payload?.model || 'unknown model'}`;
  }

  if (type === 'agent_end') {
    return `结束处理：${payload?.final_message_count ?? 0} 条消息`;
  }

  if (type === 'error') {
    return `错误：${payload?.error || 'unknown error'}`;
  }

  return `事件：${eventType}`;
}

function isErrorTraceEvent(event) {
  const type = String(event?.event_type ?? '').toLowerCase();
  return type === 'error' || type === 'job_failed' || type === 'job_cancelled';
}

function renderOutputEventCard(event) {
  const eventType = event.event_type ?? 'event';
  const payload = event.data ?? {};
  return `
    <article class="output-event-card">
      <div class="editor-head">
        <span>${escapeHtml(renderTraceGroupTitle(eventType, payload))}</span>
        <span class="badge neutral">${escapeHtml(formatTime(event.timestamp) || 'now')}</span>
      </div>
      <div class="editor-body">
        <pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>
      </div>
    </article>
  `;
}

function renderOutputStream(label, events) {
  if (!events.length) {
    return '';
  }

  return `
    <section class="output-stream">
      <h3 class="output-stream-title">${escapeHtml(label)}</h3>
      <div class="output-stream-body">
        ${events.map((event) => renderOutputEventCard(event)).join('')}
      </div>
    </section>
  `;
}

function renderTraceCard(event) {
  const eventType = event.event_type ?? 'event';
  const payload = event.data ?? {};
  const detailsMarkdown = [];

  if (eventType === 'tool_call_start' || eventType === 'tool_call_end') {
    detailsMarkdown.push('```json');
    detailsMarkdown.push(JSON.stringify(payload ?? {}, null, 2));
    detailsMarkdown.push('```');
  } else if (eventType === 'file_write') {
    detailsMarkdown.push('```text');
    detailsMarkdown.push(String(payload?.summary ?? payload?.result ?? ''));
    detailsMarkdown.push('```');
  } else if (eventType === 'error') {
    detailsMarkdown.push('```text');
    detailsMarkdown.push(String(payload?.error ?? ''));
    detailsMarkdown.push('```');
  } else if (payload && Object.keys(payload).length > 0) {
    detailsMarkdown.push('```json');
    detailsMarkdown.push(JSON.stringify(payload, null, 2));
    detailsMarkdown.push('```');
  }

  return `
    <article class="trace-item">
      <div class="trace-title">
        <span>${escapeHtml(renderTraceGroupTitle(eventType, payload))}</span>
        <span class="badge neutral">${escapeHtml(formatTime(event.timestamp) || 'now')}</span>
      </div>
      <div class="trace-body">
        ${detailsMarkdown.length ? `<pre>${escapeHtml(detailsMarkdown.join('\n'))}</pre>` : `<pre>${escapeHtml(renderTraceGroupTitle(eventType, payload))}</pre>`}
      </div>
    </article>
  `;
}

function renderTracePanel(events) {
  if (!events.length) {
    return '';
  }

  const stdoutEvents = events.filter((event) => !isErrorTraceEvent(event));
  const stderrEvents = events.filter((event) => isErrorTraceEvent(event));

  return `
    <details class="request-container output-container" ${uiState.expandDetails ? 'open' : ''}>
      <summary class="title">
        <div class="request-main">
          <span class="request-chevron">${uiState.expandDetails ? '▼' : '▶'}</span>
          <span class="request-title">Output</span>
        </div>
        <div class="request-stats">
          <span class="badge neutral">${escapeHtml(String(events.length))} events</span>
        </div>
      </summary>
      <div class="request-details">
        ${renderOutputStream('stdout', stdoutEvents)}
        ${renderOutputStream('stderr', stderrEvents)}
      </div>
    </details>
  `;
}

function renderTurnSummary(turn) {
  const userText = turn.userMessage?.content ? formatSnippet(turn.userMessage.content, 64) : '';
  const assistantText = (turn.assistantMessages ?? []).map((message) => formatSnippet(message.content, 64)).filter(Boolean).at(-1) ?? '';
  return userText || assistantText || '未命名 turn';
}

function renderRequestSection(message) {
  if (!message) {
    return '';
  }

  return `
    <section class="chat-message user">
      <div class="chat-message-head">
        <span>You</span>
        <span>${escapeHtml(formatTime(message.created_at) || '')}</span>
      </div>
      <div class="chat-message-body reply">${renderMarkdown(message.content || '')}</div>
    </section>
  `;
}

function renderResponseSection(assistantMessages, isPending) {
  if (!assistantMessages.length && !isPending) {
    return '';
  }

  return `
    <section>
      <div class="turn-section-body">
        ${assistantMessages.length ? assistantMessages.map((assistantMessage, assistantIndex) => `
          <div class="chat-message assistant">
            <div class="chat-message-head">
              <span>${assistantIndex === 0 ? 'Assistant' : `Assistant #${assistantIndex + 1}`}</span>
              <span>${escapeHtml(formatTime(assistantMessage.created_at) || '')}</span>
              
              <!-- 消息操作按钮区 -->
              <div class="message-actions">
                <!-- 默认始终显示 4个按钮 -->
                <button class="action-btn" title="复制完整回复" data-action="copy">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                  </svg>
                </button>
                <button class="action-btn" title="重新生成" data-action="regenerate">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="1 4 1 10 7 10"></polyline>
                    <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"></path>
                  </svg>
                </button>
                <button class="action-btn" title="点赞" data-action="thumbs-up">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path>
                  </svg>
                </button>
                <button class="action-btn" title="点踩" data-action="thumbs-down">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M10 15v1a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"></path>
                  </svg>
                </button>
                
                <!-- 悬停才显示 8个按钮 -->
                <button class="action-btn hover-only" title="编辑并重试" data-action="edit-retry">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="插入到编辑器" data-action="insert-editor">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="16 18 22 12 16 6"></polyline>
                    <line x1="2" y1="12" x2="22" y2="12"></line>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="在终端运行" data-action="run-terminal">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="4 17 10 11 4 5"></polyline>
                    <line x1="12" y1="19" x2="20" y2="19"></line>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="创建新文件" data-action="create-file">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                    <line x1="12" y1="18" x2="12" y2="12"></line>
                    <line x1="9" y1="15" x2="15" y2="15"></line>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="解释代码" data-action="explain-code">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"></circle>
                    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path>
                    <line x1="12" y1="17" x2="12.01" y2="17"></line>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="优化代码" data-action="optimize-code">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="添加注释" data-action="add-comments">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                  </svg>
                </button>
                <button class="action-btn hover-only" title="分享" data-action="share">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="18" cy="5" r="3"></circle>
                    <circle cx="6" cy="12" r="3"></circle>
                    <circle cx="18" cy="19" r="3"></circle>
                    <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line>
                    <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line>
                  </svg>
                </button>
              </div>
            </div>
            <div class="chat-message-body reply">${renderMarkdown(assistantMessage.content || '')}</div>
          </div>
        `).join('') : `
          <div class="chat-message assistant">
            <div class="chat-message-head">
              <span>Assistant</span>
              <span>running</span>
            </div>
            <div class="chat-message-body reply">正在思考并调用工具...</div>
          </div>
        `}
      </div>
    </section>
  `;
}

function renderTurn(turn, index, totalTurns) {
  const isLast = index === totalTurns - 1;
  const isPending = Boolean(turn.pending) || turn.status === 'running';
  const userMessage = turn.userMessage;
  const assistantMessages = turn.assistantMessages ?? [];
  const showTrace = (turn.events?.length ?? 0) > 0 && uiState.expandDetails;

  return `
    <div class="request-container" data-turn-id="${escapeHtml(turn.turnId)}">
      ${renderRequestSection(userMessage)}
      ${renderResponseSection(assistantMessages, isPending)}
      ${showTrace ? renderTracePanel(turn.events.map((event) => normalizeTraceEvent(event))) : ''}
    </div>
  `;
}

function renderTranscript() {
  const activeSession = getActiveSession();
  const sessionId = activeSession?.session_id;
  const turns = sessionId ? getTurnsForSession(sessionId) : [];

  workspaceEl.textContent = uiState.workspaceName || 'workspace';
  workspaceEl.title = uiState.workspaceRoot || uiState.workspaceName || 'workspace';
  workspaceStatusEl.textContent = activeSession?.title ? activeSession.title : 'No active session';
  workspaceStatusEl.title = activeSession?.title || 'No active session';

  if (!turns.length) {
    turnListEl.innerHTML = `
      <div class="empty-state">
        <div style="font-weight:600; margin-bottom: 4px;">会话</div>
        <div>输入内容开始聊天。</div>
      </div>
    `;
    return;
  }

  turnListEl.innerHTML = turns.map((turn, index) => renderTurn(turn, index, turns.length)).join('');
}

function render() {
  renderSessionList();
  renderTranscript();
  setStatus(uiState.status || '准备就绪');
}

function ensurePendingTurn(sessionId) {
  let pendingTurn = uiState.pendingTurns.get(sessionId);
  if (!pendingTurn) {
    pendingTurn = {
      turnId: `local_${Date.now()}_${Math.random().toString(16).slice(2)}`,
      sessionId,
      userMessage: null,
      assistantMessages: [],
      events: [],
      status: 'running',
      jobId: null,
      pending: true,
    };
    uiState.pendingTurns.set(sessionId, pendingTurn);
  }

  return pendingTurn;
}

function submitCurrentMessage() {
  const content = inputEl.value.trim();
  postDebug('submitCurrentMessage 调用');

  if (!content) {
    setStatus('请输入内容后再发送', true);
    return false;
  }

  const activeSession = getActiveSession();
  if (!activeSession) {
    setStatus('请先创建 session', true);
    return false;
  }

  const pendingTurn = ensurePendingTurn(activeSession.session_id);
  pendingTurn.userMessage = {
    message_id: `local_user_${Date.now()}`,
    session_id: activeSession.session_id,
    role: 'user',
    content,
    metadata: { source: 'local_optimistic' },
    attachments: [],
    created_at: new Date().toISOString(),
  };
  pendingTurn.assistantMessages = pendingTurn.assistantMessages ?? [];
  pendingTurn.events = pendingTurn.events ?? [];
  pendingTurn.status = 'running';
  pendingTurn.jobId = null;
  pendingTurn.pending = true;

  inputEl.value = '';
  setStatus('已发送，正在等待模型响应...');
  persistState();
  renderTranscript();

  try {
    vscode.postMessage({ type: 'sendMessage', content });
  } catch (error) {
    reportWebviewError(error);
    return false;
  }

  return true;
}

function handleStateMessage(message) {
  const incoming = message.state ?? {};

  if (Array.isArray(incoming.sessions)) {
    uiState.sessions = incoming.sessions;
  }

  if (incoming.workspaceRoot !== undefined) {
    uiState.workspaceRoot = incoming.workspaceRoot;
  }

  if (incoming.workspaceName !== undefined) {
    uiState.workspaceName = incoming.workspaceName;
  }

  if (incoming.session !== undefined) {
    uiState.currentSession = incoming.session;
  }

  if (Array.isArray(incoming.messages)) {
    uiState.messages = incoming.messages;
  }

  if (Array.isArray(incoming.traceEvents)) {
    uiState.traceEvents = incoming.traceEvents;
  }

  if (incoming.activeJob !== undefined) {
    uiState.activeJob = incoming.activeJob;
  }

  const activeSessionId = uiState.currentSession?.session_id;
  const pendingTurn = activeSessionId ? uiState.pendingTurns.get(activeSessionId) : null;
  if (pendingTurn) {
    const backendTurns = splitMessagesIntoTurns(uiState.messages.filter((item) => item.session_id === activeSessionId));
    const lastBackendTurn = backendTurns[backendTurns.length - 1];

    if (pendingTurn.status === 'done' && lastBackendTurn?.userMessage?.content === pendingTurn.userMessage?.content && lastBackendTurn.assistantMessages.length > 0) {
      uiState.pendingTurns.delete(activeSessionId);
    }
  }

  setStatus(message.status ?? uiState.status ?? '已同步状态');
  persistState();
}

function handleMessageAccepted(message) {
  const sessionId = message.sessionId;
  if (!sessionId) {
    return;
  }

  const pendingTurn = uiState.pendingTurns.get(sessionId) ?? ensurePendingTurn(sessionId);
  pendingTurn.jobId = message.jobId ?? null;
  pendingTurn.status = 'running';
  pendingTurn.pending = true;
  pendingTurn.assistantMessages = pendingTurn.assistantMessages ?? [];
  pendingTurn.events = pendingTurn.events ?? [];

  uiState.activeJob = {
    jobId: message.jobId ?? null,
    sessionId,
    status: 'running',
    messageId: message.messageId ?? null,
    content: message.content ?? '',
  };

  setStatus('任务已提交，开始接收思考过程...');
  persistState();
}

function handleJobEvent(message) {
  const sessionId = message.sessionId;
  const jobId = message.jobId;
  if (!sessionId || !jobId) {
    return;
  }

  const pendingTurn = uiState.pendingTurns.get(sessionId);
  if (!pendingTurn) {
    return;
  }

  if (pendingTurn.jobId && pendingTurn.jobId !== jobId) {
    return;
  }

  pendingTurn.jobId = jobId;
  pendingTurn.events = [
    ...(pendingTurn.events ?? []),
    {
      event_type: message.eventType ?? 'event',
      data: message.payload ?? {},
      timestamp: message.payload?.timestamp ?? new Date().toISOString(),
    },
  ];

  if (['job_completed', 'job_failed', 'job_cancelled'].includes(String(message.eventType ?? '').toLowerCase())) {
    pendingTurn.status = message.eventType === 'job_completed' ? 'done' : 'error';
    pendingTurn.pending = false;
    uiState.activeJob = {
      ...(uiState.activeJob ?? {}),
      jobId,
      sessionId,
      status: message.eventType,
    };
  }

  renderTranscript();
  persistState();
}

function initializeWebview() {
  try {
    setStatus(uiState.status || '前端已加载');
    if (expandDetailsToggle) {
      expandDetailsToggle.checked = Boolean(uiState.expandDetails);
    }
  // 初始化时隐藏历史会话面板 - 默认关闭
  const historyPanel = document.getElementById('historyPanel');
  const chatPanel = document.getElementById('chatPanel');
  if (historyPanel) historyPanel.classList.remove('open');
  if (chatPanel) chatPanel.classList.remove('with-history');
  if (historyButton) historyButton.classList.remove('active');
    postDebug(`webview 脚本已启动，readyState=${document.readyState}`);
    vscode.postMessage({ type: 'ready' });
    render();
  } catch (error) {
    reportWebviewError(error);
  }
}

newSessionButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('新建 session');
  vscode.postMessage({ type: 'createSession', title: '新会话' });
});

refreshButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('刷新');
  vscode.postMessage({ type: 'refresh' });
});

sendButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('发送按钮点击');
  void submitCurrentMessage();
});

attachButton?.addEventListener('click', (event) => {
  event.preventDefault();
  showTodoFeedback('附件选择');
});

mentionButton?.addEventListener('click', (event) => {
  event.preventDefault();
  showTodoFeedback('@提及');
});

quickPromptButton?.addEventListener('click', (event) => {
  event.preventDefault();
  showTodoFeedback('快速提示');
});

voiceInputButton?.addEventListener('click', (event) => {
  event.preventDefault();
  showTodoFeedback('语音输入');
});

stopButton?.addEventListener('click', (event) => {
  event.preventDefault();
  showTodoFeedback('停止生成');
});

mentionButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('@提及按钮点击');
  // TODO: 实现@提及选择器
});

quickPromptButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('快速提示按钮点击');
  // TODO: 实现快速提示菜单
});

clearInputButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('清空输入按钮点击');
  inputEl.value = '';
  inputEl.focus();
});

voiceInputButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('语音输入按钮点击');
  // TODO: 实现语音输入功能
});

stopButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('停止生成按钮点击');
  // TODO: 实现停止生成功能
});

pinButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('固定会话按钮点击');
  showTodoFeedback('固定会话');
  // TODO: 实现固定会话功能
});

historyButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('历史会话按钮点击');
  
  const historyPanel = document.getElementById('historyPanel');
  const chatPanel = document.getElementById('chatPanel');
  
  if (!historyPanel || !chatPanel) return;
  
  // 切换面板状态
  const isOpen = historyPanel.classList.contains('open');
  
  if (isOpen) {
    // 关闭面板
    historyPanel.classList.remove('open');
    chatPanel.classList.remove('with-history');
    historyButton.classList.remove('active');
  } else {
    // 打开面板
    historyPanel.classList.add('open');
    chatPanel.classList.add('with-history');
    historyButton.classList.add('active');
    
    // 显示面板时刷新会话列表
    renderSessionList();
    // 向后端请求最新会话列表
    vscode.postMessage({ type: 'listSessions' });
  }
});

viewToggleButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('视图切换按钮点击');
  showTodoFeedback('视图切换');
  // TODO: 实现视图切换功能
});

modelSelectButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('模型选择按钮点击');
  showTodoFeedback('模型选择');
  // TODO: 实现模型选择功能
});

contextButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('上下文按钮点击');
  showTodoFeedback('上下文管理');
  // TODO: 实现上下文管理功能
});

helpButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('帮助按钮点击');
  showTodoFeedback('帮助');
  // TODO: 实现帮助功能
});

settingsButton?.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('设置按钮点击');
  showTodoFeedback('设置');
  // TODO: 实现设置功能
});

expandDetailsToggle?.addEventListener('change', (event) => {
  uiState.expandDetails = Boolean(event.target.checked);
  persistState();
  renderTranscript();
});

inputEl?.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') {
    return;
  }

  if (event.ctrlKey) {
    event.preventDefault();
    const start = inputEl.selectionStart ?? inputEl.value.length;
    const end = inputEl.selectionEnd ?? inputEl.value.length;
    const value = inputEl.value;
    inputEl.value = value.slice(0, start) + '\n' + value.slice(end);
    const nextPosition = start + 1;
    inputEl.setSelectionRange(nextPosition, nextPosition);
    postDebug('Ctrl+Enter 插入换行');
    return;
  }

  if (event.metaKey || event.shiftKey) {
    return;
  }

  event.preventDefault();
  postDebug('Enter 发送');
  void submitCurrentMessage();
});

// 输入框内容变化时更新按钮状态
inputEl?.addEventListener('input', () => {
  const hasContent = inputEl.value.trim().length > 0;
  sendButton.disabled = !hasContent;
  clearInputButton.classList.toggle('hidden', !hasContent);
});

// 按钮显示/隐藏逻辑占位
function updateButtonStates() {
  const isGenerating = uiState.activeJob?.status === 'running';
  
  // 生成中显示停止按钮，隐藏发送按钮
  sendButton.classList.toggle('hidden', isGenerating);
  stopButton.classList.toggle('hidden', !isGenerating);
  
  // 输入框有内容时显示清空按钮
  const hasContent = inputEl?.value?.trim().length > 0;
  clearInputButton.classList.toggle('hidden', !hasContent);
}

// 在状态更新时调用
const originalRender = render;
render = function() {
  originalRender();
  updateButtonStates();
};

sessionListEl?.addEventListener('click', (event) => {
  // 如果点击的是session-id元素，不触发会话切换，允许文本选择
  if (event.target?.classList?.contains('session-id')) {
    return;
  }
  
  const button = event.target?.closest?.('[data-select-session]');
  if (!button) {
    return;
  }

  const sessionId = button.getAttribute('data-select-session');
  if (!sessionId) {
    return;
  }

  postDebug(`切换 session: ${sessionId}`);
  vscode.postMessage({ type: 'selectSession', sessionId });
  
  // ✅ 官方 Copilot Chat 行为: 点击历史会话不会自动关闭面板
  // 保持面板打开状态，用户可以继续浏览历史
  event.stopPropagation();
});

// 消息操作按钮点击事件
turnListEl?.addEventListener('click', (event) => {
  const button = event.target?.closest?.('.action-btn');
  if (!button) {
    return;
  }

  const action = button.getAttribute('data-action');
  const messageElement = button.closest('.chat-message');
  
  postDebug(`消息操作按钮点击: ${action}`);

  switch (action) {
    case 'copy':
      showTodoFeedback('复制完整回复');
      break;
    case 'regenerate':
      showTodoFeedback('重新生成');
      break;
    case 'thumbs-up':
      showTodoFeedback('点赞');
      break;
    case 'thumbs-down':
      showTodoFeedback('点踩');
      break;
    case 'edit-retry':
      showTodoFeedback('编辑并重试');
      break;
    case 'insert-editor':
      showTodoFeedback('插入到编辑器');
      break;
    case 'run-terminal':
      showTodoFeedback('在终端运行');
      break;
    case 'create-file':
      showTodoFeedback('创建新文件');
      break;
    case 'explain-code':
      showTodoFeedback('解释代码');
      break;
    case 'optimize-code':
      showTodoFeedback('优化代码');
      break;
    case 'add-comments':
      showTodoFeedback('添加注释');
      break;
    case 'share':
      showTodoFeedback('分享');
      break;
  }
});

// 代码块操作按钮点击事件
turnListEl?.addEventListener('click', (event) => {
  const button = event.target?.closest?.('.code-action-btn');
  if (!button) {
    return;
  }

  const action = button.getAttribute('data-code-action');
  const codeBlock = button.closest('.code-block-container');
  const codeElement = codeBlock?.querySelector('code');
  const codeContent = codeElement?.textContent || '';
  
  postDebug(`代码块操作按钮点击: ${action}`);

  switch (action) {
    case 'copy':
      showTodoFeedback('复制代码块');
      break;
    case 'insert-cursor':
      showTodoFeedback('插入光标处');
      break;
    case 'replace-selection':
      showTodoFeedback('替换选中内容');
      break;
    case 'run-terminal':
      showTodoFeedback('在终端执行');
      break;
    case 'new-file':
      showTodoFeedback('新建文件');
      break;
    case 'view-diff':
      showTodoFeedback('查看差异');
      break;
  }
});

// ✅ 官方 Copilot Chat 行为: 点击外部不关闭面板
// 只有再次点击历史按钮才会关闭面板
// 移除点击外部自动关闭逻辑

window.addEventListener('message', (event) => {
  const message = event.data;
  if (!message || !message.type) {
    return;
  }

  if (message.type === 'state') {
    handleStateMessage(message);
    render();
    return;
  }

  if (message.type === 'messageAccepted') {
    handleMessageAccepted(message);
    renderTranscript();
    return;
  }

  if (message.type === 'jobEvent') {
    handleJobEvent(message);
    return;
  }

  if (message.type === 'sessionCreated') {
    uiState.currentSession = message.session ?? uiState.currentSession;
    render();
    persistState();
    return;
  }

  if (message.type === 'error') {
    setStatus(message.message ?? '发生错误', true);
  }
});

window.addEventListener('error', (event) => {
  reportWebviewError(event.error || event.message || 'webview 发生未捕获错误');
});

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason || 'webview 发生未处理的 Promise 拒绝';
  reportWebviewError(reason);
  console.error('[UnhandledRejection]', reason);
  if (event.reason instanceof Error) {
    console.error(event.reason.stack);
  }
});

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializeWebview, { once: true });
} else {
  initializeWebview();
}
