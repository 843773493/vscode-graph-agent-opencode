import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Sequencer } from '../upstream/src/vs/base/common/async.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { encodeHex, VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { getMediaMime } from '../upstream/src/vs/base/common/mime.js';
import { Event, Emitter } from '../upstream/src/vs/base/common/event.js';
import { toDisposable, IDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import type { IUriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import type { IUntypedEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import type { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import type { IEditorGroup } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { IWorkingCopyBackupService } from '../upstream/src/vs/workbench/services/workingCopy/common/workingCopyBackup.js';
import { newWriteableStream, ReadableStreamEvents } from '../upstream/src/vs/base/common/stream.js';
import { IFileService, FileSystemProviderCapabilities, IFileSystemProviderWithFileReadWriteCapability, IFileSystemProviderWithFileReadStreamCapability, IFileReadStreamOptions, IFileWriteOptions, IStat, FileType, IFileChange, FileChangeType, createFileSystemProviderError, FileSystemProviderErrorCode, FileOperationError, FileOperationResult, toFileSystemProviderErrorCode } from '../upstream/src/vs/platform/files/common/files.js';
import type { WorkspaceFileContentDTO, WorkspaceFileReferencesDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { WorkspaceFileList } from '../../web/src/types/backend.js';
import type { ResourceNavigationResultDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import { streamWorkspaceFileEvents } from '../../web/src/api/stream/workspaceFileEvents.js';
import { getWorkspaceFileRoots, getWorkspaceRawFileBlob } from '../../web/src/api/workspaceFilesystem.js';
import { resourceFileRootUri } from '../../web/src/utils/workspaceFileReferences.js';
import { gateway, GatewayRequestError } from './agentApi.js';
import { openWorkspaceFileIntentStore } from '../../web/src/state/workspaceFiles/intentStore.js';
import { waitForWorkspaceFileOperation, WorkspaceFileIntentRejectedError } from '../../web/src/state/workspaceFiles/dispatcher.js';

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

/** 备份复用原完整读取建立的 CAS；不为热退出重新读取或计算文件 hash。 */
export function workspaceFileRevisionForBackup(files: IFileService, resource: URI): string | undefined {
  if (resource.scheme !== scheme) return undefined;
  const provider = files.getProvider(scheme);
  if (!(provider instanceof WorkspaceFiles)) throw new Error('工作区文件 Provider 未注册，不能备份保存基线');
  return provider.revisionForBackup(resource);
}

export function restoreWorkspaceFileRevision(files: IFileService, resource: URI, revision: string | undefined): void {
  if (resource.scheme !== scheme) return;
  const provider = files.getProvider(scheme);
  if (!(provider instanceof WorkspaceFiles)) throw new Error('工作区文件 Provider 未注册，不能恢复保存基线');
  provider.restoreRevision(resource, revision);
}

/** 每次准入按 owner 批量取规范引用；结果只沿本次调用传递，不保存身份缓存。 */
export async function canonicalWorkspaceFileResources(resources: readonly URI[], signal?: AbortSignal): Promise<URI[]> {
  const result = [...resources];
  const groups = new Map<string, { source: ReturnType<typeof workspaceFileSource>; indices: number[] }>();
  resources.forEach((resource, index) => {
    if (resource.scheme !== scheme) return;
    const source = workspaceFileSource(resource), key = JSON.stringify([source.gatewayId, source.workspaceId]);
    if (source.path === source.root) return;
    const group = groups.get(key) ?? { source, indices: [] };
    group.indices.push(index); groups.set(key, group);
  });
  await Promise.all([...groups.values()].map(async ({ source, indices }) => {
    const workspaceKey = gatewayWorkspaceKey(source);
    for (let offset = 0; offset < indices.length; offset += 100) {
      signal?.throwIfAborted();
      const batch = indices.slice(offset, offset + 100);
      const reply = await gateway.request<WorkspaceFileReferencesDTO>('/api/v1/workspace/files/references', {
        method: 'POST', signal, body: JSON.stringify({ paths: batch.map(index => workspaceFileSource(resources[index]).path) }),
      }, workspaceKey);
      if (reply.uris.length !== batch.length) throw new Error('文件规范引用响应与批量输入不配对');
      batch.forEach((index, position) => {
        const original = workspaceFileSource(resources[index]);
        const root = resourceFileRootUri(reply.uris[position]);
        if (!root) throw new Error('文件规范引用响应缺少 owner 文件根');
        const canonical = workspaceFileUri(original, reply.uris[position], root).with({ query: resources[index].query });
        result[index] = canonical.path === resources[index].path ? resources[index] : canonical;
      });
    }
  }));
  return result;
}

/** 复用原输入结构；diff／分屏／merge 的所有文件在同次 owner 准入中归一。 */
export async function canonicalWorkspaceFileInputs(inputs: readonly IUntypedEditorInput[]): Promise<IUntypedEditorInput[]> {
  const resources: URI[] = [];
  function project<T extends IUntypedEditorInput>(input: T, resource: (value: URI) => URI): T {
    if ('original' in input && 'modified' in input) return { ...input, original: project(input.original, resource), modified: project(input.modified, resource) };
    if ('primary' in input && 'secondary' in input) return { ...input, primary: project(input.primary, resource), secondary: project(input.secondary, resource) };
    if ('input1' in input && 'input2' in input) return { ...input, input1: project(input.input1, resource), input2: project(input.input2, resource), base: project(input.base, resource), result: project(input.result, resource) };
    if ('resources' in input) return { ...input, resources: input.resources?.map(item => ({ ...item, ...project(item, resource) })) };
    return 'resource' in input && input.resource ? { ...input, resource: resource(input.resource) } : input;
  }
  inputs.forEach(input => project(input, value => { resources.push(value); return value; }));
  const canonical = await canonicalWorkspaceFileResources(resources);
  let index = 0;
  return inputs.map(input => {
    let changed = false;
    const projected = project(input, value => { const next = canonical[index++]; changed ||= value !== next; return next; });
    return changed ? projected : input;
  });
}

// 原编辑器组可并发恢复同一文件；只串行化恢复事务，不保存 URI 身份事实。
const restoredFiles = new Sequencer();

/** 原同步反序列化后、首次模型取得前，迁移原备份并交原编辑器组替换输入。 */
export function canonicalRestoredWorkspaceFiles(group: IEditorGroup, create: (input: IUntypedEditorInput) => EditorInput,
  files: IFileService, backups: IWorkingCopyBackupService, identity: IUriIdentityService,
  restoreViewState: (source: EditorInput, target: EditorInput) => object | undefined): Promise<void> {
  return restoredFiles.queue(async () => {
    const editors = group.editors.map(editor => ({ editor, input: editor.toUntyped({ preserveViewState: group.id }) })).filter(
      (entry): entry is { editor: EditorInput; input: IUntypedEditorInput } => !!entry.input);
    // 连同无标签的热退出备份一起归一，原 BackupTracker 随后仍负责恢复它们。
    const identifiers = (await backups.getBackups()).filter(backup => backup.resource.scheme === scheme);
    const inputs = await canonicalWorkspaceFileInputs([...editors.map(entry => entry.input), ...identifiers.map(({ resource }) => ({ resource }))]);
    const key = (resource: URI, typeId: string) => JSON.stringify([identity.extUri.getComparisonKey(resource), typeId]);
    const targets = new Set<string>();
    const migrations = identifiers.flatMap((source, index) => {
      const resource = (inputs[editors.length + index] as { resource: URI }).resource;
      if (identity.extUri.isEqual(source.resource, resource)) return [];
      const target = { resource, typeId: source.typeId }, targetKey = key(resource, source.typeId);
      if (backups.hasBackupSync(target) || targets.has(targetKey)) {
        throw new Error(`文件恢复的规范目标已有草稿备份，已保留双方：${source.resource.toString()} → ${resource.toString()}`);
      }
      targets.add(targetKey);
      return [{ source, target }];
    });
    // references 允许未创建后缀；只有真实文件才能承接旧 URI 的未保存正文。
    for (const { target } of migrations) {
      if (!(await files.stat(target.resource)).isFile) throw new Error(`草稿备份的规范目标不是文件：${target.resource.toString()}`);
    }
    for (const { source, target } of migrations) {
      const backup = await backups.resolve(source);
      if (!backup) throw new Error(`文件恢复时原草稿备份不可读取，未删除原键：${source.resource.toString()}`);
      // versionId 仅属于原运行期缓存，磁盘备份不保存它；正文和 metadata 按原合同迁移。
      await backups.backup(target, backup.value, undefined, backup.meta);
      await backups.discardBackup(source);
    }
    const replacements = editors.flatMap(({ editor, input }, index) => {
      const canonical = inputs[index];
      if (input === canonical) return [];
      const replacement = create(canonical), viewState = restoreViewState(editor, replacement);
      return [{ editor, replacement, options: { ...canonical.options, viewState: canonical.options?.viewState ?? viewState,
        pinned: group.isPinned(editor), sticky: group.isSticky(editor), preserveFocus: true } }];
    });
    if (replacements.length) await group.replaceEditors(replacements);
  });
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
  constructor(private readonly identity: IUriIdentityService) { }
  private key(resource: URI): string { return this.identity.extUri.getComparisonKey(resource); }
  revisionForBackup(resource: URI): string | undefined { return this.revisions.get(this.key(resource)); }
  restoreRevision(resource: URI, revision: string | undefined): void {
    if (revision !== undefined && !/^[0-9a-f]{64}$/.test(revision)) throw new Error('草稿备份的文件 revision 不是有效的原字节 SHA-256');
    if (revision === undefined) this.revisions.delete(this.key(resource));
    else this.revisions.set(this.key(resource), revision);
  }

  private async access<T>(resource: URI, action: (workspaceKey: string) => Promise<T>, signal?: AbortSignal): Promise<T> {
    const source = workspaceFileSource(resource);
    // 真实请求由 Gateway 按完整 owner 解析和授权，不在每次 I/O 前重复枚举登记表。
    const workspaceKey = gatewayWorkspaceKey(source);
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
  private async createEntry(resource: URI, kind: 'file' | 'directory'): Promise<void> {
    const source = workspaceFileSource(resource);
    const workspaceKey = gatewayWorkspaceKey(source);
    const roots = await getWorkspaceFileRoots(0, workspaceKey);
    const root = source.root;
    const scope: 'workspace' | 'filesystem' | null = root === roots.workspace_uri ? 'workspace'
      : root === roots.filesystem_uri ? 'filesystem' : null;
    if (!scope) {
      throw new Error('文件创建必须来自后端明确公布的工作区或文件系统 URI 根');
    }
    const parent = URI.from({ scheme: resource.scheme, authority: resource.authority,
      path: resource.path.substring(0, resource.path.lastIndexOf('/')) });
    const parentSource = workspaceFileSource(parent);
    const directoryPath = parentSource.path;
    const intentStore = await openWorkspaceFileIntentStore();
    const intentScope = { gatewayId: source.gatewayId, workspaceId: source.workspaceId, workspaceRoot: roots.workspace_uri, actor: 'local' as const };
    for (const previous of await intentStore.load(intentScope)) {
      const previousReceipt = await waitForWorkspaceFileOperation(0, previous, intentStore);
      await intentStore.remove(previous);
      if (previousReceipt.state !== 'committed') throw new Error(`前序文件操作未提交: ${previousReceipt.error_detail ?? previousReceipt.state}`);
    }
    const query = new URLSearchParams({ path: directoryPath, scope });
    const snapshot = await gateway.request<WorkspaceFileList>(`/api/v1/workspace/files?${query}`, {}, workspaceKey);
    if (!snapshot.directory_key || snapshot.directory_key.path !== directoryPath || snapshot.directory_key.workspace_id !== source.workspaceId) {
      throw new Error('创建目标目录响应缺少配对 URI、owner 与 revision');
    }
    const record = await intentStore.insert({
      scope: intentScope,
      directoryPath, directoryKey: snapshot.directory_key, directoryRevision: snapshot.directory_revision,
      command: { kind: 'create', name: resource.path.slice(resource.path.lastIndexOf('/') + 1), entryKind: kind },
    });
    let conflict = false;
    try {
      const receipt = await waitForWorkspaceFileOperation(0, record, intentStore);
      if (receipt.state !== 'committed') {
        if (['rejected', 'cancelled', 'dependency_failed'].includes(receipt.state)) await intentStore.remove(record);
        conflict = receipt.state === 'rejected' && receipt.error_code === 'file_operation_conflict'
          && receipt.effect === 'none' && receipt.created_path === source.path;
        throw new Error(`文件${kind === 'file' ? '创建' : '目录创建'}失败: ${receipt.error_detail ?? receipt.state}`);
      }
      if (receipt.created_path !== source.path) throw new Error('文件创建回执与原资源 URI 不一致，保留回执供核实');
      await intentStore.remove(record);
    } catch (error) {
      if (error instanceof WorkspaceFileIntentRejectedError) {
        await intentStore.remove(record);
        conflict = error.errors[0] instanceof GatewayRequestError && error.errors[0].status === 409;
      }
      // 原 mkdirp 允许并发建立同一目录；只有已结清冲突且准确目标确为目录才沿原 FileExists 合同交还。
      if (kind === 'directory' && conflict) {
        let stat: IStat;
        try { stat = await this.stat(resource); }
        catch (statError) {
          if (toFileSystemProviderErrorCode(statError) === FileSystemProviderErrorCode.FileNotFound) throw error;
          throw new AggregateError([error, statError], '目录创建冲突后无法核实准确目标');
        }
        if (stat.type & FileType.Directory) throw createFileSystemProviderError(error, FileSystemProviderErrorCode.FileExists);
      }
      throw error;
    }
    this.changes.fire([{ resource, type: FileChangeType.ADDED }]);
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
      this.revisions.set(this.key(resource), revision);
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
  async writeFile(resource: URI, bytes: Uint8Array, options: IFileWriteOptions): Promise<void> {
    let expectedRevision = this.revisions.get(this.key(resource));
    let existsBeforeWrite = expectedRevision !== undefined;
    if (!expectedRevision || !options.overwrite) {
      try { await this.stat(resource); existsBeforeWrite = true; }
      catch (error) {
        if (toFileSystemProviderErrorCode(error) !== FileSystemProviderErrorCode.FileNotFound) throw error;
        existsBeforeWrite = false;
        expectedRevision = undefined;
      }
    }
    if (existsBeforeWrite && !options.overwrite) {
      throw createFileSystemProviderError(`目标文件已存在: ${resource.toString()}`, FileSystemProviderErrorCode.FileExists);
    }
    if (!existsBeforeWrite && !options.create) {
      throw createFileSystemProviderError(`目标文件不存在: ${resource.toString()}`, FileSystemProviderErrorCode.FileNotFound);
    }
    if (!expectedRevision) {
      if (existsBeforeWrite) {
        await this.readBytes(resource);
        expectedRevision = this.revisions.get(this.key(resource));
      } else {
        await this.createEntry(resource, 'file');
        // create 回执确认的是空文件；不能重读并把随后他人的正文当成本次保存基线。
        expectedRevision = encodeHex(VSBuffer.wrap(sha256(new Uint8Array())));
        this.revisions.set(this.key(resource), expectedRevision);
        if (bytes.byteLength === 0) return;
      }
    }
    if (!expectedRevision) throw new Error('保存前未能建立后端 revision');
    try {
      const file = await this.content(resource, { method: 'PUT', body: JSON.stringify({ content: new TextDecoder('utf-8', { fatal: true }).decode(bytes), expected_revision: expectedRevision }) });
      this.revisions.set(this.key(resource), file.revision);
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
    const key = this.key(workspaceFileUri(source, source.root, source.root));
    let watch = this.watches.get(key);
    if (!watch) {
      watch = { controller: new AbortController(), resources: new Map() };
      this.watches.set(key, watch);
      const active = watch;
      void this.watchRoot(source, active).catch(error => {
        if (!active.controller.signal.aborted) this.watchErrors.fire(`文件监听失败：${error instanceof Error ? error.message : String(error)}`);
      });
    }
    const name = this.key(resource), entry = watch.resources.get(name) ?? { resource, references: 0 };
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
    const workspaceKey = gatewayWorkspaceKey(source);
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
          if (!watched.some(entry => this.identity.extUri.isEqualOrParent(resource, entry.resource) || this.identity.extUri.isEqualOrParent(entry.resource, resource))) return [];
          const type = change.kind === 'create' ? FileChangeType.ADDED : change.kind === 'delete' ? FileChangeType.DELETED : FileChangeType.UPDATED;
          return [{ resource, type }];
        });
        if (changes.length) this.changes.fire(changes);
      },
    });
    if (!signal.aborted) throw new Error('文件监听流意外结束');
  }
  // 此 provider 读取原字节并沿原文本合同保存；目录管理复用会话文件树的 operation receipt 链路。
  async mkdir(resource: URI): Promise<void> { await this.createEntry(resource, 'directory'); }
  async readdir(): Promise<[string, FileType][]> { throw createFileSystemProviderError('请在 Thread 的文件资源中浏览目录', FileSystemProviderErrorCode.FileNotADirectory); }
  async delete(): Promise<void> { throw new Error('文本资源不支持直接删除后端文件'); }
  async rename(): Promise<void> { throw new Error('文本资源不支持直接重命名后端文件'); }
}

export function installBackendFiles(files: IFileService, identity: IUriIdentityService): void {
  files.registerProvider(scheme, new WorkspaceFiles(identity));
}
