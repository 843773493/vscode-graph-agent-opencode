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
  <script id="graph-agent-boot" type="application/json">${JSON.stringify(initialState).replaceAll('<', '\\u003c')}</script>
  <script nonce="${nonce}" type="module" src="${htmlEscape(distJsUri)}"></script>
</head>
<body>
  <div id="root"></div>
</body>
</html>`;
}
