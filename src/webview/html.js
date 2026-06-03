import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const htmlEscape = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;');

const templateCache = new Map();

async function loadTemplate(templatePath) {
  const cached = templateCache.get(templatePath);
  if (cached) {
    return cached;
  }

  const content = await fs.readFile(templatePath, 'utf8');
  templateCache.set(templatePath, content);
  return content;
}

function injectTemplate(template, replacements) {
  return template.replace(/\{\{([A-Z0-9_]+)\}\}/g, (match, key) => {
    if (!Object.prototype.hasOwnProperty.call(replacements, key)) {
      throw new Error(`HTML 模板缺少占位符 ${key}`);
    }

    return String(replacements[key]);
  });
}

async function renderTemplate(templatePath, replacements) {
  const template = await loadTemplate(templatePath);
  return injectTemplate(template, replacements);
}

export async function renderSidebarHtml(webview, boot) {
  const cspSource = webview.cspSource;
  const nonce = boot.nonce;
  const shellMode = boot.shellMode ?? false;
  const distCssUri = boot.distCssUri ?? '';
  const distJsUri = boot.distJsUri ?? '';
  const initialState = {
    apiPort: boot.apiPort ?? null,
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

  const templateFile = shellMode ? 'shell.html' : 'main.html';
  const templatePath = path.join(path.dirname(fileURLToPath(import.meta.url)), templateFile);
  if (typeof boot.log === 'function') {
    boot.log(`renderSidebarHtml: templateFile=${templateFile}, templatePath=${templatePath}, shellMode=${shellMode}, distCssUri=${distCssUri || '(空)'}, distJsUri=${distJsUri || '(空)'}, cspSource=${cspSource || '(空)'}`);
  }

  return renderTemplate(templatePath, {
    CSP_SOURCE: cspSource,
    NONCE: nonce,
    DIST_CSS_URI: htmlEscape(distCssUri),
    DIST_JS_URI: htmlEscape(distJsUri),
    INITIAL_STATE: JSON.stringify(initialState).replaceAll('<', '\u003c'),
  });
}
