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
      padding: 4px 6px;
      border-bottom: 1px solid var(--vscode-panel-border);
      background: var(--vscode-editor-background);
      flex: 0 0 auto;
      min-height: 30px;
      height: 30px;
    }

    .toolbar-group {
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
    }

    .toolbar-separator {
      width: 1px;
      height: 16px;
      background: var(--vscode-panel-border);
      margin: 0 2px;
      opacity: 0.7;
    }

    .toolbar button {
      border: none;
      background: transparent;
      color: var(--vscode-icon-foreground);
      border-radius: 3px;
      padding: 2px 6px;
      height: 22px;
      font-size: 12px;
      cursor: pointer;
      line-height: 18px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: background 0.1s ease;
    }

    .toolbar button:hover {
      background: var(--vscode-list-hoverBackground);
    }

    .toolbar button:active {
      background: var(--vscode-list-activeSelectionBackground);
    }

    .toolbar button.active {
      background: var(--vscode-list-activeSelectionBackground);
      color: var(--vscode-list-activeSelectionForeground);
    }

    .toolbar button:focus {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: -1px;
    }

    .toolbar button svg {
      width: 16px;
      height: 16px;
      fill: currentColor;
      flex-shrink: 0;
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
      display: flex;
      flex-direction: row;
      flex: 1 1 auto;
      min-height: 0;
      overflow: hidden;
      position: relative;
    }

    /* 历史会话面板 - 官方 Copilot Chat 风格 */
    .history-panel {
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 280px;
      min-width: 280px;
      max-width: 280px;
      border-right: 1px solid var(--vscode-panel-border);
      background: var(--vscode-editor-background);
      overflow: hidden;
      z-index: 10;
      transform: translateX(-100%);
      transition: transform 200ms cubic-bezier(0.4, 0, 0.2, 1);
    }

    .history-panel.open {
      transform: translateX(0);
    }

    .history-panel .panel-header {
      padding: 9px 12px;
      border-bottom: 1px solid var(--vscode-panel-border);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--vscode-descriptionForeground);
    }

    .history-panel .panel-body {
      min-height: 0;
      overflow: auto;
      padding: 8px;
      height: calc(100% - 36px);
    }

    /* 聊天主面板 */
    .chat-panel {
      flex: 1 1 100%;
      min-width: 0;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transition: margin-left 200ms cubic-bezier(0.4, 0, 0.2, 1);
    }

    .chat-panel.with-history {
      margin-left: 280px;
    }

    .chat-panel .panel-header {
      padding: 9px 12px;
      border-bottom: 1px solid var(--vscode-panel-border);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--vscode-descriptionForeground);
    }

    .chat-panel .panel-body {
      min-height: 0;
      overflow: auto;
      padding: 8px;
      flex: 1 1 auto;
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
      justify-content: space-between;
      align-items: center;
      margin-top: 4px;
      padding: 0 2px;
      min-height: 24px;
    }

    .composer-actions-left {
      display: flex;
      align-items: center;
      gap: 0;
    }

    .composer-actions-right {
      display: flex;
      align-items: center;
      gap: 0;
    }

    .composer-actions button {
      width: 24px;
      height: 24px;
      padding: 4px;
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
      justify-content: center;
      gap: 0;
    }

    .composer-actions button.send-button {
      width: auto;
      padding: 4px 12px 4px 8px;
    }

    .composer-actions button svg {
      width: 16px;
      height: 16px;
      fill: currentColor;
      flex-shrink: 0;
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

    .composer-actions button.hidden {
      display: none !important;
    }

    .composer-actions button.hover-only {
      opacity: 0;
      pointer-events: none;
    }

    .composer:hover .composer-actions button.hover-only {
      opacity: 1;
      pointer-events: auto;
    }

    /* 消息操作按钮区 - Copilot Chat 1:1 复刻 */
    .chat-message-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 8px 0 8px;
      min-height: 28px;
    }

    .message-actions {
      display: flex;
      align-items: center;
      gap: 2px;
      margin-left: auto;
      flex-shrink: 0;
    }

    .action-btn {
      width: 22px;
      height: 22px;
      padding: 0;
      border: none;
      border-radius: 2px;
      background: transparent;
      color: var(--vscode-icon-foreground);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.1s ease;
    }

    .action-btn svg {
      width: 14px;
      height: 14px;
      stroke-width: 1.75;
      flex-shrink: 0;
    }

    .action-btn:hover {
      background: var(--vscode-list-hoverBackground);
    }

    .action-btn:active {
      background: var(--vscode-list-activeSelectionBackground);
    }

    .action-btn:focus-visible {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: -1px;
    }

    /* 悬停才显示的按钮 - 默认隐藏 */
    .action-btn.hover-only {
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.12s ease;
    }

    /* 鼠标悬停在消息气泡上时显示全部按钮 */
    .chat-message:hover .action-btn.hover-only {
      opacity: 1;
      pointer-events: auto;
    }

    /* ================================
       代码块操作按钮 - Copilot Chat 1:1 复刻
       ================================ */
    .code-block-container {
      position: relative;
      margin: 8px 0;
    }

    .code-block-actions {
      position: absolute;
      top: 4px;
      right: 4px;
      display: flex;
      align-items: center;
      gap: 2px;
      z-index: 10;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.12s ease;
    }

    .code-block-container:hover .code-block-actions {
      opacity: 1;
      pointer-events: auto;
    }

    .code-action-btn {
      width: 22px;
      height: 22px;
      padding: 0;
      border: none;
      border-radius: 2px;
      background: transparent;
      color: var(--vscode-icon-foreground);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.1s ease;
    }

    .code-action-btn svg {
      width: 14px;
      height: 14px;
      stroke-width: 1.75;
      flex-shrink: 0;
    }

    .code-action-btn:hover {
      background: var(--vscode-list-hoverBackground);
    }

    .code-action-btn:active {
      background: var(--vscode-list-activeSelectionBackground);
    }

    .code-action-btn:focus-visible {
      outline: 1px solid var(--vscode-focusBorder);
      outline-offset: -1px;
    }

    /* 保持原有代码块样式不变 */
    .code-block-container pre {
      margin: 0 !important;
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
    .hidden {
      display: none !important;
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <header class="toolbar">
      <div class="toolbar-group">
        <!-- 左侧按钮组 -->
        <button id="newSessionButton" type="button" title="新建聊天">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M14 2H2C1.44772 2 1 2.44772 1 3V13C1 13.5523 1.44772 14 2 14H8V12H3V4H13V8H15V3C15 2.44772 14.5523 2 14 2ZM10 10V13H13L10 16V14H7V10H10Z"/>
          </svg>
        </button>
        <button id="historyButton" type="button" title="历史记录">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M8 1C4.13401 1 1 4.13401 1 8C1 11.866 4.13401 15 8 15C11.866 15 15 11.866 15 8H13C13 10.7614 10.7614 13 8 13C5.23858 13 3 10.7614 3 8C3 5.23858 5.23858 3 8 3C9.90213 3 11.576 4.01486 12.5355 5.5H10V7H15V2H13V4.25736C11.8234 2.27593 10.0523 1 8 1ZM7 5V9L10.5 11.1L11 10.4L8 8.6V5H7Z"/>
          </svg>
        </button>
        <button id="pinButton" type="button" title="固定会话">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M10 1V2H11V6L13 8V9H9V14H7V9H3V8L5 6V2H6V1H10ZM6 3V6.5L4.5 8H11.5L10 6.5V3H6Z"/>
          </svg>
        </button>
        <button id="viewToggleButton" type="button" title="视图切换">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M2 2H7V7H2V2ZM3 3V6H6V3H3ZM9 2H14V7H9V2ZM10 3V6H13V3H10ZM2 9H7V14H2V9ZM3 10V13H6V10H3ZM9 9H14V14H9V9ZM10 10V13H13V10H10Z"/>
          </svg>
        </button>
      </div>
       <div class="toolbar-group">
         <!-- 右侧按钮组 -->
         <button id="modelSelectButton" type="button" title="选择模型">
           <span>GPT-4o</span>
           <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
             <path d="M8 10L4 6H12L8 10Z"/>
           </svg>
         </button>
         <button id="agentSelectButton" type="button" title="选择Agent">
           <span>default</span>
           <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
             <path d="M12 8c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4zm-2 6c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm0-6c-.55 0-1 .45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z"/>
           </svg>
         </button>
         <div class="toolbar-separator" aria-hidden="true"></div>
         <button id="contextButton" type="button" title="上下文设置">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M8 1C5.79086 1 4 2.79086 4 5C4 6.8625 5.275 8.425 7 8.875V15H9V8.875C10.725 8.425 12 6.8625 12 5C12 2.79086 10.2091 1 8 1ZM8 2C9.65685 2 11 3.34315 11 5C11 6.65685 9.65685 8 8 8C6.34315 8 5 6.65685 5 5C5 3.34315 6.34315 2 8 2Z"/>
          </svg>
        </button>
        <button id="helpButton" type="button" title="帮助">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M8 1C4.13401 1 1 4.13401 1 8C1 11.866 4.13401 15 8 15C11.866 15 15 11.866 15 8C15 4.13401 11.866 1 8 1ZM8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8C14 11.3137 11.3137 14 8 14ZM7 11H9V13H7V11ZM8 4C6.34315 4 5 5.34315 5 7H7C7 6.44772 7.44772 6 8 6C8.55228 6 9 6.44772 9 7C9 8 7 7.75 7 10H9C9 8.75 11 8.5 11 7C11 5.34315 9.65685 4 8 4Z"/>
          </svg>
        </button>
        <button id="settingsButton" type="button" title="设置">
          <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
            <path d="M7.5 0L7 2H9L8.5 0H7.5ZM12.1924 1.3934L10.8284 2.75736L12.2426 4.17157L13.6066 2.80761L12.1924 1.3934ZM14 7V7.5H16V8.5H14V9H14V7ZM13.6066 13.1924L12.2426 11.8284L10.8284 13.2426L12.1924 14.6066L13.6066 13.1924ZM8.5 16H7.5L7 14H9L8.5 16ZM2.80761 14.6066L4.17157 13.2426L2.75736 11.8284L1.3934 13.1924L2.80761 14.6066ZM2 9V8.5H0V7.5H2V7H2V9ZM1.3934 2.80761L2.75736 4.17157L4.17157 2.75736L2.80761 1.3934L1.3934 2.80761ZM8 5C6.34315 5 5 6.34315 5 8C5 9.65685 6.34315 11 8 11C9.65685 11 11 9.65685 11 8C11 6.34315 9.65685 5 8 5ZM8 6C9.10457 6 10 6.89543 10 8C10 9.10457 9.10457 10 8 10C6.89543 10 6 9.10457 6 8C6 6.89543 6.89543 6 8 6Z"/>
          </svg>
        </button>
      </div>
    </header>

    <div class="info-row">
      <div class="info-chip"><strong id="workspace">Workspace</strong><span id="workspaceStatus">ready</span></div>
      <div class="info-chip"><span id="status" aria-live="polite">同步中…</span></div>
    </div>

    <main class="content">
      <!-- 历史会话面板 - 左侧滑入 -->
      <aside class="history-panel" id="historyPanel">
        <div class="panel-header">历史会话</div>
        <div class="panel-body" id="sessionList"></div>
      </aside>

      <!-- 聊天主面板 -->
      <section class="chat-panel" id="chatPanel">
        <div class="panel-header">当前会话</div>
        <div class="panel-body" id="turnList"></div>
      </section>
    </main>

    <footer class="composer">
      <div class="composer-copy">
        <textarea id="input" placeholder="输入消息后回车发送，Ctrl+Enter 换行"></textarea>
        <div class="composer-hint">Enter 发送 · Ctrl+Enter 插入换行</div>
      </div>
    <div class="composer-actions">
        <div class="composer-actions-left">
          <button id="attachButton" type="button" title="添加附件">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M10.5 1L11 2.5H13.5C14.3284 2.5 15 3.17157 15 4V12C15 12.8284 14.3284 13.5 13.5 13.5H2.5C1.67157 13.5 1 12.8284 1 12V4C1 3.17157 1.67157 2.5 2.5 2.5H5L5.5 1H10.5ZM5.5 3L5 4.5H2.5V12H13.5V4.5H11L10.5 3H5.5ZM8 6C6.89543 6 6 6.89543 6 8C6 9.10457 6.89543 10 8 10C9.10457 10 10 9.10457 10 8C10 6.89543 9.10457 6 8 6ZM8 7C8.55228 7 9 7.44772 9 8C9 8.55228 8.55228 9 8 9C7.44772 9 7 8.55228 7 8C7 7.44772 7.44772 7 8 7Z"/>
            </svg>
          </button>
          <button id="mentionButton" type="button" title="@提及">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M8 2C4.68629 2 2 4.68629 2 8C2 11.3137 4.68629 14 8 14C9.86083 14 11.5487 13.1448 12.7071 11.7889L11.2929 10.3747C10.4017 11.3709 9.26278 12 8 12C5.79086 12 4 10.2091 4 8C4 5.79086 5.79086 4 8 4C10.2091 4 12 5.79086 12 8V8.5C12 9.32843 11.3284 10 10.5 10C9.67157 10 9 9.32843 9 8.5V7H7V8.5C7 10.433 8.567 12 10.5 12C12.433 12 14 10.433 14 8.5V8C14 4.68629 11.3137 2 8 2ZM8 5C7.44772 5 7 5.44772 7 6C7 6.55228 7.44772 7 8 7C8.55228 7 9 6.55228 9 6C9 5.44772 8.55228 5 8 5Z"/>
            </svg>
          </button>
        </div>
        <div class="composer-actions-right">
          <button id="quickPromptButton" type="button" title="快速提示" class="hover-only">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4Z"/>
            </svg>
          </button>
          <button id="clearInputButton" type="button" title="清空输入" class="hover-only">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M8 0C3.58172 0 0 3.58172 0 8C0 12.4183 3.58172 16 8 16C12.4183 16 16 12.4183 16 8C16 3.58172 12.4183 0 8 0ZM8 14C4.68629 14 2 11.3137 2 8C2 4.68629 4.68629 2 8 2C11.3137 2 14 4.68629 14 8C14 11.3137 11.3137 14 8 14ZM10.2929 5.29289L8 7.58579L5.70711 5.29289L5.29289 5.70711L7.58579 8L5.29289 10.2929L5.70711 10.7071L8 8.41421L10.2929 10.7071L10.7071 10.2929L8.41421 8L10.7071 5.70711L10.2929 5.29289Z"/>
            </svg>
          </button>
          <button id="voiceInputButton" type="button" title="语音输入">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M8 1C6.89543 1 6 1.89543 6 3V8C6 9.10457 6.89543 10 8 10C9.10457 10 10 9.10457 10 8V3C10 1.89543 9.10457 1 8 1ZM4 8C4 5.79086 5.79086 4 8 4C10.2091 4 12 5.79086 12 8H10C10 6.89543 9.10457 6 8 6C6.89543 6 6 6.89543 6 8H4ZM8 12C6.89543 12 6 11.1046 6 10H4C4 12.2091 5.79086 14 8 14C10.2091 14 12 12.2091 12 10H10C10 11.1046 9.10457 12 8 12ZM8 15V13C10.7614 13 13 10.7614 13 8H11C11 9.65685 9.65685 11 8 11C6.34315 11 5 9.65685 5 8H3C3 10.7614 5.23858 13 8 13V15H8Z"/>
            </svg>
          </button>
          <button id="stopButton" type="button" title="停止生成" class="hidden">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M4 4H12V12H4V4Z"/>
            </svg>
          </button>
          <button id="sendButton" type="button" disabled class="send-button">
            <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
              <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z"/>
            </svg>
          </button>
        </div>
    </div>
    </footer>
  </div>
  <script id="graph-agent-boot" type="application/json">${JSON.stringify(initialState).replaceAll('<', '\\u003c')}</script>
  <script nonce="${nonce}" src="${htmlEscape(scriptUri)}"></script>
</body>
</html>`;
}
