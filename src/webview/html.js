function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

export function renderSidebarHtml(webview, { nonce, scriptUri, apiPort, workspaceRoot, workspaceName, sessions, session, messages, traceEvents, activeJob }) {
  const boot = escapeHtml(
    JSON.stringify({
      apiPort,
      workspaceRoot,
      workspaceName,
      sessions: sessions ?? [],
      session: session ?? null,
      messages: messages ?? [],
      traceEvents: traceEvents ?? [],
      activeJob: activeJob ?? null,
    }),
  );

  return /* html */ `
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
      <meta charset="UTF-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource} https:; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';" />
      <title>Graph Agent</title>
      <style>
        :root {
          color-scheme: dark;
          --bg: #0d1320;
          --panel: #111a2a;
          --panel-2: #182538;
          --panel-3: #202e44;
          --border: #2a3850;
          --text: #e8eef9;
          --muted: #8fa1bb;
          --accent: #7cb7ff;
          --accent-2: #4dd4ff;
          --accent-3: #9ae6b4;
          --danger: #ff8092;
          --shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
        }
        body {
          margin: 0;
          font-family: Inter, 'Segoe UI', system-ui, sans-serif;
          background:
            radial-gradient(circle at top left, rgba(124, 183, 255, 0.14), transparent 32%),
            radial-gradient(circle at top right, rgba(77, 212, 255, 0.08), transparent 26%),
            linear-gradient(180deg, #0c1220 0%, var(--bg) 100%);
          color: var(--text);
          overflow: hidden;
        }
        * {
          box-sizing: border-box;
        }
        button,
        textarea,
        details,
        summary {
          font: inherit;
        }
        .app {
          display: grid;
          grid-template-columns: 320px minmax(0, 1fr);
          height: 100vh;
          min-width: 0;
        }
        .sidebar {
          display: flex;
          flex-direction: column;
          min-width: 0;
          border-right: 1px solid var(--border);
          background: rgba(12, 18, 32, 0.92);
          backdrop-filter: blur(14px);
        }
        .sidebar-header,
        .conversation-header,
        .composer {
          padding: 14px 16px;
          border-bottom: 1px solid var(--border);
        }
        .sidebar-header {
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .brand {
          display: flex;
          align-items: center;
          gap: 12px;
        }
        .brand-mark {
          width: 38px;
          height: 38px;
          border-radius: 14px;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          box-shadow: 0 10px 30px rgba(124, 183, 255, 0.25);
        }
        .brand-title {
          font-size: 14px;
          font-weight: 700;
          letter-spacing: 0.02em;
        }
        .brand-subtitle,
        .header-meta,
        .session-meta,
        .turn-meta,
        .event-meta {
          color: var(--muted);
          font-size: 12px;
        }
        .toolbar,
        .conversation-toolbar,
        .composer-actions {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
        }
        .toolbar button,
        .conversation-toolbar button,
        .composer-actions button,
        .session-action {
          border: 1px solid var(--border);
          border-radius: 999px;
          padding: 7px 12px;
          background: rgba(25, 36, 54, 0.85);
          color: var(--text);
          cursor: pointer;
          transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
        }
        .toolbar button:hover,
        .conversation-toolbar button:hover,
        .composer-actions button:hover,
        .session-action:hover {
          transform: translateY(-1px);
          border-color: rgba(124, 183, 255, 0.48);
          background: rgba(32, 46, 68, 0.96);
        }
        .toolbar button:disabled,
        .conversation-toolbar button:disabled {
          opacity: 0.45;
          cursor: not-allowed;
          transform: none;
        }
        .session-list {
          overflow: auto;
          padding: 10px 10px 14px;
          display: grid;
          gap: 10px;
          min-height: 0;
        }
        .session-card,
        .turn-card,
        .event-card,
        .thinking-panel {
          background: var(--panel);
          border: 1px solid var(--border);
          border-radius: 18px;
          box-shadow: var(--shadow);
        }
        .session-card {
          overflow: hidden;
        }
        .session-card[open] {
          border-color: rgba(124, 183, 255, 0.42);
        }
        .session-card summary,
        .turn-card summary,
        .thinking-panel summary {
          list-style: none;
          cursor: pointer;
        }
        .session-card summary::-webkit-details-marker,
        .turn-card summary::-webkit-details-marker,
        .thinking-panel summary::-webkit-details-marker {
          display: none;
        }
        .session-summary,
        .turn-summary,
        .thinking-summary {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          padding: 12px 14px;
        }
        .session-title,
        .turn-title,
        .thinking-title {
          font-size: 13px;
          font-weight: 700;
          min-width: 0;
        }
        .session-title,
        .turn-title {
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .pill,
        .status-pill,
        .badge,
        .event-type {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border-radius: 999px;
          padding: 4px 9px;
          font-size: 11px;
          border: 1px solid rgba(124, 183, 255, 0.2);
          background: rgba(26, 38, 57, 0.9);
          color: var(--muted);
          white-space: nowrap;
        }
        .status-pill.running {
          color: #b7e8ff;
          border-color: rgba(77, 212, 255, 0.35);
          background: rgba(12, 72, 88, 0.35);
        }
        .status-pill.complete {
          color: #d7ffe5;
          border-color: rgba(154, 230, 180, 0.42);
          background: rgba(20, 80, 50, 0.34);
        }
        .status-pill.error {
          color: #ffd9df;
          border-color: rgba(255, 128, 146, 0.42);
          background: rgba(101, 30, 41, 0.3);
        }
        .session-body {
          padding: 0 14px 14px;
          display: grid;
          gap: 6px;
        }
        .session-preview {
          font-size: 12px;
          color: var(--muted);
          line-height: 1.45;
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .session-card .session-action {
          width: 100%;
          text-align: left;
          border-radius: 14px;
        }
        .conversation {
          display: grid;
          grid-template-rows: auto minmax(0, 1fr) auto;
          min-width: 0;
          min-height: 0;
        }
        .conversation-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 16px;
          background: rgba(15, 24, 39, 0.92);
        }
        .conversation-title {
          font-size: 15px;
          font-weight: 700;
          letter-spacing: 0.01em;
        }
        .conversation-meta {
          margin-top: 4px;
          font-size: 12px;
          color: var(--muted);
        }
        .turn-list {
          flex: 1;
          overflow: auto;
          padding: 14px;
          display: grid;
          gap: 12px;
          min-height: 0;
          align-content: start;
        }
        .empty-state {
          margin: auto;
          max-width: 440px;
          width: 100%;
          padding: 26px;
          border: 1px dashed var(--border);
          border-radius: 22px;
          color: var(--muted);
          background: rgba(18, 26, 40, 0.75);
          text-align: center;
        }
        .turn-card {
          overflow: hidden;
        }
        .turn-card[open] {
          border-color: rgba(124, 183, 255, 0.38);
        }
        .turn-body {
          padding: 0 14px 14px;
          display: grid;
          gap: 10px;
        }
        .bubble {
          border-radius: 18px;
          padding: 12px 14px;
          border: 1px solid var(--border);
          background: var(--panel-2);
        }
        .bubble-user {
          background: linear-gradient(180deg, rgba(32, 49, 73, 0.96), rgba(25, 38, 57, 0.96));
          border-color: rgba(124, 183, 255, 0.28);
        }
        .bubble-assistant {
          background: linear-gradient(180deg, rgba(24, 37, 56, 0.92), rgba(18, 28, 43, 0.92));
        }
        .bubble-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          margin-bottom: 8px;
          font-size: 12px;
          color: var(--muted);
        }
        .bubble-content {
          white-space: pre-wrap;
          line-height: 1.6;
          font-size: 13px;
        }
        .markdown-body {
          display: grid;
          gap: 8px;
        }
        .markdown-body > :first-child {
          margin-top: 0;
        }
        .markdown-body > :last-child {
          margin-bottom: 0;
        }
        .markdown-heading {
          margin: 0;
          line-height: 1.25;
        }
        .markdown-body p {
          margin: 0;
        }
        .markdown-list {
          margin: 0;
          padding-left: 18px;
          display: grid;
          gap: 4px;
        }
        .markdown-quote {
          margin: 0;
          padding: 8px 12px;
          border-left: 3px solid rgba(124, 183, 255, 0.42);
          color: var(--muted);
          background: rgba(18, 27, 42, 0.75);
          border-radius: 10px;
        }
        .code-block {
          margin: 0;
          padding: 12px;
          border-radius: 14px;
          background: rgba(7, 13, 22, 0.9);
          border: 1px solid rgba(124, 183, 255, 0.18);
          overflow: auto;
        }
        .code-block code {
          display: block;
          white-space: pre;
          background: transparent;
          padding: 0;
          color: #d7e7ff;
        }
        .markdown-body a {
          color: var(--accent-2);
          text-decoration: none;
        }
        .markdown-body a:hover {
          text-decoration: underline;
        }
        .bubble-content code,
        .event-content code {
          font-family: 'Cascadia Mono', 'SFMono-Regular', Consolas, monospace;
          font-size: 12px;
          background: rgba(255, 255, 255, 0.08);
          padding: 1px 5px;
          border-radius: 6px;
        }
        .thinking-panel {
          overflow: hidden;
          background: linear-gradient(180deg, rgba(19, 29, 44, 0.95), rgba(15, 23, 37, 0.95));
        }
        .thinking-panel[open] {
          border-color: rgba(77, 212, 255, 0.32);
        }
        .thinking-body {
          padding: 0 14px 14px;
          display: grid;
          gap: 10px;
        }
        .thinking-empty {
          color: var(--muted);
          font-size: 12px;
        }
        .event-list {
          display: grid;
          gap: 8px;
        }
        .event-card {
          padding: 10px 12px;
          border-radius: 14px;
          background: rgba(22, 34, 51, 0.92);
          box-shadow: none;
        }
        .event-head {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 10px;
          margin-bottom: 6px;
        }
        .event-title {
          font-size: 12px;
          font-weight: 700;
        }
        .event-content {
          white-space: pre-wrap;
          line-height: 1.5;
          font-size: 12px;
          color: var(--text);
        }
        .thinking-spinner {
          width: 10px;
          height: 10px;
          border-radius: 50%;
          border: 2px solid rgba(124, 183, 255, 0.22);
          border-top-color: var(--accent-2);
          animation: spin 900ms linear infinite;
        }
        .composer {
          display: grid;
          gap: 10px;
          border-top: 1px solid var(--border);
          background: rgba(14, 21, 34, 0.96);
        }
        .composer textarea {
          width: 100%;
          min-height: 110px;
          resize: vertical;
          border-radius: 16px;
          border: 1px solid var(--border);
          background: var(--panel-3);
          color: var(--text);
          padding: 12px 14px;
          outline: none;
          line-height: 1.55;
        }
        .composer textarea:focus {
          border-color: rgba(124, 183, 255, 0.55);
          box-shadow: 0 0 0 3px rgba(124, 183, 255, 0.08);
        }
        .composer-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
        }
        .status {
          font-size: 12px;
          color: var(--muted);
        }
        .status.error {
          color: var(--danger);
        }
        .send-button {
          border: 0;
          border-radius: 999px;
          padding: 10px 16px;
          background: linear-gradient(135deg, var(--accent), var(--accent-2));
          color: #08111c;
          font-weight: 800;
          cursor: pointer;
          box-shadow: 0 10px 26px rgba(124, 183, 255, 0.18);
        }
        .send-button:hover {
          transform: translateY(-1px);
        }
        @keyframes spin {
          to {
            transform: rotate(360deg);
          }
        }
        @media (max-width: 900px) {
          .app {
            grid-template-columns: 1fr;
          }

          .sidebar {
            border-right: 0;
            border-bottom: 1px solid var(--border);
            max-height: 320px;
          }
        }
      </style>
    </head>
    <body>
      <div class="app">
        <aside class="sidebar">
          <div class="sidebar-header">
            <div class="brand">
              <div class="brand-mark"></div>
              <div>
                <div class="brand-title">Graph Agent</div>
                <div class="brand-subtitle" id="workspace"></div>
              </div>
            </div>
            <div class="toolbar">
              <button id="newSessionButton" type="button">New Session</button>
              <button id="refreshButton" type="button">Refresh</button>
              <button type="button" disabled>Tools</button>
            </div>
          </div>
          <div class="session-list" id="sessionList"></div>
        </aside>

        <main class="conversation">
          <header class="conversation-header">
            <div>
              <div class="conversation-title" id="conversationTitle">Workspace Chat</div>
              <div class="conversation-meta" id="conversationMeta"></div>
            </div>
            <div class="conversation-toolbar">
              <button type="button" disabled>History</button>
              <button type="button" disabled>Attach</button>
              <button type="button" disabled>Run</button>
            </div>
          </header>

          <div class="turn-list" id="turnList"></div>

          <footer class="composer">
            <textarea id="input" placeholder="输入消息后发送到当前 session"></textarea>
            <div class="composer-row">
              <div class="status" id="status">准备就绪</div>
              <div class="composer-actions">
                <button id="send" class="send-button" type="button">发送</button>
              </div>
            </div>
          </footer>
        </main>
      </div>
      <script id="graph-agent-boot" type="application/json">${boot}</script>
      <script nonce="${nonce}" src="${scriptUri}"></script>
    </body>
    </html>
  `;
}
