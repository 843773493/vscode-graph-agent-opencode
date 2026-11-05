import { URI } from '../upstream/src/vs/base/common/uri.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { encodeHex, VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { getMediaMime } from '../upstream/src/vs/base/common/mime.js';
import { Event, Emitter } from '../upstream/src/vs/base/common/event.js';
import { toDisposable, IDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { isEqualOrParent } from '../upstream/src/vs/base/common/resources.js';
import { newWriteableStream, ReadableStreamEvents } from '../upstream/src/vs/base/common/stream.js';
import { IFileService, FileSystemProviderCapabilities, IFileSystemProviderWithFileReadWriteCapability, IFileSystemProviderWithFileReadStreamCapability, IFileReadStreamOptions, IStat, FileType, IFileChange, FileChangeType, createFileSystemProviderError, FileSystemProviderErrorCode, FileOperationError, FileOperationResult } from '../upstream/src/vs/platform/files/common/files.js';
import type { WorkspaceFileContentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ResourceNavigationResultDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { streamWorkspaceFileEvents } from '../../web/src/api/stream/workspaceFileEvents.js';
import { getWorkspaceRawFileBlob } from '../../web/src/api/workspaceFilesystem.js';
import { gateway, GatewayRequestError } from './agentApi.js';

const scheme = 'boxteam-file';
interface FileWatch {
  controller: AbortController;
  resources: Map<string, { resource: URI; references: number }>;
}
/** 根引用作为不透明路径段保存；子路径可直接使用原版 joinPath／relativePath。 */
export function workspaceFileUri(owner: WorkspaceOwner, path: string, rootPath: string): URI {
  if (path !== rootPath && !path.startsWith(rootPath + '/')) throw new Error('文件不属于后端返回的文件根');
  if (!owner.gatewayId || !owner.workspaceId) throw new Error('文件编辑器缺少稳定 Gateway／工作区身份');
  return URI.from({ scheme, authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(rootPath)}${decodeURIComponent(path.slice(rootPath.length))}` });
}

export function workspaceFileSource(resource: URI): WorkspaceOwner & { path: string; root: string } {
  const [, workspaceId, encodedRoot, ...children] = resource.path.split('/');
  // 原媒体 Webview 添加的 version 仅用于绕过浏览器缓存，不是资源版本参数。
  const mediaCacheQuery = getMediaMime(resource.path) && /^version=\d+$/.test(resource.query);
  if (resource.scheme !== scheme || !resource.authority || !workspaceId || !encodedRoot || (resource.query && !mediaCacheQuery) || resource.fragment) {
    throw new Error('文件编辑器资源缺少稳定身份，或包含旧临时路由；请从原文件入口重新打开');
  }
  // 仅反转编辑器投影；公开 URI grammar、根权限和真实目标仍由后端唯一 owner 校验。
  const root = decodeURIComponent(encodedRoot);
  return { gatewayId: resource.authority, workspaceId, root,
    path: root + (children.length ? '/' + children.map(segment => encodeURIComponent(segment)
      .replace(/[!'()*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase())).join('/') : '') };
}

class WorkspaceFiles implements IFileSystemProviderWithFileReadWriteCapability, IFileSystemProviderWithFileReadStreamCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.FileReadStream | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  private readonly changes = new Emitter<readonly IFileChange[]>();
  readonly onDidChangeFile = this.changes.event;
  private readonly watchErrors = new Emitter<string>();
  readonly onDidWatchError = this.watchErrors.event;
  // 仅完整正文读取能建立保存基线；stat/watch 和媒体范围读取不得推进它。
  private readonly revisions = new Map<string, string>();
  private readonly watches = new Map<string, FileWatch>();

  private async access<T>(resource: URI, action: (workspaceKey: string) => Promise<T>, signal?: AbortSignal): Promise<T> {
    const source = workspaceFileSource(resource);
    const workspaceKey = await gateway.resolveWorkspace(source, signal);
    signal?.throwIfAborted();
    try { return await action(workspaceKey); }
    catch (error) {
      if (error instanceof GatewayRequestError && error.status === 404) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.FileNotFound);
      if (error instanceof GatewayRequestError && error.status === 403) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.NoPermissions);
      throw error;
    }
  }
  private request<T>(resource: URI, endpoint: string, options: RequestInit = {}): Promise<T> {
    return this.access(resource, workspaceKey => gateway.request<T>(endpoint, options, workspaceKey));
  }
  private async content(resource: URI, options: RequestInit = {}): Promise<WorkspaceFileContentDTO> {
    const source = workspaceFileSource(resource);
    const query = new URLSearchParams({ scope: 'workspace', path: source.path });
    const file = await this.request<WorkspaceFileContentDTO>(resource, `/api/v1/workspace/files/content?${query}`, options);
    if (file.path !== source.path || !/^[0-9a-f]{64}$/.test(file.revision)) throw new Error('文件响应的资源 URI 或 revision 不匹配');
    return file;
  }
  async stat(resource: URI): Promise<IStat> {
    const source = workspaceFileSource(resource);
    const result = await this.request<ResourceNavigationResultDTO>(resource, `/api/v1/workspaces/${source.workspaceId}/resources/navigation`, {
      method: 'POST', body: JSON.stringify({ uri: source.path, operation: 'describe' }),
    });
    const { node } = result;
    // 固定工作区入口已核验完整 owner；远端响应会补齐 authority，不能按字符串拒绝等价 URI。
    if (!node?.stat || !['file', 'directory'].includes(node.resource_shape)) {
      throw new Error('后端文件描述缺少配对的资源身份或真实 stat');
    }
    return { type: node.resource_shape === 'directory' ? FileType.Directory : FileType.File,
      ctime: node.stat.changed_at_ms, mtime: node.stat.modified_at_ms, size: node.stat.size };
  }
  async readFile(resource: URI): Promise<Uint8Array> {
    return this.readBytes(resource);
  }
  private async readBytes(resource: URI, signal?: AbortSignal, options: IFileReadStreamOptions = {}): Promise<Uint8Array> {
    if (options.length === 0) return new Uint8Array();
    const start = options.position ?? 0;
    const range = options.position !== undefined || options.length !== undefined
      ? { start, end: options.length === undefined ? undefined : start + options.length - 1 } : undefined;
    const source = workspaceFileSource(resource);
    const blob = await this.access(resource, workspaceKey => getWorkspaceRawFileBlob(0, source.path, workspaceKey, signal, range), signal);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    signal?.throwIfAborted();
    if (!range) {
      // 复用后端 expected_revision 的原字节 SHA-256；不额外请求文本正文或重读磁盘。
      const revision = encodeHex(VSBuffer.wrap(sha256(bytes)));
      this.revisions.set(resource.toString(), revision);
    }
    return bytes;
  }
  readFileStream(resource: URI, options: IFileReadStreamOptions, token: CancellationToken): ReadableStreamEvents<Uint8Array> {
    const stream = newWriteableStream<Uint8Array>(chunks => VSBuffer.concat(chunks.map(chunk => VSBuffer.wrap(chunk))).buffer);
    const controller = new AbortController();
    const cancellation = token.onCancellationRequested(() => controller.abort());
    if (token.isCancellationRequested) controller.abort();
    void this.readBytes(resource, controller.signal, options).then(bytes => stream.end(bytes), error => {
      stream.error(error instanceof Error ? error : new Error(String(error)));
      stream.end();
    }).finally(() => cancellation.dispose());
    return stream;
  }
  async writeFile(resource: URI, bytes: Uint8Array): Promise<void> {
    const expectedRevision = this.revisions.get(resource.toString());
    if (!expectedRevision) throw new Error('保存前必须先读取后端文件，不能跳过 revision 校验');
    try {
      const file = await this.content(resource, { method: 'PUT', body: JSON.stringify({ content: new TextDecoder('utf-8', { fatal: true }).decode(bytes), expected_revision: expectedRevision }) });
      this.revisions.set(resource.toString(), file.revision);
      this.changes.fire([{ resource, type: FileChangeType.UPDATED }]);
    } catch (error) {
      if (error instanceof GatewayRequestError && error.status === 409) {
        // 重读以确认权威状态，但不替换用户尚未保存的文本或其基线。
        const latest = await this.content(resource);
        throw new FileOperationError(`后端文件已变化（${latest.revision}），请比较或重新读取后再保存。${error.message}`, FileOperationResult.FILE_MODIFIED_SINCE);
      }
      throw error;
    }
  }
  watch(resource: URI): IDisposable {
    const source = workspaceFileSource(resource);
    const key = workspaceFileUri(source, source.root, source.root).toString();
    let watch = this.watches.get(key);
    if (!watch) {
      watch = { controller: new AbortController(), resources: new Map() };
      this.watches.set(key, watch);
      const active = watch;
      void this.watchRoot(source, active).catch(error => {
        if (!active.controller.signal.aborted) this.watchErrors.fire(`文件监听失败：${error instanceof Error ? error.message : String(error)}`);
      });
    }
    const name = resource.toString(), entry = watch.resources.get(name) ?? { resource, references: 0 };
    entry.references++;
    watch.resources.set(name, entry);
    return toDisposable(() => {
      if (--entry.references === 0) watch.resources.delete(name);
      if (!watch.resources.size) { watch.controller.abort(); this.watches.delete(key); }
    });
  }

  /** 同一规范根只订阅一次原 watcher／SSE；通知不读正文或推进保存基线。 */
  private async watchRoot(source: ReturnType<typeof workspaceFileSource>, watch: FileWatch): Promise<void> {
    const signal = watch.controller.signal;
    const workspaceKey = await gateway.resolveWorkspace(source, signal);
    if (signal.aborted) return;
    const invalidate = () => this.changes.fire([...watch.resources.values()].map(({ resource }) => ({ resource, type: FileChangeType.UPDATED })));
    let connected = false;
    await streamWorkspaceFileEvents(0, [source.root], {
      workspaceId: workspaceKey, signal,
      // 首次注册不是文件变化；连接重建可能漏掉事件，届时才重新核实已打开资源。
      onConnected: () => { if (connected) invalidate(); connected = true; },
      onBatch: batch => {
        if (signal.aborted) return;
        if (batch.overflow) { invalidate(); return; }
        const watched = [...watch.resources.values()];
        const changes = batch.changes.flatMap(change => {
          // 原观察通道也可覆盖默认工作区；仅接收本次明确根的事件。
          if (change.path !== source.root && !change.path.startsWith(source.root + '/')) return [];
          const resource = workspaceFileUri(source, change.path, source.root);
          if (!watched.some(entry => isEqualOrParent(resource, entry.resource) || isEqualOrParent(entry.resource, resource))) return [];
          const type = change.kind === 'create' ? FileChangeType.ADDED : change.kind === 'delete' ? FileChangeType.DELETED : FileChangeType.UPDATED;
          return [{ resource, type }];
        });
        if (changes.length) this.changes.fire(changes);
      },
    });
    if (!signal.aborted) throw new Error('文件监听流意外结束');
  }
  // 此 provider 读取原字节并沿原文本合同保存；目录管理复用会话文件树的 operation receipt 链路。
  async mkdir(): Promise<void> { throw new Error('请在会话文件树中新建目录'); }
  async readdir(): Promise<[string, FileType][]> { throw createFileSystemProviderError('请在 Thread 的文件资源中浏览目录', FileSystemProviderErrorCode.FileNotADirectory); }
  async delete(): Promise<void> { throw new Error('文本资源不支持直接删除后端文件'); }
  async rename(): Promise<void> { throw new Error('文本资源不支持直接重命名后端文件'); }
}

export function installBackendFiles(files: IFileService): void {
  files.registerProvider(scheme, new WorkspaceFiles());
}
