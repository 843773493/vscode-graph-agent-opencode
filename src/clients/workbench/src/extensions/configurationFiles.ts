import { decodeBase64, encodeBase64, VSBuffer } from '../../upstream/src/vs/base/common/buffer.js';
import { Emitter, Event } from '../../upstream/src/vs/base/common/event.js';
import { Disposable, IDisposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { dirname, extUri } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { createFileSystemProviderError, FileChangeType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, FileType, IFileChange, IFileService, IFileSystemProviderWithFileReadWriteCapability, IFileWriteOptions, IStat } from '../../upstream/src/vs/platform/files/common/files.js';
import { requestGatewayResponse } from '../../../web/src/api/http.js';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../../../web/src/sse/sseClient.js';
import { SSE_IDLE_TIMEOUT_MS } from '../../../web/src/sse/sseIdleTimeout.js';
import { validateExtensionConfigurationEventDTO } from '../../../../shared/sseRuntimeValidators.js';
import type { ExtensionConfigurationEventDTO, ExtensionConfigurationReadDTO, ExtensionConfigurationStatDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway, GatewayRequestError } from '../agentApi.js';
import { targetConfigurationRequest, targetSettings, targetWorkspace, type TargetExtensionContext } from './targetContext.js';

interface Route {
  key: string;
  context: TargetExtensionContext;
  resources: readonly URI[];
  references: number;
  pending: number;
  controller: AbortController;
  stream?: AbortController;
}

/** 只路由已认证的两个原配置文件；正文和保存基线仍归原 FileService／TextFileService。 */
class TargetConfigurationFiles extends Disposable implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  private readonly changes = this._register(new Emitter<readonly IFileChange[]>());
  readonly onDidChangeFile = this.changes.event;
  private readonly errors = this._register(new Emitter<string>());
  readonly onDidWatchError = this.errors.event;
  private readonly routes = new Map<string, Route>();
  private readonly watches = new Map<string, { resource: URI; count: number }>();

  private resources(context: TargetExtensionContext): readonly URI[] {
    const resources = [targetSettings(context), targetWorkspace(context).configuration].filter(value => !!value)
      .map(value => URI.parse(value.resource_uri));
    if (resources.some(uri => uri.scheme !== 'boxteam' || uri.query || uri.fragment)) throw new Error('目标配置不是规范资源 URI');
    return resources;
  }

  register(context: TargetExtensionContext): IDisposable {
    return this.retainRoute(this.configurationRoute(context, this.resources(context)));
  }

  private configurationRoute(context: TargetExtensionContext, resources: readonly URI[]): Route {
    const key = JSON.stringify([context.kind, targetConfigurationRequest(context), resources.map(resource => resource.toString()).sort()]);
    let route = this.routes.get(key);
    if (!route) {
      route = { key, context, resources, references: 0, pending: 0, controller: new AbortController() };
      this.routes.set(key, route);
    }
    return route;
  }

  private retainRoute(route: Route): IDisposable {
    route.references++;
    this.updateWatches();
    return toDisposable(() => {
      if (--route.references !== 0) return;
      route.stream?.abort(); route.stream = undefined;
      this.releaseRoute(route);
      this.updateWatches();
    });
  }

  private releaseRoute(route: Route): void {
    if (route.references || route.pending) return;
    this.routes.delete(route.key);
    route.controller.abort();
  }

  private route(resource: URI, directory = false): Route {
    // 同一 profile 文件可由多个 view 注册；只按精确规范资源选择活动宿主路由。
    const route = [...this.routes.values()].find(candidate => candidate.references > 0
      && candidate.resources.some(uri => extUri.isEqual(uri, resource) || directory && extUri.isEqual(dirname(uri), resource)));
    if (!route?.references) throw createFileSystemProviderError('配置资源未绑定可用的目标宿主：' + resource.toString(), FileSystemProviderErrorCode.Unavailable);
    return route;
  }
  private async request<T>(resource: URI, operation: string, extra: object = {}, directory = false): Promise<T> {
    const route = this.route(resource, directory);
    // 固定本次已认证路由；宿主释放后，pending 计数会等请求结算再销毁路由。
    const signal = route.controller.signal;
    const body = JSON.stringify({ ...targetConfigurationRequest(route.context), resource_uri: resource.toString(), ...extra });
    route.pending++;
    try {
      const result = await gateway.request<T>('/api/gateway/extension-configurations/' + operation, {
        method: 'POST', signal, body,
      });
      signal.throwIfAborted();
      return result;
    } catch (error) {
      if (error instanceof GatewayRequestError) {
        const detail = error.detail;
        const exists = typeof detail === 'object' && detail !== null && 'owner' in detail && detail.owner === 'extension_configuration'
          && 'code' in detail && detail.code === 'EntryExists';
        const code = error.status === 404 ? FileSystemProviderErrorCode.FileNotFound
          : error.status === 403 ? FileSystemProviderErrorCode.NoPermissions
            : error.status === 409 ? exists ? FileSystemProviderErrorCode.FileExists : FileSystemProviderErrorCode.Unavailable : undefined;
        if (code) throw createFileSystemProviderError(error, code);
      }
      throw error;
    } finally {
      route.pending--;
      this.releaseRoute(route);
    }
  }
  async stat(resource: URI): Promise<IStat> {
    const stat = await this.request<ExtensionConfigurationStatDTO>(resource, 'stat', {}, true);
    return { ...stat, permissions: stat.permissions ?? undefined };
  }
  async readFile(resource: URI): Promise<Uint8Array> {
    const result = await this.request<ExtensionConfigurationReadDTO>(resource, 'read');
    return decodeBase64(result.content_base64).buffer;
  }
  async writeFile(resource: URI, content: Uint8Array, options: IFileWriteOptions): Promise<void> {
    if (options.atomic || options.append) throw new Error('目标配置 Provider 不支持原子写入或追加');
    await this.request<ExtensionConfigurationStatDTO>(resource, 'write', {
      content_base64: encodeBase64(VSBuffer.wrap(content)),
      options: { create: options.create, overwrite: options.overwrite, unlock: options.unlock ?? false, atomic: false, append: false },
    });
  }
  watch(resource: URI): IDisposable {
    this.route(resource, true);
    const key = resource.toString(), watch = this.watches.get(key) ?? { resource, count: 0 };
    watch.count++; this.watches.set(key, watch); this.updateWatches();
    return toDisposable(() => { if (--watch.count === 0) this.watches.delete(key); this.updateWatches(); });
  }
  private updateWatches(): void {
    for (const route of this.routes.values()) {
      const needed = route.references > 0 && [...this.watches.values()].some(watch => route.resources.some(uri =>
        extUri.isEqual(uri, watch.resource) || extUri.isEqual(dirname(uri), watch.resource)));
      if (!needed) { route.stream?.abort(); route.stream = undefined; }
      else if (!route.stream) {
        const stream = route.stream = new AbortController();
        void this.watchRoute(route, stream.signal).catch(error => {
          if (!stream.signal.aborted && !route.controller.signal.aborted) this.errors.fire('目标配置监听失败：' + String(error));
        });
      }
    }
  }
  private async watchRoute(route: Route, watchSignal: AbortSignal): Promise<void> {
    const signal = AbortSignal.any([route.controller.signal, watchSignal]);
    const response = await requestGatewayResponse(0, '/api/gateway/extension-configurations/events', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(targetConfigurationRequest(route.context)), signal,
    });
    const accept = (event: ExtensionConfigurationEventDTO) => {
      signal.throwIfAborted();
      if (event.error) throw new Error(event.error);
      const changes = event.overflow ? route.resources.map(resource => ({ resource, type: FileChangeType.UPDATED }))
        : event.changes.map(change => {
          const resource = URI.parse(change.resource_uri);
          if (!route.resources.some(uri => extUri.isEqual(uri, resource))) throw new Error('目标配置监听返回未绑定资源');
          if (!['create', 'edit', 'delete'].includes(change.kind)) throw new Error('目标配置监听返回未知变化类型：' + change.kind);
          return { resource, type: change.kind === 'create' ? FileChangeType.ADDED : change.kind === 'delete' ? FileChangeType.DELETED : FileChangeType.UPDATED };
        });
      if (changes.length) this.changes.fire(changes);
    };
    const event = defineSseEvent((data, frame) => {
      const value = decodeJsonSseData(data, frame);
      if (!validateExtensionConfigurationEventDTO(value)) throw new Error('目标配置事件不符合公开结构：' + JSON.stringify(validateExtensionConfigurationEventDTO.errors));
      return value as ExtensionConfigurationEventDTO;
    }, accept);
    await consumeSseResponse(response, { signal, idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
      idleTimeoutError: timeoutMs => new Error(`目标配置监听超过 ${timeoutMs}ms 未收到数据`),
      events: { ready: event, changes: event,
      error: defineSseEvent(event.decode, value => { throw new Error(value.error ?? '目标配置监听返回错误帧'); }),
    } });
    if (!signal.aborted) throw new Error('目标配置监听流意外结束');
  }
  async mkdir(resource: URI): Promise<void> {
    // 父目录由目标初始化 owner 准备；FileService 的重复准备只核该精确目录。
    const stat = await this.stat(resource);
    if (!(stat.type & FileType.Directory)) throw createFileSystemProviderError('配置目标已是文件', FileSystemProviderErrorCode.FileExists);
  }
  async readdir(): Promise<[string, FileType][]> { throw createFileSystemProviderError('目标配置入口不提供目录枚举', FileSystemProviderErrorCode.NoPermissions); }
  async delete(): Promise<void> { throw createFileSystemProviderError('请通过原配置编辑删除设置覆盖', FileSystemProviderErrorCode.NoPermissions); }
  async rename(): Promise<void> { throw createFileSystemProviderError('目标配置入口不提供重命名', FileSystemProviderErrorCode.NoPermissions); }
}

const providers = new WeakMap<IFileService, TargetConfigurationFiles>();
function configurationFiles(files: IFileService): TargetConfigurationFiles {
  let provider = providers.get(files);
  if (!provider) {
    provider = new TargetConfigurationFiles();
    files.registerProvider('boxteam', provider);
    providers.set(files, provider);
  }
  return provider;
}

export function registerTargetConfigurationFiles(files: IFileService, context: TargetExtensionContext): IDisposable {
  return configurationFiles(files).register(context);
}
