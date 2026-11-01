import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Event, Emitter } from '../upstream/src/vs/base/common/event.js';
import { toDisposable, IDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IFileService, FileSystemProviderCapabilities, IFileSystemProviderWithFileReadWriteCapability, IStat, FileType, IFileChange, FileChangeType, createFileSystemProviderError, FileSystemProviderErrorCode, FileOperationError, FileOperationResult } from '../upstream/src/vs/platform/files/common/files.js';
import type { WorkspaceFileContentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway, GatewayRequestError, ThreadOwner } from './agentApi.js';

const scheme = 'boxteam-file';
// 查询部分冻结 Gateway route 和后端资源 URI；path 只负责标签、面包屑显示。
export function workspaceFileUri(owner: ThreadOwner, file: Pick<WorkspaceFileContentDTO, 'path'>, rootPath: string): URI {
  if (!file.path.startsWith(rootPath + '/')) throw new Error('文件不属于后端返回的文件根');
  return URI.from({ scheme, authority: owner.workspaceId, path: decodeURIComponent(file.path.slice(rootPath.length)),
    query: new URLSearchParams({ route: owner.routeId, path: file.path }).toString() });
}

function fileSource(resource: URI): { routeId: string; workspaceId: string; path: string } {
  const query = new URLSearchParams(resource.query);
  const routeId = query.get('route'), path = query.get('path');
  if (!resource.authority || !routeId || !path) throw createFileSystemProviderError('文件 URI 缺少工作区路由或后端资源路径', FileSystemProviderErrorCode.FileNotFound);
  return { routeId, workspaceId: resource.authority, path };
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

  private async request<T>(resource: URI, options: RequestInit = {}): Promise<T> {
    const source = fileSource(resource);
    await gateway.assertWorkspace(source);
    const query = new URLSearchParams({ scope: 'workspace', path: source.path });
    try { return await gateway.request<T>(`/api/v1/workspace/files/content?${query}`, options, source.routeId); }
    catch (error) {
      if (error instanceof GatewayRequestError && error.status === 404) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.FileNotFound);
      throw error;
    }
  }
  private async content(resource: URI): Promise<WorkspaceFileContentDTO> {
    const file = await this.request<WorkspaceFileContentDTO>(resource);
    if (file.path !== fileSource(resource).path || !/^[0-9a-f]{64}$/.test(file.revision)) throw new Error('文件响应的资源 URI 或 revision 不匹配');
    return file;
  }
  async stat(resource: URI): Promise<IStat> {
    const file = await this.content(resource);
    const mtime = Date.parse(file.modified_at ?? '');
    if (!Number.isFinite(mtime)) throw new Error('后端文件缺少有效的 modified_at');
    return { type: FileType.File, ctime: mtime, mtime, size: file.size };
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
      const file = await this.request<WorkspaceFileContentDTO>(resource, { method: 'PUT', body: JSON.stringify({ content: new TextDecoder('utf-8', { fatal: true }).decode(bytes), expected_revision: expectedRevision }) });
      if (file.path !== fileSource(resource).path || !/^[0-9a-f]{64}$/.test(file.revision)) throw new Error('文件保存响应的资源 URI 或 revision 不匹配');
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
