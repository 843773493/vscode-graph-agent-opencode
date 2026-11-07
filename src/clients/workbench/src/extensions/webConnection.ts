import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { getGatewayToken } from '../../../web/src/api/http.js';
import type { ExtensionWebHostConnectionDTO, ExtensionWebHostOpenRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { ITargetWebHostConnection } from './targetContext.js';

/** 原 view 持有唯一资格通道；Registry 的包位置始终引用这一代冻结发布。 */
export class TargetWebHostConnection extends Disposable implements ITargetWebHostConnection {
  private readonly cancellation = this._register(new CancellationTokenSource());
  private readonly closed = this._register(new Emitter<string>());
  readonly onDidClose = this.closed.event;
  private value: ExtensionWebHostConnectionDTO | undefined;
  private closing = false;
  constructor(readonly request: ExtensionWebHostOpenRequest) { super(); }

  get connection(): ExtensionWebHostConnectionDTO {
    if (this.closing || !this.value) throw new Error('目标 Web 宿主资格已关闭；请重新载入窗口取得新发布');
    return this.value;
  }

  async connect(): Promise<void> {
    const token = await raceCancellationError(getGatewayToken(0), this.cancellation.token);
    if (this.closing) throw new CancellationError();
    const url = new URL('/api/gateway/extension-web-hosts/channel', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', token);
    const socket = new WebSocket(url);
    this._register(toDisposable(() => socket.close()));
    await new Promise<void>((resolve, reject) => {
      const listeners = new DisposableStore();
      const finish = (error?: unknown) => { clearTimeout(timer); listeners.dispose(); error ? reject(error) : resolve(); };
      const timer = setTimeout(() => finish(new Error('等待目标 Web 宿主资格超时')), 30_000);
      listeners.add(this.cancellation.token.onCancellationRequested(() => finish(new CancellationError())));
      const onOpen = () => socket.send(JSON.stringify(this.request));
      const onClose = (event: CloseEvent) => finish(new Error('目标 Web 宿主资格返回前关闭：' + event.code + ' ' + event.reason));
      const onMessage = (event: MessageEvent<unknown>) => {
        try {
          if (typeof event.data !== 'string') throw new Error('目标 Web 宿主资格不是 JSON 文本');
          const value = JSON.parse(event.data) as ExtensionWebHostConnectionDTO;
          const request = this.request;
          if (value.protocol_version !== 1 || value.gateway_id !== request.gateway_id || value.backend_id !== request.backend_id
            || value.view_id !== request.view_id || value.profile_id !== request.profile_id
            || JSON.stringify(value.workspace_ids) !== JSON.stringify(request.workspace_ids)
            || JSON.stringify(value.extension_keys) !== JSON.stringify(request.extension_keys)
            || value.inventory?.profile_id !== request.profile_id
            || JSON.stringify(value.inventory?.extensions.map(entry => entry.extension_key)) !== JSON.stringify(request.extension_keys)
            || !value.client_id || !value.generation || !value.request_id || !value.context?.remote_settings
            || !value.context.workspace?.id || !value.context.workspace.name
            || !['linux', 'windows', 'macos'].includes(value.context.os)
            || JSON.stringify(value.context.workspace.folders.map(folder => folder.workspace_id)) !== JSON.stringify(request.workspace_ids)) {
            throw new Error('目标 Web 宿主资格与冻结目标、profile、视图或扩展集合不匹配');
          }
          this.value = value;
          socket.addEventListener('close', event => {
            if (!this.closing) { this.closing = true; this.closed.fire(event.code + ' ' + event.reason); }
          }, { once: true });
          finish();
        } catch (error) { finish(error); }
      };
      socket.addEventListener('open', onOpen, { once: true });
      socket.addEventListener('message', onMessage);
      socket.addEventListener('close', onClose, { once: true });
      listeners.add(toDisposable(() => {
        socket.removeEventListener('open', onOpen); socket.removeEventListener('message', onMessage); socket.removeEventListener('close', onClose);
      }));
    }).catch(error => { this.dispose(); throw error; });
  }

  packageUri(extensionKey: string, extensionId: string): URI {
    const value = this.connection;
    if (!value.extension_keys.includes(extensionKey)) throw new Error('扩展不属于该 Web 宿主冻结发布：' + extensionKey);
    const via = this.request.gateway_connection_id ? '/via/' + encodeURIComponent(this.request.gateway_connection_id) : '';
    const parts = [value.gateway_id, value.backend_id, value.profile_id, value.view_id, value.generation, extensionKey, extensionId].map(encodeURIComponent);
    return URI.parse(new URL('/api/gateway/extension-packages' + via + '/content/' + parts.join('/') + '/', location.href).href);
  }

  override dispose(): void { this.closing = true; this.cancellation.cancel(); super.dispose(); }
}
