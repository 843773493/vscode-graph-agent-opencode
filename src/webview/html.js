const htmlEscape = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

export function renderSidebarHtml(webview, boot) {
  const cspSource = webview.cspSource;
  const nonce = boot.nonce;
  const scriptUri = boot.scriptUri;
  const initialState = {
    workspaceRoot: boot.workspaceRoot ?? '',
    workspaceName: boot.workspaceName ?? 'workspace',
    sessions: Array.isArray(boot.sessions) ? boot.sessions : [],
    session: boot.session ?? null,
    messages: Array.isArray(boot.messages) ? boot.messages : [],
    traceEvents: Array.isArray(boot.traceEvents) ? boot.traceEvents : [],
    activeJob: boot.activeJob ?? null,
  };
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${cspSource} https: data:; style-src ${cspSource} 'unsafe-inline'; script-src ${cspSource} 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BoxTeam</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      font-weight: var(--vscode-font-weight);
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: var(--vscode-editor-background);
      color: var(--vscode-editor-foreground);
      font-size: var(--vscode-font-size);
      line-height: var(--vscode-line-height);
    }

    body {
      display: flex;
    }

    .app-shell {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      min-width: 0;
      min-height: 0;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 8px;
      border-bottom: 1px solid var(--vscode-panel-border);
      background: var(--vscode-editor-background);
      flex: 0 0 auto;
      min-height: 35px;
    }

    .toolbar-group {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex-wrap: wrap;
    }

    .toolbar-brand {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 0;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .toolbar-separator {
      width: 1px;
      height: 20px;
      background: var(--border);
      margin: 0 2px;
    }

    .toolbar button {
      border: 1px solid var(--vscode-button-border);
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border-radius: 4px;
      padding: 4px 11px;
      height: 26px;
      font-size: 12px;
      cursor: pointer;
      line-height: 18px;
    }

    .toolbar button:hover {
      background: var(--vscode-button-hoverBackground);
    }

    .toolbar button:focus {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: -1px;
    }

    .toolbar label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      user-select: none;
    }

    .toolbar input[type="checkbox"] {
      accent-color: var(--accent);
    }

    .info-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      background: #fbfdff;
      color: var(--muted);
      flex: 0 0 auto;
    }

    .info-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
      flex: 1 1 auto;
    }

    .info-chip strong {
      color: var(--text);
      font-weight: 600;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      display: block;
    }

    .info-chip span {
      white-space: nowrap;
      flex: 0 0 auto;
      font-size: 11px;
    }

    #status.error {
      color: var(--danger);
    }

    .content {
      display: grid;
      grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
      gap: 0;
      flex: 1 1 auto;
      min-height: 0;
      overflow: hidden;
    }

    .panel {
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 0;
      border: 0;
      border-right: 1px solid var(--vscode-panel-border);
      background: var(--vscode-editor-background);
      overflow: hidden;
    }

    .panel:last-child {
      border-right: 0;
    }

    .panel-header {
      padding: 9px 12px;
      border-bottom: 1px solid var(--border);
      background: var(--surface-soft);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .panel-body {
      min-height: 0;
      overflow: auto;
      padding: 8px;
    }

    .session-group {
      margin-bottom: 10px;
    }

    .session-group summary {
      list-style: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 10px;
      border: 1px solid transparent;
      border-radius: 9px;
      background: transparent;
      color: var(--text);
      font-weight: 600;
      line-height: 1.2;
    }

    .session-group summary::-webkit-details-marker {
      display: none;
    }

    .session-group[open] > summary,
    .session-group summary:hover {
      background: #f2f7fc;
      border-color: #dde7f1;
    }

    .session-count {
      color: var(--muted);
      font-size: 11px;
      font-weight: 500;
    }

    .session-list {
      margin: 8px 0 0;
      padding: 0;
      list-style: none;
    }

    .session-item {
      display: flex;
      flex-direction: column;
      gap: 3px;
      margin: 0 0 6px;
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
      cursor: pointer;
      transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
      text-align: left;
      width: 100%;
    }

    .session-item:hover {
      border-color: var(--border-strong);
      transform: translateY(-1px);
      box-shadow: 0 10px 18px rgba(15, 23, 42, 0.06);
    }

    .session-item.active {
      border-color: rgba(37, 99, 235, 0.38);
      background: #f6fbff;
      box-shadow: inset 3px 0 0 rgba(37, 99, 235, 0.8);
    }

    .session-title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .session-title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
      font-size: 12px;
    }

    .session-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      color: var(--muted);
      font-size: 11px;
    }

    .session-preview {
      color: #556173;
      font-size: 11px;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 1;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      border: 1px solid transparent;
      padding: 2px 7px;
      font-size: 10px;
      line-height: 1.4;
      white-space: nowrap;
    }

    .badge.neutral {
      background: #eff3f8;
      color: #475569;
      border-color: #d9e0ea;
    }

    .badge.active {
      background: var(--accent-soft);
      color: var(--accent);
      border-color: rgba(37, 99, 235, 0.22);
    }

    .badge.success {
      background: rgba(15, 118, 110, 0.1);
      color: var(--success);
      border-color: rgba(15, 118, 110, 0.18);
    }

    .badge.warning {
      background: rgba(180, 83, 9, 0.1);
      color: var(--warning);
      border-color: rgba(180, 83, 9, 0.18);
    }

    .badge.danger {
      background: rgba(185, 28, 28, 0.1);
      color: var(--danger);
      border-color: rgba(185, 28, 28, 0.18);
    }

    .request-container {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-bottom: 10px;
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      background: var(--surface);
      overflow: hidden;
      box-shadow: var(--shadow);
    }

    .request-container summary {
      list-style: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 12px;
      background: var(--surface-soft);
      border-bottom: 1px solid var(--border);
      font-weight: 600;
    }

    .request-container summary::-webkit-details-marker {
      display: none;
    }

    .request-container[open] summary,
    .request-container summary:hover {
      background: #f2f7fc;
    }

    .request-main {
      display: flex;
      align-items: baseline;
      gap: 6px;
      min-width: 0;
      flex-wrap: wrap;
    }

    .request-chevron {
      color: var(--muted);
      font-size: 11px;
      line-height: 1;
      flex: 0 0 auto;
    }

    .request-title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 0 0 auto;
    }

    .request-subtitle {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-size: 11px;
      font-weight: 500;
      flex: 1 1 auto;
    }

    .request-stats {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }

    .request-details {
      padding: 8px 10px 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .turn-section {
      border-left: 1px solid #d9e1ec;
      margin-left: 7px;
      padding-left: 10px;
    }

    .turn-section h3 {
      margin: 0 0 4px;
      color: var(--text);
      font-size: 13px;
      font-weight: 600;
      line-height: 1.3;
    }

    .turn-section-note {
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.4;
    }

    .turn-section-body {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .editor-shell {
      border: 1px solid #e2e8f1;
      border-radius: 9px;
      background: #fcfdff;
      overflow: hidden;
    }

    .editor-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      border-bottom: 1px solid #e2e8f1;
      background: #f8fbff;
      font-size: 12px;
      font-weight: 600;
    }

    .editor-body {
      padding: 10px 12px;
    }

    .editor-body pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.55;
      color: #1f2937;
    }

    .section-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 6px;
    }

    .detail-block {
      border: 0;
      border-left: 1px solid #d9e1ec;
      border-radius: 0;
      background: transparent;
      overflow: hidden;
      margin-left: 7px;
      padding-left: 10px;
    }

    .detail-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 3px 0 5px;
      border-bottom: 0;
      background: transparent;
      font-weight: 600;
      font-size: 12px;
    }

    .detail-body {
      padding: 0 0 6px;
    }

    .reply {
      color: var(--vscode-editor-foreground);
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: var(--vscode-font-size);
    }

    .reply h1,
    .reply h2,
    .reply h3,
    .reply h4,
    .reply h5,
    .reply h6 {
      margin: 0 0 8px;
      line-height: 1.3;
      color: var(--text);
    }

    .reply p {
      margin: 0 0 8px;
    }

    .reply p:last-child {
      margin-bottom: 0;
    }

    .reply ul,
    .reply ol {
      margin: 0 0 8px;
      padding-left: 20px;
    }

    .reply blockquote {
      margin: 0 0 8px;
      padding: 8px 12px;
      border-left: 3px solid rgba(0, 120, 212, 0.36);
      border-radius: 8px;
      background: #f6faff;
      color: #526173;
    }

    .reply a {
      color: var(--accent);
      text-decoration: none;
    }

    .reply a:hover {
      text-decoration: underline;
    }

    .reply code {
      padding: 1px 4px;
      border-radius: 3px;
      background: var(--vscode-textCodeBlock-background);
      color: var(--vscode-editor-foreground);
      font-family: var(--vscode-editor-font-family);
      font-size: var(--vscode-editor-font-size);
    }

    .reply pre {
      margin: 8px 0;
      padding: 8px 10px;
      border-radius: 4px;
      background: var(--vscode-textCodeBlock-background);
      color: var(--vscode-editor-foreground);
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--vscode-editor-font-family);
      font-size: var(--vscode-editor-font-size);
      line-height: var(--vscode-editor-line-height);
      border: 1px solid var(--vscode-panel-border);
    }

    .reply pre:last-child {
      margin-bottom: 0;
    }

    .reply pre code {
      padding: 0;
      border-radius: 0;
      background: transparent;
      color: inherit;
      font-size: inherit;
    }

    .reply p {
      margin: 0 0 8px;
    }

    .reply p:last-child {
      margin-bottom: 0;
    }

    .code-block {
      margin: 0;
      padding: 12px;
      border-radius: 10px;
      background: #0f172a;
      color: #e2e8f0;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.6;
      font-size: 12px;
    }

    .trace-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .output-stream {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-left: 7px;
      padding-left: 10px;
      border-left: 1px solid #d9e1ec;
    }

    .output-stream-title {
      margin: 0 0 4px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .output-stream-body {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .output-event-card {
      border: 1px solid #e3e8ef;
      border-radius: 8px;
      background: #fff;
      overflow: hidden;
    }

    .trace-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      background: #f8fbff;
      border-bottom: 1px solid #e3e8ef;
      font-weight: 600;
      font-size: 12px;
    }

    .trace-body,
    .editor-body pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      color: #1f2937;
      font-size: 11px;
      line-height: 1.5;
    }

    .trace-body {
      padding: 8px 10px;
    }

    .empty-state {
      padding: 14px 12px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed #cfd7e2;
      border-radius: 10px;
      background: #fafcff;
    }

    .composer {
      flex: 0 0 auto;
      display: flex;
      flex-direction: column;
      gap: 0;
      padding: 8px 12px 12px;
      border-top: 1px solid var(--vscode-panel-border);
      background: var(--vscode-editor-background);
    }

    .composer textarea {
      width: 100%;
      min-height: 52px;
      max-height: 200px;
      resize: none;
      border: 1px solid var(--vscode-input-border);
      border-radius: 4px;
      padding: 8px 10px;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      font: inherit;
      line-height: 1.4;
      outline: none;
      overflow-y: auto;
    }

    .composer textarea:focus {
      border-color: var(--vscode-focusBorder);
    }

    .composer textarea::placeholder {
      color: var(--vscode-input-placeholderForeground);
      opacity: 1;
    }

    .composer-hint {
      color: var(--vscode-descriptionForeground);
      font-size: 11px;
      line-height: 1.4;
      margin-top: 6px;
      padding: 0 2px;
    }

    .composer-actions {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      margin-top: 4px;
      padding: 0 2px;
      min-height: 24px;
    }

    .composer-actions button {
      height: 24px;
      padding: 4px 8px;
      border: none;
      border-radius: 2px;
      background: transparent;
      color: var(--vscode-icon-foreground);
      font-weight: 400;
      cursor: pointer;
      font-size: 13px;
      line-height: 16px;
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .composer-actions button.send-button {
      padding: 4px 12px 4px 8px;
    }

    .composer-actions button svg {
      width: 16px;
      height: 16px;
      fill: currentColor;
    }

    .composer-actions button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .composer-actions button:hover:not(:disabled) {
      background: var(--vscode-list-hoverBackground);
    }

    .composer-actions button:active:not(:disabled) {
      opacity: 0.8;
    }

    .composer-actions button:focus-visible {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: 1px;
    }

    @media (max-width: 900px) {
      .content {
        grid-template-columns: 1fr;
      }

      .info-row {
        gap: 8px;
      }

      .info-chip span {
        display: none;
      }

      .composer {
        grid-template-columns: 1fr;
      }

      .composer-actions {
        justify-content: stretch;
      }

      .composer-actions button {
        width: 100%;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="toolbar">
      <div class="toolbar-group">
        <div class="toolbar-brand">BoxTeam</div>
        <div class="toolbar-separator" aria-hidden="true"></div>
        <button id="newSessionButton" type="button">New Session</button>
        <button id="refreshButton" type="button">Refresh</button>
      </div>
      <div class="toolbar-group">
        <label><input id="expandDetailsToggle" type="checkbox" checked /> Expand details</label>
      </div>
    </header>

    <div class="info-row">
      <div class="info-chip"><strong id="workspace">Workspace</strong><span id="workspaceStatus">ready</span></div>
      <div class="info-chip"><span id="status" aria-live="polite">同步中…</span></div>
    </div>

    <main class="content">
      <section class="panel">
        <div class="panel-header">Sessions</div>
        <div class="panel-body" id="sessionList"></div>
      </section>

      <section class="panel">
        <div class="panel-header">Transcript</div>
        <div class="panel-body" id="turnList"></div>
      </section>
    </main>

    <footer class="composer">
      <div class="composer-copy">
        <textarea id="input" placeholder="输入消息后回车发送，Ctrl+Enter 换行"></textarea>
        <div class="composer-hint">Enter 发送 · Ctrl+Enter 插入换行</div>
      </div>
    <div class="composer-actions">
        <button id="sendButton" type="button" disabled class="send-button">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z"/>
          </svg>
        </button>
    </div>
    </footer>
  </div>
  <script id="graph-agent-boot" type="application/json">${JSON.stringify(initialState).replaceAll('<', '\\u003c')}</script>
  <script nonce="${nonce}" src="${htmlEscape(scriptUri)}"></script>
</body>
</html>`;
}
