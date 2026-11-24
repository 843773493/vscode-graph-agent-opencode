import { t } from '../../../shared/core/i18n/index.js';
import { VSBuffer } from '../../upstream/src/vs/base/common/buffer.js';
import { Emitter, Event } from '../../upstream/src/vs/base/common/event.js';
import { CancellationError, getErrorMessage } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { parse } from '../../upstream/src/vs/base/common/json.js';
import { dirname, resolvePath } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IModelService } from '../../upstream/src/vs/editor/common/services/model.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IFileService, FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError,
  type IFileChange, type IFileSystemProviderWithFileReadWriteCapability, type IFileWriteOptions, type IStat } from '../../upstream/src/vs/platform/files/common/files.js';
import { IStorageService, StorageScope, StorageTarget } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { ILabelService } from '../../upstream/src/vs/platform/label/common/label.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../../upstream/src/vs/workbench/common/contributions.js';
import { ITextFileService, TextFileEditorModelState } from '../../upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import { IFilesConfigurationService } from '../../upstream/src/vs/workbench/services/filesConfiguration/common/filesConfigurationService.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { GatewayRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { PluginConfigurationSchemasDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { ConfigurationSourceOwner, sourceBaseline, sourceStatus, sourceFanoutStatus, type SourceDocument, type SourceBaseline } from './sourceOwner.js';
import { workspaceContext } from '../workspaceContext.js';

export const configSourceScheme = 'boxteam-config-source';

/** 编辑器身份，不是业务资源 grammar；规范来源 URI 仍只取 owner 的 sources 回执。 */
export function configSourceEditorUri(target: GatewayRequestTarget, sourceKey: string): URI {
  return URI.from({ scheme: configSourceScheme, authority: target.gateway_id,
    path: '/' + [target.workspace_id ? 'workspace' : 'gateway', target.workspace_id ?? '-', sourceKey + '.jsonc'].join('/') });
}

export function sourceLocation(resource: URI): { target: GatewayRequestTarget; sourceKey: string } {
  const parts = resource.path.slice(1).split('/');
  if (resource.scheme !== configSourceScheme || !resource.authority || resource.query || resource.fragment || parts.length !== 3
    || !['gateway', 'workspace'].includes(parts[0]) || !parts[1] || !parts[2].endsWith('.jsonc') || parts[2] === '.jsonc'
    || parts[0] === 'gateway' && parts[1] !== '-') throw new Error(t('settings.editor.editorIdentityMissing'));
  return { target: { gateway_id: resource.authority, ...(parts[0] === 'workspace' ? { workspace_id: parts[1] } : {}) }, sourceKey: parts[2].slice(0, -6) };
}

export interface ConfigSourceBackup { baseline?: SourceBaseline }
interface SourceSubmission { key: string; gatewayConnectionId?: string }
interface SchemaObserver { next: (schemas: PluginConfigurationSchemasDTO) => void; error: (error: Error) => void }
interface SchemaStream {
  route?: string; abort: AbortController; observers: Set<SchemaObserver>;
  current?: PluginConfigurationSchemasDTO; failure?: Error;
}
interface SourceEdit extends ConfigSourceBackup {
  owner: ConfigurationSourceOwner;
  sourceKey: string;
  submission?: SourceSubmission;
  saving?: boolean;
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
  private readonly changed = this._register(new Emitter<URI>());
  readonly onDidChangeSource = this.changed.event;
  constructor(private readonly dialogs: IDialogService, private readonly notifications: INotificationService,
    private readonly storage: IStorageService) { super(); }

  private submissionStorageKey(resource: URI): string { return 'boxteam.configurationSource.submission:' + resource.toString(); }
  /** 只保存原提交的查询身份；不保存请求正文、密码或客户端秘密摘要。 */
  private async setSubmission(resource: URI, edit: SourceEdit, submission?: SourceSubmission): Promise<void> {
    edit.submission = submission;
    this.storage.store(this.submissionStorageKey(resource), submission, StorageScope.WORKSPACE, StorageTarget.MACHINE);
    this.changed.fire(resource);
    await this.storage.flush();
  }
  pendingSubmission(resource: URI): string | undefined { return this.edit(resource).submission?.key; }
  async preflight(resource: URI, jsonc: string, signal?: AbortSignal, modelRef?: { provider_id: string; model_id?: string }) {
    const edit = this.edit(resource);
    if (!edit.baseline) throw new Error(t('settings.editor.restoredBaselineMissing'));
    return edit.owner.preflight(edit.owner.intent(edit.sourceKey, jsonc, edit.baseline), signal, modelRef);
  }


  private edit(resource: URI): SourceEdit {
    const key = resource.toString();
    let edit = this.edits.get(key);
    if (!edit) {
      const location = sourceLocation(resource);
      edit = { owner: new ConfigurationSourceOwner(location.target), sourceKey: location.sourceKey,
        submission: this.storage.getObject<SourceSubmission>(this.submissionStorageKey(resource), StorageScope.WORKSPACE) };
      this.edits.set(key, edit);
    }
    return edit;
  }
  prepareEditor(target: GatewayRequestTarget, sourceKey: string, document: SourceDocument): URI {
    const resource = configSourceEditorUri(target, sourceKey), edit = this.edit(resource);
    // 显式重选只切换后续传输，保留当前草稿、CAS 和原提交身份。
    if (edit.owner.target.gateway_connection_id !== target.gateway_connection_id) edit.owner = new ConfigurationSourceOwner(target);
    if (!edit.document) {
      edit.initial = document;
      this.update(resource, edit, document);
    }
    return resource;
  }
  currentDocument(resource: URI): SourceDocument {
    const document = this.edit(resource).document;
    if (!document) throw new Error(t('settings.editor.sourceNotRead'));
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
    return { baseline: edit?.baseline };
  }
  async restore(resource: URI, backup: ConfigSourceBackup | undefined): Promise<void> {
    const edit = this.edit(resource);
    edit.baseline = backup?.baseline;
    // 原文本备份只恢复草稿和 CAS；先取得来源声明，再创建模型供相对 schema 与表单消费。
    await this.document(resource);
  }
  private update(resource: URI, edit: SourceEdit, document: SourceDocument): SourceDocument {
    edit.document = document;
    this.changed.fire(resource);
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
    const document = edit.submission ? await this.query(resource) : edit.initial ?? await owner.read(edit.sourceKey);
    edit.initial = undefined;
    this.update(resource, edit, document);
    edit.baseline = sourceBaseline(document);
    return VSBuffer.fromString(document.jsonc).buffer;
  }
  async query(resource: URI): Promise<SourceDocument> {
    const edit = this.edit(resource);
    const submission = edit.submission, key = submission?.key;
    const owner = submission ? new ConfigurationSourceOwner({ ...edit.owner.target, gateway_connection_id: submission.gatewayConnectionId }) : edit.owner;
    const document = await owner.read(edit.sourceKey, key);
    this.update(resource, edit, document);
    // 查不到回执仍是未知，不能因重开编辑器便换键提交。
    if (key && document.edit && (document.edit.source_saved || ['rejected', 'discarded', 'conflict'].includes(document.edit.state))) {
      await this.setSubmission(resource, edit);
      this.notifications.info(sourceStatus(document));
      if (document.edit.source_saved && document.edit.file_digest !== document.file_digest) {
        this.notifications.warn(t('settings.editor.savedThenChanged'));
      }
    }
    return document;
  }
  async writeFile(resource: URI, content: Uint8Array, options: IFileWriteOptions): Promise<void> {
    if (this.schemaDocument(resource)) throw createFileSystemProviderError(t('settings.editor.schemaReadOnly'), FileSystemProviderErrorCode.NoPermissions);
    if (options.append || options.atomic) throw new Error(t('settings.editor.appendUnsupported'));
    await this.saveSource(resource, VSBuffer.wrap(content).toString());
  }
  private async saveSource(resource: URI, jsonc: string): Promise<void> {
    // 每次操作捕获原路由；显式重选连接只影响后续操作，资源目标始终来自 URI。
    const edit = this.edit(resource), owner = edit.owner;
    if (edit.saving) throw new Error(t('configuration.entities.saveInProgress'));
    edit.saving = true;
    try {
    if (edit.submission) {
      const original = await this.query(resource);
      if (edit.submission) throw new Error(t('settings.editor.originalSaveUnknown', edit.submission.key));
      // 查询只确认旧提交；当前草稿仍须显式重读／重新预检，不能被当作已经保存。
      if (original.edit?.source_saved) throw new Error(t('settings.editor.savedDraftDifferent'));
    }
    let document = edit.document;
    if (!document) {
      document = await owner.read(edit.sourceKey);
      edit.document = document;
    }
    if (!document.writable) throw createFileSystemProviderError(t('settings.editor.selectedReadOnly'), FileSystemProviderErrorCode.NoPermissions);
    if (!edit.baseline) throw new Error(t('settings.editor.restoredBaselineMissing'));
    const intent = owner.intent(edit.sourceKey, jsonc, edit.baseline);
    let saving = false;
    try {
      const preflight = await owner.preflight(intent);
      const accepted = await this.dialogs.confirm({ type: 'question', message: t('settings.editor.confirmSourceSave'), primaryButton: t('settings.editor.saveSource'),
        detail: t('settings.editor.sourceSaveDetail', owner.target.gateway_id, owner.target.workspace_id ? t('settings.editor.workspaceSuffix') + owner.target.workspace_id : ' · Gateway', edit.sourceKey, preflight.changed_paths.join(t('plugins.listSeparator')) || t('settings.editor.formatOrShadowed'), sourceFanoutStatus(preflight.document!)) });
      if (!accepted.confirmed) throw new CancellationError();
      await this.setSubmission(resource, edit, { key: intent.idempotency_key, gatewayConnectionId: owner.target.gateway_connection_id });
      saving = true;
      const saved = await owner.save(intent);
      this.update(resource, edit, saved);
      if (saved.edit && (saved.edit.source_saved || ['rejected', 'discarded', 'conflict'].includes(saved.edit.state))) await this.setSubmission(resource, edit);
      if (!saved.edit?.source_saved) throw new Error(t('settings.editor.sourceNotSaved', sourceStatus(saved)));
      edit.baseline = sourceBaseline(saved);
      if (!saved.edit.layer_imported) this.notifications.error(sourceStatus(saved));
      else this.notifications.info(sourceStatus(saved));
      return;
    } catch (error) {
      if (error instanceof CancellationError && !saving) {
        await this.setSubmission(resource, edit);
        throw error;
      }
      // PUT 的网络或服务端错误可能已经提交；只沿原键查询，后续保存也不能换键重放。
      const uncertain = saving && !(error instanceof HttpRequestError && error.status >= 400 && error.status < 500);
      if (!uncertain) await this.setSubmission(resource, edit);
      const latest = await owner.read(edit.sourceKey, saving ? intent.idempotency_key : undefined).catch(readError => {
        throw new AggregateError([error, readError], uncertain ? t('settings.editor.unknownAndReadFailed', intent.idempotency_key) : t('settings.editor.changeAndReadFailed'));
      });
      this.update(resource, edit, latest);
      if (uncertain) {
        if (!latest.edit) throw new Error(t('settings.editor.unknownNotResubmitted', intent.idempotency_key, getErrorMessage(error)));
        if (latest.edit.source_saved || ['rejected', 'discarded', 'conflict'].includes(latest.edit.state)) await this.setSubmission(resource, edit);
        if (latest.edit.source_saved) {
          if (latest.edit.file_digest !== latest.file_digest) {
            edit.baseline = sourceBaseline(latest);
            throw new Error(t('settings.editor.savedThenChanged'));
          }
          edit.baseline = sourceBaseline(latest);
          this.notifications.info(sourceStatus(latest)); return;
        }
      }
      throw new Error(t('settings.editor.changeFailedDraftPreserved', getErrorMessage(error)));
    }
    } finally { edit.saving = false; }
  }
  async rebase(resource: URI): Promise<void> {
    const edit = this.edit(resource);
    if (edit.submission) throw new Error(t('settings.editor.unresolvedNoPreflight'));
    const owner = edit.owner;
    const latest = await owner.read(edit.sourceKey);
    const accepted = await this.dialogs.confirm({ type: 'warning', message: t('settings.editor.confirmRebase'), primaryButton: t('settings.editor.useLatestBaseline'),
      detail: t('settings.editor.rebaseDetail', sourceStatus(latest)) });
    if (!accepted.confirmed) return;
    edit.baseline = sourceBaseline(latest); this.update(resource, edit, latest);
    this.notifications.info(t('settings.editor.rebaseCompleted'));
  }
  watch(): IDisposable { return Disposable.None; }
  async mkdir(): Promise<void> { throw new Error(t('settings.editor.mkdirUnsupported')); }
  async readdir(): Promise<[string, FileType][]> { throw new Error(t('settings.editor.chooseSourceDirectory')); }
  async delete(): Promise<void> { throw new Error(t('settings.editor.deleteSourceUnsupported')); }
  async rename(): Promise<void> { throw new Error(t('settings.editor.renameUnsupported')); }
}

export function configSourceFiles(files: IFileService): ConfigurationSourceFiles {
  const provider = files.getProvider(configSourceScheme);
  if (!(provider instanceof ConfigurationSourceFiles)) throw new Error(t('settings.editor.editorNotReady'));
  return provider;
}
export function configSourceBackup(files: IFileService, resource: URI): ConfigSourceBackup | undefined {
  return resource.scheme === configSourceScheme ? configSourceFiles(files).backup(resource) : undefined;
}
export async function restoreConfigSourceBackup(files: IFileService, resource: URI, backup?: ConfigSourceBackup): Promise<void> {
  if (resource.scheme === configSourceScheme) await configSourceFiles(files).restore(resource, backup);
}

class ConfigurationSourceEditors extends Disposable {
  constructor(@IFileService files: IFileService, @IDialogService dialogs: IDialogService, @INotificationService notifications: INotificationService,
    @IModelService models: IModelService, @IFilesConfigurationService configuration: IFilesConfigurationService, @IStorageService storage: IStorageService,
    @ITextFileService textFiles: ITextFileService, @ILabelService labels: ILabelService) {
    super();
    const provider = this._register(new ConfigurationSourceFiles(dialogs, notifications, storage));
    this._register(files.registerProvider(configSourceScheme, provider));
    // 显示根与资源身份分离；沿用原标签服务刷新 Tab、悬停和面包屑，不改 URI 或保存归属。
    const labelChanged = this._register(new Emitter<void>());
    this._register({ dispose: workspaceContext.subscribe(() => labelChanged.fire()) });
    for (const path of ['/workspace/${workspaceId}', '/gateway/-']) {
      this._register(labels.registerFormatter({
        home: URI.from({ scheme: configSourceScheme, path }),
        onDidChangeFormatting: labelChanged.event,
        formatting: ({ resource, parameters }) => {
          const snapshot = workspaceContext.getSnapshot();
          const workspace = snapshot.workspaces.find(item => item.gateway_id === resource.authority
            && item.workspace_id === parameters.get('workspaceId'));
          const remote = snapshot.workspaces.flatMap(item => item.remote_gateways).find(item => item.gateway_id === resource.authority);
          const target = resource.authority === snapshot.gatewayId ? t('workbench.managementTarget.local') : remote?.name ?? resource.authority;
          return { label: `${target} / ${workspace?.name ?? parameters.get('workspaceId') ?? t('settings.editor.gatewayConfiguration')}`, separator: '/' };
        },
      }));
    }
    this._register(textFiles.files.onDidSave(({ model }) => {
      if (model.resource.scheme !== configSourceScheme || model.textEditorModel?.getValue() === provider.currentDocument(model.resource).jsonc) return;
      // 秘密占位随服务端数组位置归一；只刷新已经保存且未再编辑的原文本模型。
      void (async () => {
        await model.joinState(TextFileEditorModelState.PENDING_SAVE);
        if (!model.isDirty()) await model.resolve({ forceReadFromFile: true });
      })().catch(error => notifications.error(error));
    }));
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
