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

    output.push(`<pre><code data-lang="${escapeHtml(codeLang)}">${escapeHtml(codeLines.join('\n'))}</code></pre>`);
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
  sessionListEl.innerHTML = '';
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

sessionListEl?.addEventListener('click', (event) => {
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
});

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
