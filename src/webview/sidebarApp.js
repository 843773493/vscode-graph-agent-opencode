const bootElement = document.getElementById('graph-agent-boot');
const boot = bootElement ? JSON.parse(bootElement.textContent || '{}') : {};
const vscode = acquireVsCodeApi();

const workspaceEl = document.getElementById('workspace');
const conversationTitleEl = document.getElementById('conversationTitle');
const conversationMetaEl = document.getElementById('conversationMeta');
const sessionListEl = document.getElementById('sessionList');
const turnListEl = document.getElementById('turnList');
const inputEl = document.getElementById('input');
const statusEl = document.getElementById('status');
const sendButton = document.getElementById('send');
const newSessionButton = document.getElementById('newSessionButton');
const refreshButton = document.getElementById('refreshButton');

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
};

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
    pendingTurns: Array.from(uiState.pendingTurns.values()),
  });
}

function escapeHtml(value) {
  return String(value)
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

    output.push(`<pre class="code-block"><code data-lang="${escapeHtml(codeLang)}">${escapeHtml(codeLines.join('\n'))}</code></pre>`);
    codeLines = [];
    codeLang = '';
  };

  const flushList = () => {
    if (!listItems.length) {
      return;
    }

    const tag = listType === 'ol' ? 'ol' : 'ul';
    output.push(`<${tag} class="markdown-list">${listItems.map((item) => `<li>${applyInlineMarkdown(item)}</li>`).join('')}</${tag}>`);
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

  const flushAllText = () => {
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
        flushAllText();
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
      flushAllText();
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      flushAllText();
      const level = Math.min(headingMatch[1].length, 6);
      output.push(`<h${level} class="markdown-heading">${applyInlineMarkdown(headingMatch[2])}</h${level}>`);
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
      flushAllText();
      output.push(`<blockquote class="markdown-quote">${applyInlineMarkdown(trimmed.slice(1).trim())}</blockquote>`);
      continue;
    }

    if (listItems.length) {
      flushList();
    }

    paragraph.push(trimmed);
  }

  flushAllText();
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

function groupTraceEvents(traceEvents) {
  const groups = [];
  let currentGroup = null;

  for (const rawEvent of traceEvents ?? []) {
    const event = normalizeTraceEvent(rawEvent);

    if (event.event_type === 'agent_start' || !currentGroup) {
      currentGroup = {
        startedAt: event.timestamp,
        endedAt: null,
        summary: event.event_type === 'agent_start' && event.data?.message ? formatSnippet(event.data.message, 96) : 'Agent 执行',
        events: [],
      };
      groups.push(currentGroup);
    }

    currentGroup.events.push(event);

    if (event.event_type === 'agent_end') {
      currentGroup.endedAt = event.timestamp;
      currentGroup = null;
    }
  }

  return groups;
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

function renderSessionList() {
  const sessions = [...uiState.sessions].sort((a, b) => new Date(b.updated_at ?? 0) - new Date(a.updated_at ?? 0));
  const activeSessionId = uiState.currentSession?.session_id;

  if (!sessions.length) {
    sessionListEl.innerHTML = '<div class="empty-state">暂无会话，点击 New Session 创建第一条会话。</div>';
    return;
  }

  sessionListEl.innerHTML = sessions
    .map((session) => {
      const turns = splitMessagesIntoTurns(uiState.messages.filter((message) => message.session_id === session.session_id));
      const pendingTurn = uiState.pendingTurns.get(session.session_id);
      const totalTurns = turns.length + (pendingTurn ? 1 : 0);
      const latestTurn = [...turns, pendingTurn ?? null].filter(Boolean).at(-1);
      const preview = latestTurn?.userMessage?.content || latestTurn?.assistantMessages?.at?.(-1)?.content || '暂无消息';
      const isActive = session.session_id === activeSessionId;
      const jobStatus = isActive && uiState.activeJob?.jobId ? uiState.activeJob.status : '';

      return `
        <details class="session-card" data-session-id="${escapeHtml(session.session_id)}" ${isActive ? 'open' : ''}>
          <summary class="session-summary">
            <div style="min-width:0; display:grid; gap:4px;">
              <div class="session-title">${escapeHtml(session.title || '新会话')}</div>
              <div class="session-meta">${escapeHtml(session.session_id)} · ${totalTurns} turn${totalTurns === 1 ? '' : 's'}</div>
            </div>
            <span class="badge ${jobStatus === 'running' ? 'running' : ''}">${escapeHtml(jobStatus || (isActive ? 'active' : 'idle'))}</span>
          </summary>
          <div class="session-body">
            <div class="session-preview">${escapeHtml(formatSnippet(preview, 120))}</div>
            <div class="session-meta">更新于 ${escapeHtml(formatTime(session.updated_at) || '未知')}</div>
            <button class="session-action" type="button" data-select-session="${escapeHtml(session.session_id)}">切换到此会话</button>
          </div>
        </details>
      `;
    })
    .join('');
}

function renderTraceSummary(eventType, payload) {
  const type = String(eventType ?? '').toLowerCase();

  if (type === 'agent_start') {
    return `开始处理：${payload?.message ? formatSnippet(payload.message, 120) : '启动 agent'}`;
  }

  if (type === 'agent_step') {
    return payload?.phase ? `阶段：${payload.phase}` : '执行中';
  }

  if (type === 'tool_call_start') {
    return `调用工具：${payload?.tool_name || 'unknown_tool'}\n\n\`\`\`json\n${JSON.stringify(payload?.args ?? {}, null, 2)}\n\`\`\``;
  }

  if (type === 'tool_call_end') {
    return `工具完成：${payload?.tool_name || 'unknown_tool'}\n\n\`\`\`text\n${String(payload?.result ?? '')}\n\`\`\``;
  }

  if (type === 'file_write') {
    return `文件写入：${payload?.path || payload?.file_path || 'unknown path'}\n\n\`\`\`text\n${String(payload?.summary ?? payload?.result ?? '')}\n\`\`\``;
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

function renderTraceEventCard(event) {
  const eventType = event.event_type ?? 'event';
  const payload = event.data ?? {};

  return `
    <article class="event-card">
      <div class="event-head">
        <div class="event-title">${escapeHtml(String(eventType).replaceAll('_', ' '))}</div>
        <span class="event-type">${escapeHtml(formatTime(event.timestamp) || 'now')}</span>
      </div>
      <div class="event-content markdown-body">${renderMarkdown(renderTraceSummary(eventType, payload))}</div>
    </article>
  `;
}

function renderTraceGroup(group, index) {
  const title = group?.summary || `Trace #${index + 1}`;
  const startedAt = formatTime(group?.startedAt);
  const events = group?.events ?? [];

  return `
    <details class="thinking-panel" open>
      <summary class="thinking-summary">
        <div style="display:flex; align-items:center; gap:10px; min-width:0;">
          <span class="thinking-spinner" aria-hidden="true"></span>
          <span class="thinking-title">${escapeHtml(title)}</span>
        </div>
        <span class="pill">${escapeHtml(startedAt || 'now')} · ${escapeHtml(String(events.length))} events</span>
      </summary>
      <div class="thinking-body">
        <div class="event-list">
          ${events.map((event) => renderTraceEventCard(event)).join('')}
        </div>
      </div>
    </details>
  `;
}

function renderThoughtPanel(turn, index) {
  const traceGroups = groupTraceEvents(uiState.traceEvents);
  const traceGroup = traceGroups[index] ?? null;

  if (traceGroup) {
    return renderTraceGroup(traceGroup, index);
  }

  const events = turn.events ?? [];
  const open = events.length > 0 || turn.pending;

  return `
    <details class="thinking-panel" ${open ? 'open' : ''}>
      <summary class="thinking-summary">
        <div style="display:flex; align-items:center; gap:10px; min-width:0;">
          <span class="thinking-spinner" aria-hidden="true"></span>
          <span class="thinking-title">思考过程</span>
        </div>
        <span class="pill">${escapeHtml(String(events.length))} events</span>
      </summary>
      <div class="thinking-body">
        ${events.length ? `<div class="event-list">${events.map((event) => renderTraceEventCard(normalizeTraceEvent(event))).join('')}</div>` : '<div class="thinking-empty">暂无思考事件，等待模型输出中。</div>'}
      </div>
    </details>
  `;
}

function renderTurn(turn, index, totalTurns) {
  const isLast = index === totalTurns - 1;
  const isPending = Boolean(turn.pending) || turn.status === 'running';
  const userMessage = turn.userMessage;
  const assistantMessages = turn.assistantMessages ?? [];
  const latestAssistant = assistantMessages[assistantMessages.length - 1] ?? null;
  const open = isPending || isLast;
  const summaryText = userMessage?.content ? formatSnippet(userMessage.content, 72) : latestAssistant?.content ? formatSnippet(latestAssistant.content, 72) : '未命名 turn';
  const statusClass = turn.status === 'error' ? 'error' : turn.status === 'completed' || turn.status === 'done' ? 'complete' : 'running';

  return `
    <details class="turn-card" ${open ? 'open' : ''} data-turn-id="${escapeHtml(turn.turnId)}">
      <summary class="turn-summary">
        <div style="min-width:0; display:grid; gap:4px;">
          <div class="turn-title">${escapeHtml(summaryText)}</div>
          <div class="turn-meta">${escapeHtml(userMessage?.created_at ? formatTime(userMessage.created_at) : latestAssistant?.created_at ? formatTime(latestAssistant.created_at) : '进行中')}</div>
        </div>
        <span class="status-pill ${statusClass}">${escapeHtml(isPending ? 'running' : turn.status || 'done')}</span>
      </summary>
      <div class="turn-body">
        ${userMessage ? `
          <section class="bubble bubble-user">
            <div class="bubble-header">
              <span>User</span>
              <span>${escapeHtml(formatTime(userMessage.created_at) || '')}</span>
            </div>
            <div class="bubble-content markdown-body">${renderMarkdown(userMessage.content || '')}</div>
          </section>
        ` : ''}

        ${assistantMessages.length ? assistantMessages.map((assistantMessage, assistantIndex) => `
          <section class="bubble bubble-assistant">
            <div class="bubble-header">
              <span>Assistant ${assistantIndex === 0 ? '' : `#${assistantIndex + 1}`}</span>
              <span>${escapeHtml(formatTime(assistantMessage.created_at) || '')}</span>
            </div>
            <div class="bubble-content markdown-body">${renderMarkdown(assistantMessage.content || '')}</div>
          </section>
        `).join('') : isPending ? `
          <section class="bubble bubble-assistant">
            <div class="bubble-header">
              <span>Assistant</span>
              <span class="thinking-spinner" aria-hidden="true"></span>
            </div>
            <div class="bubble-content markdown-body">正在思考并调用工具...</div>
          </section>
        ` : ''}

        ${renderThoughtPanel(turn, index)}
      </div>
    </details>
  `;
}

function renderConversation() {
  const activeSession = getActiveSession();
  const sessionId = activeSession?.session_id;
  const turns = sessionId ? getTurnsForSession(sessionId) : [];

  conversationTitleEl.textContent = activeSession?.title || 'Workspace Chat';
  conversationMetaEl.textContent = activeSession
    ? `${activeSession.session_id} · ${turns.length} turn${turns.length === 1 ? '' : 's'} · ${uiState.activeJob?.status || 'idle'}`
    : '尚未创建会话';

  if (!turns.length) {
    turnListEl.innerHTML = `
      <div class="empty-state">
        <div style="font-weight:700; color: var(--text); margin-bottom: 6px;">暂无对话</div>
        <div>创建新的 session 后，输入内容即可发送。发送后会先显示本地用户消息，再展示思考过程和回复。</div>
      </div>
    `;
    return;
  }

  turnListEl.innerHTML = turns.map((turn, index) => renderTurn(turn, index, turns.length)).join('');
  requestAnimationFrame(() => {
    turnListEl.scrollTop = turnListEl.scrollHeight;
  });
}

function render() {
  workspaceEl.textContent = `${uiState.workspaceName || 'workspace'} · ${uiState.workspaceRoot || ''}`;
  renderSessionList();
  renderConversation();
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
  renderConversation();

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
  pendingTurn.events = [...(pendingTurn.events ?? []), {
    event_type: message.eventType ?? 'event',
    data: message.payload ?? {},
    timestamp: message.payload?.timestamp ?? new Date().toISOString(),
  }];

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

  renderConversation();
  persistState();
}

function initializeWebview() {
  try {
    setStatus(uiState.status || '前端已加载');
    postDebug(`webview 脚本已启动，readyState=${document.readyState}`);
    vscode.postMessage({ type: 'ready' });
    render();
  } catch (error) {
    reportWebviewError(error);
  }
}

newSessionButton.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('新建 session');
  vscode.postMessage({ type: 'createSession', title: '新会话' });
});

refreshButton.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('刷新');
  vscode.postMessage({ type: 'refresh' });
});

sendButton.addEventListener('click', (event) => {
  event.preventDefault();
  postDebug('发送按钮点击');
  void submitCurrentMessage();
});

inputEl.addEventListener('keydown', (event) => {
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

sessionListEl.addEventListener('click', (event) => {
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
    renderConversation();
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
  reportWebviewError(event.reason || 'webview 发生未处理的 Promise 拒绝');
});

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializeWebview, { once: true });
} else {
  initializeWebview();
}