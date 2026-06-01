import fs from 'node:fs';
import path from 'node:path';
import * as vscode from 'vscode';

import { createSession, getSessionTraces, listAgents, listMessages, listSessions, sendMessage, streamJobEvents } from '../shared/api.js';
import { DEFAULT_AGENT_ID, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_TOKEN, DEFAULT_SESSION_TITLE } from '../shared/constants.js';
import { HostToWebviewMessageType, WebviewToHostMessageType } from '../shared/protocol.js';
import { renderSidebarHtml } from './html.js';

function getWebviewUiDistDir(extensionUri) {
  return path.join(extensionUri.fsPath, 'src', 'webview-ui', 'dist');
}

function getWebviewUiAssetUri(webview, extensionUri, assetFileName) {
  return webview.asWebviewUri(vscode.Uri.file(path.join(getWebviewUiDistDir(extensionUri), 'assets', assetFileName))).toString();
}

function getNonce() {
  return String(Date.now()) + String(Math.random()).slice(2);
}

function workspaceSummary(workspace) {
  return {
    workspaceRoot: workspace?.root_path ?? '',
    workspaceName: workspace?.name ?? 'workspace',
  };
}

function workspaceFromVscode() {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    return null;
  }

  return {
    root_path: folder.uri.fsPath,
    name: folder.name || 'workspace',
  };
}

function ensureDirSync(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

function writeFileOverwritten(filePath, content) {
  ensureDirSync(path.dirname(filePath));
  fs.writeFileSync(filePath, content, { encoding: 'utf8' });
}

function getUserLogRoot() {
  return path.join(process.env.USERPROFILE ?? require('node:os').homedir(), '.boxteams', 'logs');
}

function getWebviewPreviewPath() {
  return path.join(getUserLogRoot(), 'ui', 'snapshot.html');
}

function getRuntimeWebviewUiLogPath() {
  return path.join(getUserLogRoot(), 'vscode_runtime_webview_ui.log');
}

function formatLogTimestamp() {
  return new Date().toISOString();
}

function requestBackendJson(port, message) {
  if (!message?.path) {
    throw new Error('API请求缺少 path');
  }

  const normalizedPath = message.path.startsWith('/') ? message.path : `/${message.path}`;
  const url = `http://${DEFAULT_BACKEND_HOST}:${port}/api/v1${normalizedPath}`;
  return fetch(url, {
    method: message.method ?? 'GET',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(message.headers ?? {}),
    },
    body: message.body === undefined ? undefined : (typeof message.body === 'string' ? message.body : JSON.stringify(message.body)),
  }).then(async (response) => {
    const responseText = await response.text();
    if (!response.ok) {
      throw new Error(`后端请求失败 ${response.status}: ${responseText}`);
    }
    return responseText ? JSON.parse(responseText) : null;
  });
}

export class SidebarProvider {
  constructor(context, backendManager, options = {}) {
    this.context = context;
    this.backendManager = backendManager;
    this.shellMode = Boolean(options.shellMode || process.env.GRAPH_AGENT_UI_SHELL_DEBUG === '1');
    this.view = null;
    this.webviewMessageDisposable = null;
    this.visibilityDisposable = null;
    this.disposed = false;
    this.initializePromise = null;
    this.jobStreams = new Map();
    this.lastStatus = '准备就绪';
    this.lastStatePayload = null;
    this.state = {
      apiPort: null,
      workspace: null,
      sessions: [],
      currentSession: null,
      messages: [],
      backendMessages: [],
      localMessages: [],
      agents: [],
      traceEvents: [],
      activeJob: null,
    };
  }

  log(message) {
    this.backendManager.log(`[sidebar] ${message}`);
  }

  dispose() {
    this.disposed = true;
    this.webviewMessageDisposable?.dispose();
    this.visibilityDisposable?.dispose();
    for (const controller of this.jobStreams.values()) {
      controller.abort();
    }
    this.jobStreams.clear();
    this.view = null;
  }

  async createShellDebugPanel() {
    const panel = vscode.window.createWebviewPanel(
      'vscode-graph-agent-shell-debug',
      'Graph Agent UI Shell Debug',
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [this.context.extensionUri] },
    );

    panel.webview.html = await renderSidebarHtml(panel.webview, {
      log: (message) => this.log(message),
      nonce: getNonce(),
      shellMode: true,
      apiPort: this.state.apiPort,
      workspaceRoot: this.state.workspace?.root_path ?? '',
      workspaceName: this.state.workspace?.name ?? 'workspace',
      sessions: [],
      session: null,
      messages: [],
      traceEvents: [],
      activeJob: null,
    });

    return panel;
  }

  async resolveWebviewView(webviewView, _context, _token) {
    this.webviewMessageDisposable?.dispose();
    this.visibilityDisposable?.dispose();

    this.view = webviewView;
    const webview = webviewView.webview;

    webview.options = { enableScripts: true, localResourceRoots: [this.context.extensionUri] };
    webview.html = await renderSidebarHtml(webview, {
      log: (message) => this.log(message),
      nonce: getNonce(),
      shellMode: this.shellMode,
      distCssUri: this.shellMode ? '' : getWebviewUiAssetUri(webview, this.context.extensionUri, 'App.css'),
      distJsUri: this.shellMode ? '' : getWebviewUiAssetUri(webview, this.context.extensionUri, 'main.js'),
      apiPort: this.state.apiPort,
      workspaceRoot: this.state.workspace?.root_path ?? '',
      workspaceName: this.state.workspace?.name ?? 'workspace',
      sessions: this.state.sessions,
      session: this.state.currentSession,
      messages: this.state.messages,
      traceEvents: this.state.traceEvents,
      activeJob: this.state.activeJob ?? null,
    });

    this.webviewMessageDisposable = webview.onDidReceiveMessage(async (message) => {
      try {
        await this.handleMessage(message);
      } catch (error) {
        this.postError(error);
      }
    });

    this.visibilityDisposable = webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) {
        this.flushState();
      }
    });

    webviewView.onDidDispose(() => this.dispose());
    this.flushState();
    void this.initialize().catch((error) => this.postError(error));
  }

  async initialize() {
    if (this.disposed || this.initializePromise) {
      return this.initializePromise;
    }

    this.initializePromise = (async () => {
      if (!this.view) {
        return;
      }

      if (!this.state.workspace) {
        this.state.workspace = workspaceFromVscode();
      }

      this.syncState('正在预热后端...');
      await this.ensureBackendReady();
      this.syncState('后端已就绪');
    })().finally(() => {
      this.initializePromise = null;
    });

    return this.initializePromise;
  }

  async ensureBackendReady() {
    if (this.state.apiPort && this.state.currentSession) {
      return { port: this.state.apiPort, workspace: this.state.workspace };
    }

    const ready = await this.backendManager.ensureStarted();
    this.state.apiPort = ready.port;
    this.state.workspace = ready.workspace;
    this.state.agents = await listAgents(ready.port);
    this.state.sessions = await listSessions(ready.port).then((page) => page.items ?? []);
    this.state.currentSession = this.state.sessions[0] ?? null;

    if (!this.state.currentSession) {
      this.state.currentSession = await createSession(ready.port, DEFAULT_SESSION_TITLE);
      this.state.sessions = [this.state.currentSession, ...this.state.sessions];
    }

    await this.reloadMessages();
    await this.reloadTraces();
    this.syncState('初始化完成');
    return ready;
  }

  async handleMessage(message) {
    if (!message?.type) {
      return;
    }

    if (message.type === 'writeWebviewPreview') {
      const targetPath = getWebviewPreviewPath();
      this.log(`[${formatLogTimestamp()}] 收到 webview preview 写入请求: ${targetPath}`);
      writeFileOverwritten(targetPath, String(message.content ?? ''));
      return;
    }

    if (message.type === 'writeRuntimeWebviewUiLog') {
      const targetPath = getRuntimeWebviewUiLogPath();
      this.log(`收到 webview runtime 日志写入请求: ${targetPath}`);
      writeFileOverwritten(targetPath, String(message.content ?? ''));
      return;
    }

    if (message.type === 'api_request') {
      await this.handleApiRequest(message);
      return;
    }

    if (message.type === WebviewToHostMessageType.refresh) {
      await this.ensureBackendReady();
      await this.reloadMessages();
      await this.reloadTraces();
      this.syncState('已刷新消息');
      return;
    }

    if (message.type === WebviewToHostMessageType.createSession) {
      await this.createNewSession(message.title);
      return;
    }

    if (message.type === WebviewToHostMessageType.selectSession) {
      await this.selectSession(message.sessionId);
      return;
    }

    if (message.type === 'updateSession') {
      await this.updateSessionAgent(message.sessionId, message.data?.agent_id);
      return;
    }

    if (message.type === WebviewToHostMessageType.sendMessage) {
      await this.sendCurrentMessage(message.content);
    }
  }

  async createNewSession(title) {
    const port = (await this.ensureBackendReady()).port;
    const session = await createSession(port, title || DEFAULT_SESSION_TITLE);
    this.state.currentSession = session;
    this.state.sessions = [session, ...this.state.sessions.filter((item) => item.session_id !== session.session_id)];
    this.state.backendMessages = [];
    this.state.localMessages = [];
    this.state.messages = [];
    this.state.traceEvents = [];
    this.state.activeJob = null;
    this.syncState('已创建新 session');
  }

  async selectSession(sessionId) {
    if (!sessionId) {
      return;
    }

    const selected = this.state.sessions.find((session) => session.session_id === sessionId);
    if (!selected) {
      throw new Error(`未找到 session: ${sessionId}`);
    }

    this.state.currentSession = selected;
    this.state.activeJob = null;
    await this.reloadMessages();
    await this.reloadTraces();
    this.syncState('已切换 session');
  }

  async updateSessionAgent(sessionId, agentId) {
    if (!sessionId || !agentId) {
      return;
    }

    const { port } = await this.ensureBackendReady();
    const url = `http://${DEFAULT_BACKEND_HOST}:${port}/api/v1/sessions/${sessionId}`;
    const response = await fetch(url, {
      method: 'PATCH',
      headers: {
        accept: 'application/json',
        'content-type': 'application/json',
        'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      },
      body: JSON.stringify({ agent_id: agentId }),
    });

    if (!response.ok) {
      throw new Error(`PATCH ${url} failed: ${response.status} ${await response.text()}`);
    }

    const result = await response.json();
    const updatedSession = result.data ?? result;
    const sessionIndex = this.state.sessions.findIndex((s) => s.session_id === sessionId);
    if (sessionIndex !== -1) {
      this.state.sessions[sessionIndex] = updatedSession;
    } else {
      this.state.sessions.push(updatedSession);
    }

    if (this.state.currentSession?.session_id === sessionId) {
      this.state.currentSession = updatedSession;
    }

    this.syncState(`已切换Agent为 ${agentId}`);
  }

  async handleApiRequest(message) {
    const requestId = message.request_id ?? '';
    const { port } = await this.ensureBackendReady();
    const data = await requestBackendJson(port, message);
    this.postMessageToWebview({ type: 'api_response', request_id: requestId, ok: true, data });
  }

  mergeMessages() {
    const seenIds = new Set();
    const messages = [];
    for (const message of [...this.state.backendMessages, ...this.state.localMessages]) {
      const key = message.message_id ?? `${message.role}:${message.content}`;
      if (seenIds.has(key)) {
        continue;
      }
      seenIds.add(key);
      messages.push(message);
    }
    this.state.messages = messages;
  }

  async reloadMessages() {
    if (!this.state.currentSession || !this.state.apiPort) {
      this.state.backendMessages = [];
      this.mergeMessages();
      return;
    }

    const page = await listMessages(this.state.apiPort, this.state.currentSession.session_id);
    this.state.backendMessages = page.items ?? [];
    this.mergeMessages();
  }

  async reloadTraces() {
    if (!this.state.currentSession || !this.state.apiPort) {
      this.state.traceEvents = [];
      return;
    }

    this.state.traceEvents = await getSessionTraces(this.state.apiPort, this.state.currentSession.session_id);
  }

  async reloadSessions() {
    if (!this.state.apiPort) {
      return;
    }

    const page = await listSessions(this.state.apiPort);
    this.state.sessions = page.items ?? [];

    if (!this.state.currentSession && this.state.sessions.length > 0) {
      this.state.currentSession = this.state.sessions[0];
    } else if (this.state.currentSession) {
      const refreshed = this.state.sessions.find((s) => s.session_id === this.state.currentSession.session_id);
      if (refreshed) {
        this.state.currentSession = refreshed;
      }
    }
  }

  async sendCurrentMessage(content) {
    if (!this.state.currentSession) {
      await this.createNewSession(DEFAULT_SESSION_TITLE);
    }

    const ready = await this.ensureBackendReady();
    const defaultAgent = this.state.agents.find((agent) => agent.agent_id === DEFAULT_AGENT_ID) ?? this.state.agents[0];
    if (!defaultAgent) {
      throw new Error('未找到可用 agent');
    }

    const payload = {
      message: { role: 'user', content, attachments: [], metadata: {} },
      run: {
        mode: 'single_agent',
        agent_id: defaultAgent.agent_id,
        response_mode: 'stream',
        async: true,
        max_steps: 20,
        timeout_seconds: 600,
        context: {
          workspace_root: this.state.workspace?.root_path ?? '',
          workspace_name: this.state.workspace?.name ?? '',
        },
      },
    };

    const accepted = await sendMessage(ready.port, this.state.currentSession.session_id, payload);
    this.state.activeJob = accepted?.job_id
      ? {
          jobId: accepted.job_id,
          sessionId: this.state.currentSession.session_id,
          status: 'running',
          messageId: accepted.message_id ?? null,
          content,
        }
      : null;

    if (accepted?.job_id) {
      this.postMessageToWebview({
        type: HostToWebviewMessageType.messageAccepted,
        jobId: accepted.job_id,
        sessionId: this.state.currentSession.session_id,
        messageId: accepted.message_id ?? null,
        content,
      });
      void this.observeJobEvents(ready.port, accepted.job_id, this.state.currentSession.session_id);
    }

    this.syncState('任务已提交，正在更新回复...');
  }

  async observeJobEvents(port, jobId, sessionId) {
    const controller = new AbortController();
    this.jobStreams.set(jobId, controller);

    try {
      await streamJobEvents(port, jobId, {
        signal: controller.signal,
        onEvent: ({ eventType, payload }) => {
          this.postMessageToWebview({ type: HostToWebviewMessageType.jobEvent, jobId, sessionId, eventType, payload });
          if (['job_completed', 'job_failed', 'job_cancelled'].includes(eventType)) {
            this.state.activeJob = { ...(this.state.activeJob ?? {}), jobId, sessionId, status: eventType };
            controller.abort();
            this.jobStreams.delete(jobId);
            void Promise.all([this.reloadMessages(), this.reloadTraces()]);
          }
        },
        onError: (error) => this.postError(error),
      });
    } finally {
      this.jobStreams.delete(jobId);
    }
  }

  postMessageToWebview(message) {
    if (!this.view) {
      return;
    }
    void this.view.webview.postMessage(message);
  }

  syncState(status) {
    this.lastStatus = status;
    const { workspaceRoot, workspaceName } = workspaceSummary(this.state.workspace);
    this.lastStatePayload = {
      type: HostToWebviewMessageType.state,
      status,
      state: {
        workspaceRoot,
        workspaceName,
        sessions: this.state.sessions,
        session: this.state.currentSession,
        messages: this.state.messages,
        traceEvents: this.state.traceEvents,
        activeJob: this.state.activeJob,
      },
    };
    if (this.view) {
      void this.view.webview.postMessage(this.lastStatePayload);
    }
  }

  flushState() {
    if (this.view && this.lastStatePayload) {
      void this.view.webview.postMessage(this.lastStatePayload);
    }
  }

  postError(error) {
    this.lastStatus = '发生错误';
    this.lastStatePayload = null;
    if (!this.view) {
      this.log(`webview 错误: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    this.view.webview.postMessage({ type: HostToWebviewMessageType.error, message: error instanceof Error ? error.message : String(error) });
  }
}
