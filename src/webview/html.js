const htmlEscape = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

export function renderSidebarHtml(webview, boot) {
  const cspSource = webview.cspSource;
  const nonce = boot.nonce;
  const shellMode = boot.shellMode ?? false;
  const distCssUri = boot.distCssUri ?? '';
  const distJsUri = boot.distJsUri ?? '';
  const initialState = {
    workspaceRoot: boot.workspaceRoot ?? '',
    workspaceName: boot.workspaceName ?? 'workspace',
    sessions: Array.isArray(boot.sessions) ? boot.sessions : [],
    session: boot.session ?? null,
    messages: Array.isArray(boot.messages) ? boot.messages : [],
    traceEvents: Array.isArray(boot.traceEvents) ? boot.traceEvents : [],
    activeJob: boot.activeJob ?? null,
    status: boot.status ?? '准备就绪',
    expandDetails: boot.expandDetails ?? true,
    historyPanelOpen: boot.historyPanelOpen ?? false,
  };

  if (shellMode) {
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; img-src ${cspSource} https: data:; style-src ${cspSource}; script-src ${cspSource} 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BoxTeam Shell Debug</title>
  <style>
    body {
      margin: 0;
      width: 100vw;
      height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--vscode-editor-background);
      color: var(--vscode-editor-foreground);
      font-family: var(--vscode-font-family);
    }
    .shell-panel {
      box-sizing: border-box;
      width: min(760px, calc(100vw - 32px));
      padding: 24px;
      border: 1px solid var(--vscode-panel-border);
      border-radius: 12px;
      background: rgba(127, 127, 127, 0.06);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.16);
    }
    .shell-title {
      margin: 0 0 12px;
      font-size: 18px;
      font-weight: 700;
    }
    .shell-text {
      margin: 0;
      line-height: 1.7;
      color: var(--vscode-descriptionForeground);
      white-space: pre-wrap;
      word-break: break-word;
    }
    .shell-badge {
      display: inline-flex;
      margin-top: 14px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="shell-panel">
    <h1 class="shell-title">UI 调试纯壳模式</h1>
    <p class="shell-text">这是一个不注入 React 前端的最小 Webview 壳。
用于检查 Webview 容器、CSP、基础样式和扩展侧入口是否正常。

如果你看到这一页，说明纯壳模式已经成功启动。</p>
    <div class="shell-badge">shellMode = true</div>
  </div>
</body>
</html>`;
  }

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; img-src ${cspSource} https: data:; style-src ${cspSource}; script-src ${cspSource} 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>BoxTeam</title>
  <link rel="stylesheet" href="${htmlEscape(distCssUri)}" />
  <script id="graph-agent-boot" type="application/json">${JSON.stringify(initialState).replaceAll('<', '\u003c')}</script>
  <script nonce="${nonce}" type="module" src="${htmlEscape(distJsUri)}"></script>
</head>
<body>
  <div id="root"></div>
</body>
</html>`;
}
