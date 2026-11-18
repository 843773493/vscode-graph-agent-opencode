import { gateway, type ThreadOwner } from './agentApi.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { workspaceContext } from './workspaceContext.js';
import { t } from '../../shared/core/i18n/index.js';
import type { ThreadItemContentDTO, ThreadItemInspectionDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Event } from '../upstream/src/vs/base/common/event.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IFileService, FileSystemProviderCapabilities, FileType, FilePermission,
  createFileSystemProviderError, FileSystemProviderErrorCode, type IFileSystemProviderWithFileReadWriteCapability } from '../upstream/src/vs/platform/files/common/files.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';

const scheme = 'boxteam-item-content';
const mediaSuffixes: Record<string, string> = {
  'image/png': 'png', 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/gif': 'gif',
  'image/webp': 'webp', 'image/svg+xml': 'svg', 'image/bmp': 'bmp', 'image/avif': 'avif', 'image/x-icon': 'ico',
  'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/ogg': 'ogg',
  'video/mp4': 'mp4', 'video/webm': 'webm',
};
const suffixes: Record<string, string> = { markdown: 'md', plaintext: 'txt', javascript: 'js', typescript: 'ts', python: 'py', shell: 'sh' };

/** 编辑器只保存稳定引用，正文由原 FileService／模型引用计数持有和释放。 */
export function itemContentUri(owner: ThreadOwner, itemId: string, index: number, content: ThreadItemContentDTO): URI {
  return URI.from({ scheme, authority: owner.gatewayId, path: '/' + [owner.workspaceId, owner.sessionId, owner.threadId,
    itemId, String(index), `${content.name}.${(content.mime_type && mediaSuffixes[content.mime_type]) || suffixes[content.language] || content.language || 'txt'}`].map(encodeURIComponent).join('/') });
}

export class ItemDocuments extends Disposable implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.Readonly | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  readonly onDidChangeFile = Event.None;
  constructor(@IFileService files: IFileService) { super(); this._register(files.registerProvider(scheme, this)); }
  watch() { return Disposable.None; }
  private async inspect(resource: URI, operation: 'location' | 'content') {
    const parts = resource.path.slice(1).split('/').map(decodeURIComponent);
    if (parts.length !== 6 || parts.some(part => !part) || !resource.authority || resource.query || resource.fragment
      || !/^\d+$/.test(parts[4])) throw new Error('Item 内容引用身份无效');
    const [workspaceId, sessionId, threadId, itemId, index] = parts;
    const controller = new AbortController();
    const invalidate = () => controller.abort(new Error(t('quickAccess.resource.identityChanged')));
    const releaseToken = onGatewayTokenInvalidated(0, invalidate);
    let gatewayId = workspaceContext.getSnapshot().gatewayId;
    const releaseGateway = workspaceContext.subscribe(() => {
      const current = workspaceContext.getSnapshot().gatewayId;
      if (gatewayId && current !== gatewayId) invalidate();
      gatewayId = current;
    });
    try {
      const target = await gateway.resolveWorkspaceTarget({ gatewayId: resource.authority, workspaceId }, controller.signal);
      controller.signal.throwIfAborted();
      if (target.status !== 'ready') throw new Error(`Item 所属工作区不可用：${target.status}`);
      const result = await gateway.request<ThreadItemInspectionDTO>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}/items/inspect`,
        { signal: controller.signal, method: 'POST', body: JSON.stringify({ item_id: itemId, operation }) }, gatewayWorkspaceKey(target));
      controller.signal.throwIfAborted();
      if (result.location?.item_id !== itemId) throw new Error('Item 内容响应身份不一致');
      return { result, index: Number(index) };
    } finally { releaseToken(); releaseGateway(); }
  }
  async stat(resource: URI) {
    const { result } = await this.inspect(resource, 'location');
    return { type: FileType.File, ctime: 0, mtime: 0, size: result.location!.byte_length, permissions: FilePermission.Readonly };
  }
  async readFile(resource: URI): Promise<Uint8Array> {
    const { result, index } = await this.inspect(resource, 'content');
    const content = result.contents[index];
    if (!content || content.content === undefined || content.uri) throw new Error('Item 文本已不可用，请重新打开其内容');
    return content.encoding === 'base64' ? Uint8Array.from(atob(content.content), character => character.charCodeAt(0))
      : new TextEncoder().encode(content.content);
  }
  private deny(): never { throw createFileSystemProviderError('会话历史内容只读', FileSystemProviderErrorCode.NoPermissions); }
  writeFile(): never { return this.deny(); }
  mkdir(): never { return this.deny(); }
  delete(): never { return this.deny(); }
  rename(): never { return this.deny(); }
  readdir(): never { return this.deny(); }
}
registerWorkbenchContribution2('boxteam.itemDocuments', ItemDocuments, WorkbenchPhase.BlockRestore);
