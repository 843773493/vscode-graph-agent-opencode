import { t } from '../../../shared/core/i18n/index.js';
import { AbstractDebugAdapter } from '../../upstream/src/vs/workbench/contrib/debug/common/abstractDebugAdapter.js';
import type { IConfig, IDebugSession } from '../../upstream/src/vs/workbench/contrib/debug/common/debug.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { getGatewayToken, onGatewayTokenInvalidated, type HttpFailureContext } from '../../../web/src/api/http.js';
import { errorMessage } from '../../../web/src/utils/errorMessage.js';
import type { NodeDebugDebuggerContributionDTO, NodeDebugStartRequest, NodeDebugStateDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway, type ThreadOwner } from '../agentApi.js';
import { workspaceFileSource, workspaceFileUri } from '../workspaceFiles.js';

export interface PluginDebugLaunch {
  launchId: string;
  owner: Readonly<ThreadOwner>;
  debugger: Readonly<NodeDebugDebuggerContributionDTO>;
  rootUris: readonly string[];
  start: Readonly<NodeDebugStartRequest>;
}

export type PluginDebugConfiguration = IConfig & { pluginDebug: PluginDebugLaunch };

export function launchContext(config: IConfig): PluginDebugLaunch {
  const context = (config as Partial<PluginDebugConfiguration>).pluginDebug;
  if (!context || context.debugger.registration_id !== config.pluginDebuggerRegistration
    || context.owner.gatewayId !== context.debugger.gateway_id || context.owner.workspaceId !== context.debugger.workspace_id
    || context.start.debugger_registration_id !== context.debugger.registration_id
    || context.start.session_id !== context.owner.sessionId || context.start.thread_id !== context.owner.threadId) {
    throw new Error(t('workbench.debug.launchContextInvalid'));
  }
  return context;
}

/** 原断点模型可含多个目标；每个 session 只接收自己目标的源码。 */
export function acceptsPluginDebugSource(config: IConfig, uri: URI): boolean {
  if (!config.pluginDebuggerRegistration) return true;
  if (uri.scheme !== 'boxteam-file') return false;
  const context = launchContext(config), source = workspaceFileSource(uri);
  return source.gatewayId === context.owner.gatewayId && source.workspaceId === context.owner.workspaceId;
}

/** DAP 仅传输原 owner 的消息，序号、请求回调和事件排队复用原 adapter。 */
export class PluginDebugAdapter extends AbstractDebugAdapter {
  private socket: WebSocket | undefined;
  private stopped = false;
  private releaseToken: (() => void) | undefined;
  private readonly context: PluginDebugLaunch;

  constructor(session: IDebugSession) { super(); this.context = launchContext(session.configuration); }

  async startSession(): Promise<void> {
    const { owner, debugger: binding, start } = this.context;
    const state = await gateway.request<NodeDebugStateDTO>('/api/v1/debug/node/start',
      { method: 'POST', body: JSON.stringify(start) }, owner.workspaceKey);
    if (state.session_id !== owner.sessionId || state.thread_id !== owner.threadId || !state.run_id
      || state.debugger_binding?.registration_id !== binding.registration_id) throw new Error(t('workbench.debug.launchReceiptMismatch'));
    // 启动已提交而 UI 被关闭时保留真实 run，不重放也不自动停止。
    if (this.stopped) throw new Error(t('workbench.debug.viewClosed'));
    const token = await getGatewayToken(0);
    if (this.stopped) throw new Error(t('workbench.debug.viewClosed'));
    const url = new URL(`/api/v1/debug/node/runs/${encodeURIComponent(state.run_id)}/dap`, location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    for (const [key, value] of Object.entries({ token, gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
      session_id: owner.sessionId, thread_id: owner.threadId })) url.searchParams.set(key, value);
    const socket = this.socket = new WebSocket(url);
    this.releaseToken = onGatewayTokenInvalidated(0, () => socket.close(1000, 'Gateway 凭据已失效'));
    socket.addEventListener('message', event => {
      try {
        if (typeof event.data !== 'string') throw new Error(t('workbench.debug.jsonRequired'));
        const value: unknown = JSON.parse(event.data);
        this.acceptMessage(this.projectSources(value, true) as DebugProtocol.ProtocolMessage);
      } catch (error) {
        this._onError.fire(this.failure('websocket_receive', error instanceof Error ? error.message : String(error), error));
        socket.close(1002, 'DAP 消息无效');
      }
    });
    socket.addEventListener('close', event => {
      this.releaseToken?.(); this.releaseToken = undefined;
      if (!this.stopped) this._onError.fire(this.failure('websocket_closed', t('workbench.debug.connectionClosed', event.code, event.reason)));
      this._onExit.fire(null);
    }, { once: true });
    await new Promise<void>((resolve, reject) => {
      const finish = (error?: Error) => { clearTimeout(timer); socket.removeEventListener('open', open); socket.removeEventListener('close', close); socket.removeEventListener('error', failed); error ? reject(error) : resolve(); };
      const open = () => finish();
      const close = (event: CloseEvent) => finish(this.failure('websocket_connect', t('workbench.debug.connectionNotOpened', event.code, event.reason)));
      const failed = () => finish(this.failure('websocket_connect', t('workbench.debug.connectionFailed')));
      const timer = setTimeout(() => { finish(this.failure('websocket_connect', t('workbench.debug.connectionTimeout'))); socket.close(); }, 30_000);
      socket.addEventListener('open', open, { once: true }); socket.addEventListener('close', close, { once: true }); socket.addEventListener('error', failed, { once: true });
    });
  }

  private failure(stage: string, message: string, cause?: unknown): Error & { context: HttpFailureContext } {
    const { owner, debugger: binding } = this.context;
    const context: HttpFailureContext = { source: { component: 'workbench' }, stage,
      target: { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, session_id: owner.sessionId,
        thread_id: owner.threadId, debugger_registration_id: binding.registration_id } };
    // 浏览器不提供 WebSocket 握手状态和响应头，只标明已知边界，不推断 Gateway／Backend 谁故障。
    return Object.assign(new Error(errorMessage(message), { cause }), { context });
  }

  sendMessage(message: DebugProtocol.ProtocolMessage): void {
    if (this.stopped || this.socket?.readyState !== WebSocket.OPEN) throw this.failure('websocket_send', t('workbench.debug.connectionUnavailable'));
    this.socket.send(JSON.stringify(this.projectSources(message, false)));
  }

  private projectSources(value: unknown, incoming: boolean, source = false): unknown {
    if (Array.isArray(value)) return value.map(entry => this.projectSources(entry, incoming, source));
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      if (source && key === 'path' && typeof item === 'string') {
        if (incoming) {
          if (!item.startsWith('boxteam://')) throw new Error(t('workbench.debug.sourceUriRequired'));
          const root = this.context.rootUris.find(root => item === root || item.startsWith(root + '/'));
          if (!root) throw new Error(t('workbench.debug.sourceRootMismatch'));
          return [key, workspaceFileUri(this.context.owner, item, root).toString()];
        }
        const file = workspaceFileSource(URI.parse(item));
        if (file.gatewayId !== this.context.owner.gatewayId || file.workspaceId !== this.context.owner.workspaceId) throw new Error(t('workbench.debug.breakpointOwnerMismatch'));
        return [key, file.path];
      }
      return [key, this.projectSources(item, incoming, key === 'source' || key === 'sources')];
    }));
  }

  async stopSession(): Promise<void> {
    this.stopped = true;
    this.releaseToken?.(); this.releaseToken = undefined;
    const socket = this.socket;
    if (!socket || socket.readyState === WebSocket.CLOSED) return;
    await new Promise<void>((resolve, reject) => {
      const closed = () => { clearTimeout(timer); resolve(); };
      const timer = setTimeout(() => {
        socket.removeEventListener('close', closed);
        reject(this.failure('websocket_close', t('workbench.debug.closeTimeout')));
      }, 5_000);
      socket.addEventListener('close', closed, { once: true });
      socket.close(1000, '调试界面断开');
    });
  }

  override dispose(): void {
    this.stopped = true; this.releaseToken?.(); this.releaseToken = undefined;
    this.socket?.close(1000, '调试界面释放');
    super.dispose();
  }
}
