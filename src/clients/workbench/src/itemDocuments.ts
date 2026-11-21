import { gateway, type ThreadOwner } from './agentApi.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { workspaceContext } from './workspaceContext.js';
import { t } from '../../shared/core/i18n/index.js';
import type { ContextContentReferenceDTO, ThreadItemContentDTO, ThreadItemInspectionDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import type { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { newWriteableStream, type ReadableStreamEvents } from '../upstream/src/vs/base/common/stream.js';
import { Event } from '../upstream/src/vs/base/common/event.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IFileService, FileSystemProviderCapabilities, FileType, FilePermission,
  createFileSystemProviderError, FileSystemProviderErrorCode, type IFileSystemProviderWithFileReadStreamCapability, type IFileReadStreamOptions } from '../upstream/src/vs/platform/files/common/files.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';

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

/** 上下文来源使用明确的文本格式，选择打开方式不预读正文。 */
export function contextContentUri(owner: ThreadOwner, reference: ContextContentReferenceDTO): URI {
  return itemContentUri(owner, 'context', 0, { name: 'context', language: 'plaintext' }).with({
    query: JSON.stringify({ reference }),
  });
}

export class ItemDocuments extends Disposable implements IFileSystemProviderWithFileReadStreamCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadStream | FileSystemProviderCapabilities.Readonly | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  readonly onDidChangeFile = Event.None;
  private readonly pending = new Set<{ resource: URI; controller: AbortController }>();
  constructor(@IFileService files: IFileService, @IEditorService editors: IEditorService) {
    super();
    this._register(files.registerProvider(scheme, this));
    // 上游文本 resolve 不接收编辑器 token；最后一组关闭时释放尚未完成的网络读取。
    this._register(editors.onDidCloseEditor(event => {
      for (const read of this.pending) {
        if (event.editor.resource?.toString() === read.resource.toString() && editors.findEditors(read.resource).length === 0) {
          read.controller.abort();
        }
      }
    }));
  }
  override dispose(): void {
    for (const read of this.pending) read.controller.abort();
    super.dispose();
  }
  watch() { return Disposable.None; }
  private async inspect(resource: URI, signal?: AbortSignal) {
    signal?.throwIfAborted();
    const parts = resource.path.slice(1).split('/').map(decodeURIComponent);
    if (parts.length !== 6 || parts.some(part => !part) || !resource.authority || resource.fragment
      || !/^\d+$/.test(parts[4])) throw new Error('Item 内容引用身份无效');
    const [workspaceId, sessionId, threadId, itemId, index] = parts;
    const context = this.contextReference(resource);
    const controller = new AbortController();
    const read = { resource, controller };
    this.pending.add(read);
    const reading = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
    const invalidate = () => controller.abort(new Error(t('quickAccess.resource.identityChanged')));
    const releaseToken = onGatewayTokenInvalidated(0, invalidate);
    let gatewayId = workspaceContext.getSnapshot().gatewayId;
    const releaseGateway = workspaceContext.subscribe(() => {
      const current = workspaceContext.getSnapshot().gatewayId;
      if (gatewayId && current !== gatewayId) invalidate();
      gatewayId = current;
    });
    try {
      const target = await gateway.resolveWorkspaceTarget({ gatewayId: resource.authority, workspaceId }, reading);
      reading.throwIfAborted();
      if (target.status !== 'ready') throw new Error(`Item 所属工作区不可用：${target.status}`);
      const result = await gateway.request<ThreadItemInspectionDTO>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}/items/inspect`,
        { signal: reading, method: 'POST', body: JSON.stringify(context
          ? { content_ref: context.reference, operation: 'content' } : { item_id: itemId, operation: 'content' }) }, gatewayWorkspaceKey(target));
      reading.throwIfAborted();
      if (!context && result.location?.item_id !== itemId) throw new Error('Item 内容响应身份不一致');
      return { result, index: Number(index) };
    } finally { this.pending.delete(read); releaseToken(); releaseGateway(); }
  }
  private contextReference(resource: URI): { reference: ContextContentReferenceDTO } | undefined {
    if (!resource.query) return undefined;
    const value: { reference: ContextContentReferenceDTO } = JSON.parse(resource.query);
    if (!value.reference || !['item', 'sealed_contribution'].includes(value.reference.kind)) throw new Error('上下文内容引用身份无效');
    return value;
  }
  async stat() {
    // 沿用上游虚拟文档的未知大小语义；canonical／封存长度不等于投影文档字节数。
    return { type: FileType.File, ctime: 0, mtime: 0, size: 0, permissions: FilePermission.Readonly };
  }
  private async readBytes(resource: URI, signal?: AbortSignal): Promise<Uint8Array> {
    const { result, index } = await this.inspect(resource, signal);
    const content = result.contents[index];
    if (!content || content.content === undefined || content.uri) throw new Error('Item 文本已不可用，请重新打开其内容');
    return content.encoding === 'base64' ? Uint8Array.from(atob(content.content), character => character.charCodeAt(0))
      : new TextEncoder().encode(content.content);
  }
  readFileStream(resource: URI, options: IFileReadStreamOptions, token: CancellationToken): ReadableStreamEvents<Uint8Array> {
    const stream = newWriteableStream<Uint8Array>(chunks => VSBuffer.concat(chunks.map(chunk => VSBuffer.wrap(chunk))).buffer);
    const controller = new AbortController();
    const cancellation = token.onCancellationRequested(() => controller.abort());
    if (token.isCancellationRequested) controller.abort();
    const reading = options.length === 0 ? Promise.resolve(new Uint8Array()) : this.readBytes(resource, controller.signal);
    void reading.then(bytes => {
      controller.signal.throwIfAborted();
      const start = options.position ?? 0;
      stream.end(bytes.subarray(start, options.length === undefined ? undefined : start + options.length));
    }).catch(error => {
      stream.error(error instanceof Error ? error : new Error(String(error)));
      stream.end();
    }).finally(() => cancellation.dispose());
    return stream;
  }
  private deny(): never { throw createFileSystemProviderError('会话历史内容只读', FileSystemProviderErrorCode.NoPermissions); }
  writeFile(): never { return this.deny(); }
  mkdir(): never { return this.deny(); }
  delete(): never { return this.deny(); }
  rename(): never { return this.deny(); }
  readdir(): never { return this.deny(); }
}
registerWorkbenchContribution2('boxteam.itemDocuments', ItemDocuments, WorkbenchPhase.BlockRestore);
