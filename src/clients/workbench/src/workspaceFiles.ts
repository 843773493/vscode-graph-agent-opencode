import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Event, Emitter } from '../upstream/src/vs/base/common/event.js';
import { toDisposable, IDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IFileService, FileSystemProviderCapabilities, IFileSystemProviderWithFileReadWriteCapability, IStat, FileType, IFileChange, FileChangeType, createFileSystemProviderError, FileSystemProviderErrorCode, FileOperationError, FileOperationResult } from '../upstream/src/vs/platform/files/common/files.js';
import type { WorkspaceFileContentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ResourceNavigationResultDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { gateway, GatewayRequestError } from './agentApi.js';

const scheme = 'boxteam-file';
/** 根引用作为不透明路径段保存；子路径可直接使用原版 joinPath／relativePath。 */
export function workspaceFileUri(owner: WorkspaceOwner, path: string, rootPath: string): URI {
  if (path !== rootPath && !path.startsWith(rootPath + '/')) throw new Error('文件不属于后端返回的文件根');
  if (!owner.gatewayId || !owner.workspaceId) throw new Error('文件编辑器缺少稳定 Gateway／工作区身份');
  return URI.from({ scheme, authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(rootPath)}${decodeURIComponent(path.slice(rootPath.length))}` });
}

export function workspaceFileSource(resource: URI): WorkspaceOwner & { path: string; root: string } {
  const [, workspaceId, encodedRoot, ...children] = resource.path.split('/');
  if (resource.scheme !== scheme || !resource.authority || !workspaceId || !encodedRoot || resource.query || resource.fragment) {
    throw new Error('文件编辑器资源缺少稳定身份，或包含旧临时路由；请从原文件入口重新打开');
  }
  // 仅反转编辑器投影；公开 URI grammar、根权限和真实目标仍由后端唯一 owner 校验。
  const root = decodeURIComponent(encodedRoot);
  return { gatewayId: resource.authority, workspaceId, root,
    path: root + (children.length ? '/' + children.map(segment => encodeURIComponent(segment)
      .replace(/[!'()*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase())).join('/') : '') };
}

class WorkspaceFiles implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  private readonly changes = new Emitter<readonly IFileChange[]>();
  readonly onDidChangeFile = this.changes.event;
  private readonly watchErrors = new Emitter<string>();
  readonly onDidWatchError = this.watchErrors.event;
  // 仅保留文本模型实际读取的版本；stat/watch 不得推进写入基线。
  private readonly revisions = new Map<string, string>();

  private async request<T>(resource: URI, endpoint: string, options: RequestInit = {}): Promise<T> {
    const source = workspaceFileSource(resource);
    const routeId = await gateway.resolveWorkspace(source);
    try { return await gateway.request<T>(endpoint, options, routeId); }
    catch (error) {
      if (error instanceof GatewayRequestError && error.status === 404) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.FileNotFound);
      if (error instanceof GatewayRequestError && error.status === 403) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.NoPermissions);
      throw error;
    }
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
    const result = await this.request<ResourceNavigationResultDTO>(resource, '/api/v1/resources/navigation', {
      method: 'POST', body: JSON.stringify({ uri: source.path, operation: 'describe' }),
    });
    const { node } = result;
    if (!node || node.uri !== source.path || !node.stat || !['file', 'directory'].includes(node.resource_shape)) {
      throw new Error('后端文件描述缺少配对的资源身份或真实 stat');
    }
    return { type: node.resource_shape === 'directory' ? FileType.Directory : FileType.File,
      ctime: node.stat.changed_at_ms, mtime: node.stat.modified_at_ms, size: node.stat.size };
  }
  async readFile(resource: URI): Promise<Uint8Array> {
    const file = await this.content(resource);
    this.revisions.set(resource.toString(), file.revision);
    return new TextEncoder().encode(file.content);
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
    let revision = this.revisions.get(resource.toString()); let reading = false; let disposed = false;
    const timer = setInterval(async () => {
      if (reading || disposed) return;
      reading = true;
      try {
        const file = await this.content(resource);
        if (!disposed && revision && revision !== file.revision) this.changes.fire([{ resource, type: FileChangeType.UPDATED }]);
        revision = file.revision;
      } catch (error) {
        if (!disposed) { clearInterval(timer); this.watchErrors.fire(`文件监听失败：${error instanceof Error ? error.message : String(error)}`); }
      } finally { reading = false; }
    }, 5000);
    return toDisposable(() => { disposed = true; clearInterval(timer); });
  }
  // 此 provider 只负责已有文本；目录管理复用会话文件树的 operation receipt 链路。
  async mkdir(): Promise<void> { throw new Error('请在会话文件树中新建目录'); }
  async readdir(): Promise<[string, FileType][]> { throw createFileSystemProviderError('请在 Thread 的文件资源中浏览目录', FileSystemProviderErrorCode.FileNotADirectory); }
  async delete(): Promise<void> { throw new Error('文本资源不支持直接删除后端文件'); }
  async rename(): Promise<void> { throw new Error('文本资源不支持直接重命名后端文件'); }
}

export function installBackendFiles(files: IFileService): void {
  files.registerProvider(scheme, new WorkspaceFiles());
}
