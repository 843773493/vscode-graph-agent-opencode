
import path from 'node:path';
import * as vscode from 'vscode';

import { createSession, getSessionTraces, listAgents, listMessages, listSessions, sendMessage, streamJobEvents } from '../shared/api.js';
import { DEFAULT_AGENT_ID, DEFAULT_SESSION_TITLE } from '../shared/constants.js';
import { HostToWebviewMessageType, WebviewToHostMessageType } from '../shared/protocol.js';
import { renderSidebarHtml } from './html.js';

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

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isCompletedJobStatus(status) {
  return status === 'completed' || status === 'succeeded' || status === 'failed' || status === 'cancelled' || status === 'timed_out';
}

export class SidebarProvider {
  constructor(context, backendManager) {
    this.context = context;
    this.backendManager = backendManager;
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
    };
  }

  log(message) {
    this.backendManager.log(`[sidebar] ${message}`);
  }

  dispose() {
    this.disposed = true;
    this.webviewMessageDisposable?.dispose();
    this.webviewMessageDisposable = null;
    this.visibilityDisposable?.dispose();
    this.visibilityDisposable = null;
    for (const controller of this.jobStreams.values()) {
      controller.abort();
    }
    this.jobStreams.clear();
    this.view = null;
  }

  resolveWebviewView(webviewView, _context, _token) {
    this.webviewMessageDisposable?.dispose();
    this.visibilityDisposable?.dispose();

    this.view = webviewView;
    const webview = webviewView.webview;

    webview.options = {
      enableScripts: true,
      localResourceRoots: [this.context.extensionUri],
    };

    webview.html = renderSidebarHtml(webview, {
      nonce: getNonce(),
      scriptUri: webview.asWebviewUri(vscode.Uri.file(path.join(this.context.extensionUri.fsPath, 'src', 'webview', 'sidebarApp.js'))).toString(),
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
      if (!webviewView.visible) {
        return;
      }

      this.log('webview 重新可见，刷新最新状态');
      this.flushState();
    });

    webviewView.onDidDispose(() => {
      this.dispose();
    });

    this.flushState();

    void this.initialize().catch((error) => {
      this.postError(error);
    });
  }

  async initialize() {
    if (this.disposed) {
      return;
    }

    if (this.initializePromise) {
      return this.initializePromise;
    }

    this.initializePromise = (async () => {
      if (!this.view) {
        return;
      }

      if (!this.state.workspace) {
        this.state.workspace = workspaceFromVscode();
      }

      this.log(`webview 初始化完成，workspace=${this.state.workspace?.root_path ?? 'unknown'}`);
      this.syncState('正在预热后端...');

      await this.ensureBackendReady()
        .then(() => {
          this.log('后端预热完成');
          this.syncState('后端已就绪');
        })
        .catch((error) => {
          this.postError(error);
        });
    })().finally(() => {
      this.initializePromise = null;
    });

    return this.initializePromise;
  }

  async ensureBackendReady() {
    if (this.state.apiPort && this.state.currentSession) {
      this.log(`复用现有后端端口 ${this.state.apiPort}`);
      return { port: this.state.apiPort, workspace: this.state.workspace };
    }

    this.log('开始启动或探测后端');
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
  }

  async handleMessage(message) {
    if (!message || !message.type) {
      return;
    }

    if (message.type === WebviewToHostMessageType.debug) {
      this.log(`webview 调试: ${message.detail ?? '(empty)'}`);
      return;
    }

    if (message.type === WebviewToHostMessageType.ready) {
      this.log('收到 webview ready');
      this.syncState('Webview 已就绪');
      return;
    }

    if (message.type === WebviewToHostMessageType.refresh) {
      this.log('收到刷新请求');
      await this.ensureBackendReady();
      await this.reloadMessages();
      await this.reloadTraces();
      this.syncState('已刷新消息');
      return;
    }

    if (message.type === WebviewToHostMessageType.createSession) {
      this.log(`收到创建 session 请求: ${message.title ?? ''}`);
      await this.createNewSession(message.title);
      return;
    }

    if (message.type === WebviewToHostMessageType.selectSession) {
      this.log(`收到切换 session 请求: ${message.sessionId ?? ''}`);
      await this.selectSession(message.sessionId);
      return;
    }

    if (message.type === WebviewToHostMessageType.sendMessage) {
      this.log(`收到发送消息请求: ${String(message.content ?? '').slice(0, 80)}`);
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

  async sendCurrentMessage(content) {
    if (!this.state.currentSession) {
      await this.createNewSession(DEFAULT_SESSION_TITLE);
    }

    this.log(`开始处理发送消息，内容长度=${String(content ?? '').length}`);
    const ready = await this.ensureBackendReady();
    const port = ready.port;

    const defaultAgent = this.state.agents.find((agent) => agent.agent_id === DEFAULT_AGENT_ID) ?? this.state.agents[0];
    if (!defaultAgent) {
      throw new Error('未找到可用 agent');
    }

    const payload = {
      message: {
        role: 'user',
        content,
        attachments: [],
        metadata: {},
      },
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

    const accepted = await sendMessage(port, this.state.currentSession.session_id, payload);
    this.log(`后端已接受消息，job_id=${accepted?.job_id ?? '(none)'}`);

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
      void this.observeJobEvents(port, accepted.job_id, this.state.currentSession.session_id);
    }

    this.syncState('任务已提交，正在更新回复...');

    if (!accepted?.job_id) {
      void this.reloadMessages().then(() => {
        this.syncState('消息已提交到后端');
      });
    }
  }

  async observeJobEvents(port, jobId, sessionId) {
    const controller = new AbortController();
    this.jobStreams.set(jobId, controller);

    try {
      await streamJobEvents(port, jobId, {
        signal: controller.signal,
        onEvent: ({ eventType, payload }) => {
          this.postMessageToWebview({
            type: HostToWebviewMessageType.jobEvent,
            jobId,
            sessionId,
            eventType,
            payload,
          });

          if (['job_completed', 'job_failed', 'job_cancelled'].includes(eventType)) {
            this.state.activeJob = {
              ...(this.state.activeJob ?? {}),
              jobId,
              sessionId,
              status: eventType,
            };

            controller.abort();
            this.jobStreams.delete(jobId);
            void Promise.all([this.reloadMessages(), this.reloadTraces()]).then(() => {
              this.syncState(eventType === 'job_completed' ? '模型回复已更新' : `任务已结束: ${eventType}`);
            });
          }
        },
        onError: (error) => {
          this.postError(error);
        },
      });
    } catch (error) {
      if (!controller.signal.aborted) {
        this.postError(error);
      }
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

    if (!this.view) {
      return;
    }

    void this.view.webview.postMessage(this.lastStatePayload);
  }

  flushState() {
    if (!this.view || !this.lastStatePayload) {
      return;
    }

    void this.view.webview.postMessage(this.lastStatePayload);
  }

  postError(error) {
    this.lastStatus = '发生错误';
    this.lastStatePayload = null;

    if (!this.view) {
      this.log(`webview 错误: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }

    this.view.webview.postMessage({
      type: HostToWebviewMessageType.error,
      message: error instanceof Error ? error.message : String(error),
    });
  }
}
