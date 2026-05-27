
import path from 'node:path';
import * as vscode from 'vscode';

import { createSession, getSessionTraces, listAgents, listMessages, listSessions, sendMessage, streamJobEvents } from '../shared/api.js';
import { DEFAULT_AGENT_ID, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_TOKEN, DEFAULT_SESSION_TITLE } from '../shared/constants.js';
import { HostToWebviewMessageType, WebviewToHostMessageType } from '../shared/protocol.js';
import { renderSidebarHtml } from './html.js';

function getWebviewUiDistDir(extensionUri) {
  return path.join(extensionUri.fsPath, 'src', 'webview-ui', 'dist');
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

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isCompletedJobStatus(status) {
  return status === 'completed' || status === 'succeeded' || status === 'failed' || status === 'cancelled' || status === 'timed_out';
}

function buildBackendUrl(port, path) {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  return `http://${DEFAULT_BACKEND_HOST}:${port}${normalizedPath}`;
}

async function requestBackendJson(port, message) {
  if (!message?.path) {
    throw new Error('API请求缺少 path');
  }

  const url = buildBackendUrl(port, message.path);
  const response = await fetch(url, {
    method: message.method ?? 'GET',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
      'X-Local-Token': DEFAULT_BACKEND_TOKEN,
      ...(message.headers ?? {}),
    },
    body: message.body === undefined ? undefined : (typeof message.body === 'string' ? message.body : JSON.stringify(message.body)),
  });

  const responseText = await response.text();
  if (!response.ok) {
    throw new Error(`后端请求失败 ${response.status}: ${responseText}`);
  }

  if (!responseText) {
    return null;
  }

  try {
    return JSON.parse(responseText);
  } catch {
    return responseText;
  }
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
    };
  }

  async createShellDebugPanel() {
    if (!this.shellMode) {
      this.log('shellMode 未开启，按纯壳调试面板逻辑打开');
    }
    const panel = vscode.window.createWebviewPanel(
      'vscode-graph-agent-shell-debug',
      'Graph Agent UI Shell Debug',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [this.context.extensionUri],
      },
    );

    panel.webview.html = renderSidebarHtml(panel.webview, {
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
      shellMode: this.shellMode,
      distCssUri: this.shellMode ? '' : webview.asWebviewUri(vscode.Uri.file(path.join(getWebviewUiDistDir(this.context.extensionUri), 'assets', 'index.css'))).toString(),
      distJsUri: this.shellMode ? '' : webview.asWebviewUri(vscode.Uri.file(path.join(getWebviewUiDistDir(this.context.extensionUri), 'assets', 'index.js'))).toString(),
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

    if (message.type === 'api_request') {
      await this.handleApiRequest(message);
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

     if (message.type === 'updateSession') {
       this.log(`收到更新 session 请求: sessionId=${message.sessionId}, agentId=${message.data?.agent_id ?? 'unknown'}`);
       await this.updateSessionAgent(message.sessionId, message.data?.agent_id);
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

    async updateSessionAgent(sessionId, agentId) {
      if (!sessionId || !agentId) {
        this.log(`更新 session agent 失败: sessionId=${sessionId}, agentId=${agentId}`);
        return;
      }

      this.log(`开始更新 session ${sessionId} agent 为 ${agentId}`);

      try {
        // 1. 调用后端 PATCH API 更新
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
          const errorText = await response.text();
          throw new Error(`PATCH ${url} failed: ${response.status} ${errorText}`);
        }

        const result = await response.json();
        this.log(`后端返回: ${JSON.stringify(result)}`);

        // 2. 以后端返回的完整 session 数据更新前端状态
        const updatedSession = result.data ?? result;
        const sessionIndex = this.state.sessions.findIndex((s) => s.session_id === sessionId);

        if (sessionIndex !== -1) {
          this.state.sessions[sessionIndex] = updatedSession;
        } else {
          this.log(`警告: sessions列表中未找到session ${sessionId}，添加到列表`);
          this.state.sessions.push(updatedSession);
        }

        if (this.state.currentSession?.session_id === sessionId) {
          this.state.currentSession = updatedSession;
        }

        this.syncState(`已切换Agent为 ${agentId}`);
        this.log(`session ${sessionId} agent 更新成功: ${agentId}`);

      } catch (error) {
        this.log(`更新失败: ${error.message}`);
        // 失败时重新拉取，确保前后端一致
        await this.reloadSessions();
        this.postError(new Error(`切换Agent失败: ${error.message}`));
      }
    }

   async handleApiRequest(message) {
    const requestId = message.request_id ?? '';
    this.log(`收到 webview API 请求: ${message.method ?? 'GET'} ${message.path ?? '(empty)'} request_id=${requestId}`);

    try {
      const { port } = await this.ensureBackendReady();
      const data = await requestBackendJson(port, message);
      this.postMessageToWebview({
        type: 'api_response',
        request_id: requestId,
        ok: true,
        data,
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      this.log(`API请求失败: ${errorMessage}`);
      this.postMessageToWebview({
        type: 'api_response',
        request_id: requestId,
        ok: false,
        error: errorMessage,
      });
    }
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
       const refreshed = this.state.sessions.find(
         (s) => s.session_id === this.state.currentSession.session_id,
       );
       if (refreshed) {
         this.state.currentSession = refreshed;
       }
     }

     this.log(`session列表已刷新，共${this.state.sessions.length}个session`);
   }

  async sendCurrentMessage(content) {
    this.log(`========== 开始发送消息 ==========`);
    this.log(`消息内容: ${String(content ?? '').slice(0, 100)}`);

    if (!this.state.currentSession) {
      this.log('错误: 当前无活动session，创建新session');
      await this.createNewSession(DEFAULT_SESSION_TITLE);
    }

    this.log(`当前session: ${this.state.currentSession?.session_id}`);
    this.log(`当前端口: ${this.state.apiPort}`);

    const ready = await this.ensureBackendReady();
    const port = ready.port;
    this.log(`后端就绪确认成功，端口=${port}`);

    const defaultAgent = this.state.agents.find((agent) => agent.agent_id === DEFAULT_AGENT_ID) ?? this.state.agents[0];
    if (!defaultAgent) {
      const error = new Error('未找到可用 agent');
      this.log(`错误: ${error.message}`);
      throw error;
    }
    this.log(`使用agent: ${defaultAgent.agent_id}`);

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

    this.log(`发送API请求到: POST /api/v1/sessions/${this.state.currentSession.session_id}/messages`);
    this.log(`Payload: ${JSON.stringify(payload).slice(0, 200)}...`);

    try {
      const accepted = await sendMessage(port, this.state.currentSession.session_id, payload);
      this.log(`✓ API响应成功: job_id=${accepted?.job_id ?? '(none)'}, message_id=${accepted?.message_id ?? '(none)'}, status=${accepted?.status}`);

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
        this.log(`✓ 已通知webview消息已接受，开始监听job事件: job_id=${accepted.job_id}`);
        void this.observeJobEvents(port, accepted.job_id, this.state.currentSession.session_id);
      } else {
        this.log('⚠ 后端返回的job_id为空，非流式响应模式');
      }

      this.syncState('任务已提交，正在更新回复...');

      if (!accepted?.job_id) {
        void this.reloadMessages().then(() => {
          this.syncState('消息已提交到后端');
        });
      }
    } catch (error) {
      this.log(`✗ API请求失败: ${error.message}`);
      this.log(`错误堆栈: ${error.stack?.split('\n')[0] || 'no stack'}`);
      throw error; // 抛出给外层，会被handleMessage捕获并显示
    }

    this.log(`========== 发送消息流程结束 ==========`);
  }

  async observeJobEvents(port, jobId, sessionId) {
    this.log(`>> 开始监听job事件: job_id=${jobId}, session_id=${sessionId}, port=${port}`);
    const controller = new AbortController();
    this.jobStreams.set(jobId, controller);

    try {
      this.log(`>> 连接SSE流: /api/v1/jobs/${jobId}/events/stream`);
      await streamJobEvents(port, jobId, {
        signal: controller.signal,
        onEvent: ({ eventType, payload }) => {
          this.log(`>> 收到job事件: type=${eventType}, payload=${JSON.stringify(payload).slice(0, 100)}`);
          this.postMessageToWebview({
            type: HostToWebviewMessageType.jobEvent,
            jobId,
            sessionId,
            eventType,
            payload,
          });

          if (['job_completed', 'job_failed', 'job_cancelled'].includes(eventType)) {
            this.log(`>> Job结束: ${eventType}`);
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
          const errorMsg = `SSE流错误: ${error.message}`;
          this.log(`✗ ${errorMsg}`);
          this.postError(new Error(errorMsg));
        },
      });
      this.log(`>> SSE流正常结束`);
    } catch (error) {
      if (!controller.signal.aborted) {
        const errorMsg = `监听job事件失败: ${error.message}`;
        this.log(`✗ ${errorMsg}`);
        this.postError(new Error(errorMsg));
      }
    } finally {
      this.jobStreams.delete(jobId);
      this.log(`>> 清理job监听: job_id=${jobId}`);
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
