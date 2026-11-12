import { VSBuffer } from '../../upstream/src/vs/base/common/buffer.js';
import { Event } from '../../upstream/src/vs/base/common/event.js';
import { CancellationError, getErrorMessage } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { parse } from '../../upstream/src/vs/base/common/json.js';
import { dirname, resolvePath } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IModelService } from '../../upstream/src/vs/editor/common/services/model.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IFileService, FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError,
  type IFileChange, type IFileSystemProviderWithFileReadWriteCapability, type IFileWriteOptions, type IStat } from '../../upstream/src/vs/platform/files/common/files.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../../upstream/src/vs/workbench/common/contributions.js';
import { IFilesConfigurationService } from '../../upstream/src/vs/workbench/services/filesConfiguration/common/filesConfigurationService.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { PluginConfigurationSchemasDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { ConfigurationSourceOwner, sourceBaseline, sourceStatus, type SourceTarget, type SourceDocument, type SourceBaseline } from './sourceOwner.js';

export const configSourceScheme = 'boxteam-config-source';

/** 编辑器身份，不是业务资源 grammar；规范来源 URI 仍只取 owner 的 sources 回执。 */
export function configSourceEditorUri(target: SourceTarget, sourceKey: string): URI {
  return URI.from({ scheme: configSourceScheme, authority: target.gateway_id,
    path: '/' + [target.workspace_id ? 'workspace' : 'gateway', target.workspace_id ?? '-', sourceKey + '.jsonc'].map(encodeURIComponent).join('/') });
}

function sourceLocation(resource: URI): { target: SourceTarget; sourceKey: string } {
  const parts = resource.path.slice(1).split('/').map(decodeURIComponent);
  if (resource.scheme !== configSourceScheme || !resource.authority || resource.query || resource.fragment || parts.length !== 3
    || !['gateway', 'workspace'].includes(parts[0]) || !parts[1] || !parts[2].endsWith('.jsonc') || parts[2] === '.jsonc'
    || parts[0] === 'gateway' && parts[1] !== '-') throw new Error('配置编辑器缺少完整目标或来源身份');
  return { target: { gateway_id: resource.authority, ...(parts[0] === 'workspace' ? { workspace_id: parts[1] } : {}) }, sourceKey: parts[2].slice(0, -6) };
}

export interface ConfigSourceBackup { baseline?: SourceBaseline; submissionKey?: string }
interface SchemaObserver { next: (schemas: PluginConfigurationSchemasDTO) => void; error: (error: Error) => void }
interface SchemaStream {
  route?: string; abort: AbortController; observers: Set<SchemaObserver>;
  current?: PluginConfigurationSchemasDTO; failure?: Error;
}
interface SourceEdit extends ConfigSourceBackup {
  owner: ConfigurationSourceOwner;
  sourceKey: string;
  document?: SourceDocument;
  schemaResource?: URI;
  initial?: SourceDocument;
  loading?: Promise<SourceDocument>;
  schemaStreams?: Set<SchemaStream>;
}

/** 只适配原文本文件生命周期；正文、预检、CAS 和提交事实全部由原配置 owner 提供。 */
class ConfigurationSourceFiles extends Disposable implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  readonly onDidChangeFile = Event.None as Event<readonly IFileChange[]>;
  private readonly edits = new Map<string, SourceEdit>();
  constructor(private readonly dialogs: IDialogService, private readonly notifications: INotificationService) { super(); }

  private edit(resource: URI): SourceEdit {
    const key = resource.toString();
    let edit = this.edits.get(key);
    if (!edit) {
      const location = sourceLocation(resource);
      edit = { owner: new ConfigurationSourceOwner(location.target), sourceKey: location.sourceKey };
      this.edits.set(key, edit);
    }
    return edit;
  }
  prepareEditor(target: SourceTarget, sourceKey: string, document: SourceDocument): URI {
    const resource = configSourceEditorUri(target, sourceKey), key = resource.toString();
    const current = this.edits.get(key);
    if (current) {
      // 显式重选只切换后续传输，保留当前草稿、CAS 和原提交身份。
      if (current.owner.target.gateway_connection_id !== target.gateway_connection_id) current.owner = new ConfigurationSourceOwner(target);
    } else {
      const edit: SourceEdit = { owner: new ConfigurationSourceOwner(target), sourceKey, initial: document };
      this.edits.set(key, edit); this.update(resource, edit, document);
    }
    return resource;
  }
  currentDocument(resource: URI): SourceDocument {
    const document = this.edit(resource).document;
    if (!document) throw new Error('配置来源尚未读取');
    return document;
  }
  observeConfigurationSchemas(resource: URI, owner: ConfigurationSourceOwner, observer: SchemaObserver): IDisposable {
    const edit = this.edit(resource);
    const streams = edit.schemaStreams ??= new Set<SchemaStream>();
    let stream = [...streams].find(item => item.route === owner.target.gateway_connection_id);
    if (!stream) {
      stream = { route: owner.target.gateway_connection_id, abort: new AbortController(), observers: new Set() };
      streams.add(stream);
      const active = stream;
      void owner.watchConfigurationSchemas(active.abort.signal, schemas => {
        if (active.abort.signal.aborted) return;
        active.current = schemas;
        for (const listener of active.observers) listener.next(schemas);
      }).catch(error => {
        if (active.abort.signal.aborted) return;
        active.current = undefined;
        active.failure = new Error(getErrorMessage(error), { cause: error });
        for (const listener of active.observers) listener.error(active.failure);
      });
    }
    stream.observers.add(observer);
    if (stream.current) observer.next(stream.current);
    if (stream.failure) observer.error(stream.failure);
    const active = stream;
    return { dispose: () => {
      active.observers.delete(observer);
      if (!active.observers.size) { active.abort.abort(); streams.delete(active); }
    } };
  }
  release(resource: URI): void {
    const key = resource.toString();
    for (const stream of this.edits.get(key)?.schemaStreams ?? []) stream.abort.abort();
    this.edits.delete(key);
  }
  override dispose(): void {
    for (const key of this.edits.keys()) this.release(URI.parse(key));
    super.dispose();
  }
  backup(resource: URI): ConfigSourceBackup {
    const edit = this.edits.get(resource.toString());
    return { baseline: edit?.baseline, submissionKey: edit?.submissionKey };
  }
  restore(resource: URI, backup: ConfigSourceBackup | undefined): void {
    const edit = this.edit(resource);
    edit.baseline = backup?.baseline;
    edit.submissionKey = backup?.submissionKey;
  }
  private update(resource: URI, edit: SourceEdit, document: SourceDocument): SourceDocument {
    edit.document = document;
    const value: unknown = parse(document.jsonc);
    const schema = value && typeof value === 'object' && '$schema' in value ? value.$schema : undefined;
    // 相对 schema 仅为同一文档的 UI 投影，正文直接取 owner 回执，不能当另一来源反复请求。
    edit.schemaResource = typeof schema === 'string' && !/^[a-z][a-z0-9+.-]*:/i.test(schema) ? resolvePath(dirname(resource), schema) : undefined;
    return document;
  }
  private schemaDocument(resource: URI): SourceDocument | undefined {
    const entry = [...this.edits].find(([, edit]) => edit.schemaResource?.toString() === resource.toString());
    if (!entry) return undefined;
    return this.edit(URI.parse(entry[0])).document;
  }
  private async document(resource: URI): Promise<SourceDocument> {
    const edit = this.edit(resource);
    if (edit.document) return edit.document;
    const owner = edit.owner;
    edit.loading ??= owner.read(edit.sourceKey).then(document => {
      return this.update(resource, edit, document);
    }).finally(() => { edit.loading = undefined; });
    return edit.loading;
  }
  async stat(resource: URI): Promise<IStat> {
    const schema = this.schemaDocument(resource);
    if (schema) return { type: FileType.File, ctime: 0, mtime: 0, size: VSBuffer.fromString(JSON.stringify(schema.json_schema)).byteLength, permissions: FilePermission.Readonly };
    const document = await this.document(resource);
    // 文件元数据只服务原文本模型；并发写入始终由来源的三个 CAS 基线决定，不另算 hash。
    return { type: FileType.File, ctime: 0, mtime: 0, size: VSBuffer.fromString(document.jsonc).byteLength,
      permissions: document.writable ? undefined : FilePermission.Readonly };
  }
  async readFile(resource: URI): Promise<Uint8Array> {
    const schema = this.schemaDocument(resource);
    if (schema) return VSBuffer.fromString(JSON.stringify(schema.json_schema)).buffer;
    const edit = this.edit(resource), owner = edit.owner;
    if (edit.submissionKey) throw new Error('保存结果待核实，请先查询原来源提交；不能丢弃其身份后重新保存');
    const document = edit.initial ?? await owner.read(edit.sourceKey);
    edit.initial = undefined;
    this.update(resource, edit, document);
    edit.baseline = sourceBaseline(document);
    return VSBuffer.fromString(document.jsonc).buffer;
  }
  async query(resource: URI): Promise<SourceDocument> {
    const edit = this.edit(resource);
    return edit.owner.read(edit.sourceKey, edit.submissionKey);
  }
  async writeFile(resource: URI, content: Uint8Array, options: IFileWriteOptions): Promise<void> {
    if (this.schemaDocument(resource)) throw createFileSystemProviderError('配置 schema 只读', FileSystemProviderErrorCode.NoPermissions);
    if (options.append || options.atomic) throw new Error('配置来源不支持追加或通用原子文件替换，请使用原来源保存');
    // 每次操作捕获原路由；显式重选连接只影响后续操作，资源目标始终来自 URI。
    const edit = this.edit(resource), owner = edit.owner, jsonc = VSBuffer.wrap(content).toString();
    if (edit.submissionKey) {
      const original = await owner.read(edit.sourceKey, edit.submissionKey);
      this.update(resource, edit, original);
      if (!original.edit) throw new Error(`原保存结果仍未知，仅可查询提交 ${edit.submissionKey}，未重发写入`);
      edit.submissionKey = undefined;
      if (original.edit.source_saved) {
        edit.baseline = sourceBaseline(original);
        this.notifications.info(sourceStatus(original));
        if (original.jsonc === jsonc) return;
        throw new Error('原提交已保存；当前草稿与回读正文不同，已保留，请检查后显式再次保存');
      }
    }
    let document = edit.document;
    if (!document) {
      document = await owner.read(edit.sourceKey);
      edit.document = document;
    }
    if (!document.writable) throw createFileSystemProviderError('所选配置来源只读', FileSystemProviderErrorCode.NoPermissions);
    if (!edit.baseline) throw new Error('恢复草稿没有原保存基线，请先使用“重新预检当前设置来源草稿”核对最新来源');
    const intent = owner.intent(edit.sourceKey, jsonc, edit.baseline);
    let saving = false;
    try {
      const preflight = await owner.preflight(intent);
      const accepted = await this.dialogs.confirm({ type: 'question', message: '保存目标配置来源？', primaryButton: '保存来源',
        detail: `${owner.target.gateway_id}${owner.target.workspace_id ? ' · 工作区 ' + owner.target.workspace_id : ' · Gateway'} · ${edit.sourceKey}\n变更：${preflight.changed_paths.join('、') || '来源格式或被遮蔽的覆盖'}\n保存与运行生效分别报告。` });
      if (!accepted.confirmed) throw new CancellationError();
      saving = true; edit.submissionKey = intent.idempotency_key;
      const saved = await owner.save(intent);
      this.update(resource, edit, saved); edit.submissionKey = undefined;
      if (!saved.edit?.source_saved) throw new Error(`来源未保存：${sourceStatus(saved)}`);
      edit.baseline = sourceBaseline(saved);
      if (!saved.edit.layer_imported) this.notifications.error(sourceStatus(saved));
      else this.notifications.info(sourceStatus(saved));
      return;
    } catch (error) {
      if (error instanceof CancellationError) throw error;
      // PUT 的网络或服务端错误可能已经提交；只沿原键查询，后续保存也不能换键重放。
      const uncertain = saving && !(error instanceof HttpRequestError && error.status >= 400 && error.status < 500);
      if (!uncertain) edit.submissionKey = undefined;
      const latest = await owner.read(edit.sourceKey, saving ? intent.idempotency_key : undefined).catch(readError => {
        throw new AggregateError([error, readError], uncertain ? `保存结果未知，保留原提交 ${intent.idempotency_key}；来源回读也失败` : '配置修改失败，草稿已保留；来源回读也失败');
      });
      this.update(resource, edit, latest);
      if (uncertain) {
        if (!latest.edit) throw new Error(`保存结果未知，保留提交 ${intent.idempotency_key}，未重发：${getErrorMessage(error)}`);
        edit.submissionKey = undefined;
        if (latest.edit.source_saved) {
          if (latest.jsonc !== jsonc) {
            edit.baseline = sourceBaseline(latest);
            throw new Error('原提交已保存，但来源已被后续修改；当前草稿仍保留，请检查后显式重新预检');
          }
          edit.baseline = sourceBaseline(latest); this.notifications.info(sourceStatus(latest)); return;
        }
      }
      throw new Error(`配置修改失败，草稿已保留。${getErrorMessage(error)}。如需按最新来源提交，请显式运行“重新预检当前设置来源草稿”`);
    }
  }
  async rebase(resource: URI): Promise<void> {
    const edit = this.edit(resource);
    if (edit.submissionKey) throw new Error('原保存结果待核实，只能先查询原提交；不能重新预检或重发');
    const owner = edit.owner;
    const latest = await owner.read(edit.sourceKey);
    const accepted = await this.dialogs.confirm({ type: 'warning', message: '以最新来源重新预检当前草稿？', primaryButton: '使用最新基线',
      detail: `${sourceStatus(latest)}\n保留编辑器中的完整草稿；再次保存将按最新来源预检。请先核对并发修改，避免用旧草稿覆盖它们。` });
    if (!accepted.confirmed) return;
    edit.baseline = sourceBaseline(latest); this.update(resource, edit, latest);
    this.notifications.info('已保留草稿并更新保存基线；尚未写入，请检查后显式保存');
  }
  watch(): IDisposable { return Disposable.None; }
  async mkdir(): Promise<void> { throw new Error('配置来源不提供目录创建'); }
  async readdir(): Promise<[string, FileType][]> { throw new Error('请从正式设置来源目录选择配置'); }
  async delete(): Promise<void> { throw new Error('请在来源 JSONC 中删除覆盖后保存，不能删除配置来源'); }
  async rename(): Promise<void> { throw new Error('配置来源不能重命名或另存为其它目标'); }
}

export function configSourceFiles(files: IFileService): ConfigurationSourceFiles {
  const provider = files.getProvider(configSourceScheme);
  if (!(provider instanceof ConfigurationSourceFiles)) throw new Error('正式配置来源编辑器尚未初始化');
  return provider;
}
export function configSourceBackup(files: IFileService, resource: URI): ConfigSourceBackup | undefined {
  return resource.scheme === configSourceScheme ? configSourceFiles(files).backup(resource) : undefined;
}
export function restoreConfigSourceBackup(files: IFileService, resource: URI, backup?: ConfigSourceBackup): void {
  if (resource.scheme === configSourceScheme) configSourceFiles(files).restore(resource, backup);
}

class ConfigurationSourceEditors extends Disposable {
  constructor(@IFileService files: IFileService, @IDialogService dialogs: IDialogService, @INotificationService notifications: INotificationService,
    @IModelService models: IModelService, @IFilesConfigurationService configuration: IFilesConfigurationService) {
    super();
    const provider = this._register(new ConfigurationSourceFiles(dialogs, notifications));
    this._register(files.registerProvider(configSourceScheme, provider));
    const autoSave = new Map<string, IDisposable>();
    const added = (resource: URI) => { if (resource.scheme === configSourceScheme) autoSave.set(resource.toString(), configuration.disableAutoSave(resource)); };
    this._register(models.onModelAdded(model => added(model.uri)));
    this._register(models.onModelRemoved(model => {
      if (model.uri.scheme !== configSourceScheme) return;
      autoSave.get(model.uri.toString())?.dispose(); autoSave.delete(model.uri.toString()); provider.release(model.uri);
    }));
    this._register({ dispose: () => { for (const disposable of autoSave.values()) disposable.dispose(); autoSave.clear(); } });
  }
}
registerWorkbenchContribution2('boxteam.configurationSourceEditors', ConfigurationSourceEditors, WorkbenchPhase.BlockRestore);
