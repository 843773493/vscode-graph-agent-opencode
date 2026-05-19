const htmlEscape = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

export function renderSidebarHtml(webview, boot) {
  const cspSource = webview.cspSource;
  const nonce = boot.nonce;
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
  <div id="root">
    <div class="sidebar-shell">
      <header class="sidebar-header">
        <div class="sidebar-header-main">
          <div class="sidebar-brand">BoxTeam</div>
          <div id="workspace" class="sidebar-workspace">workspace</div>
        </div>
        <div id="workspaceStatus" class="sidebar-workspace-status">准备就绪</div>
      </header>

      <section class="toolbar">
        <button id="newSessionButton" type="button">新建会话</button>
        <button id="refreshButton" type="button">刷新</button>
        <input id="expandDetailsToggle" type="checkbox" hidden />
        <button id="attachButton" type="button">附件</button>
        <button id="mentionButton" type="button">@</button>
        <button id="quickPromptButton" type="button">提示词</button>
        <button id="clearInputButton" type="button">清空</button>
        <button id="voiceInputButton" type="button">语音</button>
        <button id="stopButton" type="button">停止</button>
        <button id="pinButton" type="button">置顶</button>
        <button id="historyButton" type="button">历史</button>
        <button id="viewToggleButton" type="button">视图</button>
        <button id="contextButton" type="button">上下文</button>
        <button id="helpButton" type="button">帮助</button>
        <button id="settingsButton" type="button">设置</button>
        <button id="autoContinueButton" type="button">托管</button>
        <button id="agentSelectButton" type="button"><span>default</span></button>
        <span id="agentNameDisplay" hidden></span>
      </section>

      <main class="sidebar-main">
        <aside id="historyPanel" class="history-panel">
          <div id="sessionList" class="session-list"></div>
        </aside>
        <section id="chatPanel" class="chat-panel">
          <div id="turnList" class="turn-list"></div>
          <div class="composer">
            <textarea id="input" rows="4" placeholder="输入内容开始聊天"></textarea>
            <div class="composer-actions">
              <button id="sendButton" type="button">发送</button>
            </div>
          </div>
        </section>
      </main>

      <footer class="sidebar-footer">
        <div id="status" class="status-line">准备就绪</div>
      </footer>
    </div>
  </div>
</body>
</html>`;
}
