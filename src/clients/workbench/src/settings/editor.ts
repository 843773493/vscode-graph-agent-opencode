import { t } from '../../../shared/core/i18n/index.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { parse, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { Disposable, DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { ConfigurationTarget, IConfigurationService, type IConfigurationValue } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { ConfigurationScope } from '../../upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ITextModelService } from '../../upstream/src/vs/editor/common/services/resolverService.js';
import { DefaultSettings, Settings2EditorModel, nullRange } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesModels.js';
import type { ISetting, ISettingsGroup } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { ITextFileService, type ITextFileEditorModel, type ITextFileEditorModelManager } from '../../upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { requireGatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { PluginConfigurationSchemasDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { managementGateway, type ManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { configSourceFiles, configSourceEditorUri } from './sourceEditor.js';
import { ConfigurationSourceOwner, type SourceDocument } from './sourceOwner.js';
import { ITargetSettingsContext, type TargetSettingsLocation } from './scope.js';

type Schema = Record<string, unknown>;
interface SourceSetting { path: string[]; schema: Schema }
function object(value: unknown): Schema { return value && typeof value === 'object' && !Array.isArray(value) ? value as Schema : {}; }
function at(value: unknown, path: readonly string[]): unknown {
  for (const key of path) { if (!value || typeof value !== 'object') return undefined; value = (value as Schema)[key]; }
  return value;
}

export function settingsEditorIdentity(location: TargetSettingsLocation): string {
  return JSON.stringify([location.gateway_id, location.workspace_id, location.source_key]);
}
export function settingsHistoryLocation(location: TargetSettingsLocation): Omit<TargetSettingsLocation, 'gateway_connection_id'> {
  const { gateway_connection_id: _route, ...stable } = location;
  return stable;
}
export function parseSettingsLocation(value: unknown): TargetSettingsLocation {
  const location = object(value);
  if (location.version !== 2 || !['gateway_id', 'source_key', 'name'].every(key => typeof location[key] === 'string' && location[key])
    || location.workspace_id !== undefined && (typeof location.workspace_id !== 'string' || !location.workspace_id)
    || location.gateway_connection_id !== undefined && typeof location.gateway_connection_id !== 'string') throw new Error(t('settings.editor.historyMissingTarget'));
  return { version: 2, gateway_id: location.gateway_id as string, source_key: location.source_key as string, name: location.name as string,
    workspace_id: location.workspace_id as string | undefined, gateway_connection_id: location.gateway_connection_id as string | undefined };
}

/** 原目标选择只读来源目录，不再发现或准备 Extension Host 的 profile/view。 */
export async function captureSettingsLocation(parent: IInstantiationService, token: CancellationToken, target: ManagementTarget = managementGateway('settings'),
  explicit?: { workspace_id?: string; source_key?: string }):
Promise<{ location: TargetSettingsLocation; document: SourceDocument } | undefined> {
  const quick = parent.invokeFunction(accessor => accessor.get(IQuickInputService));
  const frozen = { ...target };
  const controller = new AbortController(), cancellation = new CancellationTokenSource(token);
  const cancelled = cancellation.token.onCancellationRequested(() => controller.abort(new CancellationError()));
  const invalidate = () => { controller.abort(new Error(t('settings.editor.sourceTargetChanged'))); cancellation.cancel(); };
  const release = onGatewayTokenInvalidated(0, invalidate);
  const initial = workspaceContext.getSnapshot();
  const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
  try {
    if (token.isCancellationRequested) throw new CancellationError();
    await workspaceContext.refresh();
    controller.signal.throwIfAborted();
    const refreshed = workspaceContext.getSnapshot();
    if (refreshed.error) throw new Error(refreshed.error);
    if (refreshed.loading || !refreshed.gatewayId) throw new Error(t('settings.editor.targetsNotRefreshed'));
    const workspaces = refreshed.workspaces;
    const scopes = [{ label: t('settings.editor.gatewayConfiguration'), description: frozen.name, detail: frozen.gateway_id, workspaceId: undefined as string | undefined },
      ...workspaces.filter(workspace => workspace.gateway_id === frozen.gateway_id).map(workspace => ({ label: workspace.name,
        description: t('settings.editor.workspacePrefix') + workspace.status, detail: workspace.workspace_id, workspaceId: workspace.workspace_id }))];
    const scope = explicit ? scopes.find(scope => scope.workspaceId === explicit.workspace_id) : await quick.pick(scopes,
      { title: t('settings.editor.chooseTarget'), prompt: `Gateway ${frozen.gateway_id}`, placeHolder: t('settings.editor.readSourceBoundary') }, cancellation.token);
    controller.signal.throwIfAborted();
    if (explicit && !scope) throw new Error(t('workbench.settings.targetOffline'));
    if (!scope) return;
    if (scope.workspaceId) {
      const workspace = requireGatewayWorkspaceRequestTarget(workspaces, { gatewayId: frozen.gateway_id, workspaceId: scope.workspaceId });
      if (workspace.status !== 'ready') throw new Error(t('settings.editor.workspaceUnavailable', workspace.name, workspace.status));
    }
    const owner = new ConfigurationSourceOwner({ gateway_id: frozen.gateway_id, gateway_connection_id: frozen.gateway_connection_id, workspace_id: scope.workspaceId });
    const listed = await owner.list(controller.signal);
    controller.signal.throwIfAborted();
    const sources = listed.sources.filter(source => source.source_key).map(source => ({ label: source.source_key!,
      description: t('settings.editor.layerPresencePriority', source.layer, source.presence, source.precedence), detail: source.uri ?? undefined }));
    const selected = explicit?.source_key ? sources.find(source => source.label === explicit.source_key) : await quick.pick(sources,
      { title: t('settings.editor.chooseFormalSource'), prompt: `${frozen.gateway_id} · ${scope.label}`, placeHolder: t('settings.editor.sourceAccessBoundary') }, cancellation.token);
    controller.signal.throwIfAborted();
    if (explicit?.source_key && !selected) throw new Error(t('settings.editor.selectedSourceIdentityMissing'));
    if (!selected) return;
    const document = await owner.read(selected.label, undefined, controller.signal);
    controller.signal.throwIfAborted();
    return { location: { ...owner.target, version: 2, source_key: selected.label, name: scope.label }, document };
  } finally { release(); releaseContext(); cancelled.dispose(); cancellation.dispose(); controller.abort(); }
}

/** 原 Settings2EditorModel 的目标局部 schema 投影；不注册全局业务配置。 */
class SourceDefaults extends DefaultSettings {
  constructor(private readonly source: SourceSettings, ...args: ConstructorParameters<typeof DefaultSettings>) { super(...args); }
  override getSettingsGroups(): ISettingsGroup[] { return [{ id: 'mostCommonlyUsed', title: t('settings.editor.commonlyUsed'), sections: [], range: nullRange, titleRange: nullRange }, ...this.source.groups]; }
  override getRegisteredGroups(): ISettingsGroup[] { return this.source.groups; }
}

export class SourceSettings extends Disposable {
  private readonly changed = this._register(new Emitter<boolean>());
  readonly onDidChange = this.changed.event;
  private readonly fields = new Map<string, SourceSetting>();
  readonly groups: ISettingsGroup[] = [];
  readonly resource: URI;
  constructor(readonly location: TargetSettingsLocation, readonly model: ITextFileEditorModel, private readonly files: IFileService,
    private schemas: PluginConfigurationSchemasDTO | undefined, textFiles: ITextFileEditorModelManager) {
    super();
    this.resource = configSourceEditorUri(location, location.source_key);
    this.buildFields();
    this._register(configSourceFiles(files).onDidChangeSource(resource => { if (resource.toString() === this.resource.toString()) this.changed.fire(false); }));
    this._register(model.onDidChangeContent(() => this.changed.fire(false)));
    const reload = () => { this.buildFields(); this.changed.fire(true); };
    // 声明由安装事件更新；正式保存／还原只重建当前绑定的字段，不再补读库存。
    this._register(model.onDidSave(reload));
    this._register(textFiles.onDidRevert(reverted => { if (reverted === model) reload(); }));
    this._register(model.onDidChangeDirty(() => this.changed.fire(false)));
  }
  replaceSchemas(schemas: PluginConfigurationSchemasDTO | undefined): void {
    this.schemas = schemas;
    this.buildFields();
    this.changed.fire(true);
  }
  get document(): SourceDocument { return configSourceFiles(this.files).currentDocument(this.resource); }
  get pendingSubmission(): string | undefined { return configSourceFiles(this.files).pendingSubmission(this.resource); }
  async querySubmission(): Promise<void> {
    const version = this.model.textEditorModel!.getVersionId();
    await configSourceFiles(this.files).query(this.resource);
    if (this.pendingSubmission) throw new Error(t('settings.editor.originalSaveUnknown', this.pendingSubmission));
    if (!this.model.isDirty() && this.model.textEditorModel!.getVersionId() === version) await this.model.revert();
    this.changed.fire(false);
  }
  /** 密码仅构造瞬时请求，绝不进入文本模型或撤销／备份。 */
  async saveProviderSecret(id: string, password: string, version: number, checkTarget: () => void): Promise<void> {
    const check = () => {
      checkTarget();
      if (this.model.isDirty() || this.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.secretDraftChanged'));
    };
    check();
    const providers = at(this.readDraft(), ['llm', 'providers']);
    const index = Array.isArray(providers) ? providers.findIndex(value => object(value).id === id) : -1;
    if (index < 0) throw new Error(t('configuration.entities.definitionChanged', id));
    const original = this.model.textEditorModel!.getValue();
    const jsonc = applyEdits(original, setProperty(original, ['llm', 'providers', index, 'api_key'], password,
      { insertSpaces: true, tabSize: 2, eol: original.includes('\r\n') ? '\r\n' : '\n' }));
    try { await configSourceFiles(this.files).saveTransient(this.resource, jsonc, check); }
    finally {
      // 未知提交仍留原键；确定结果后只刷新未被后续编辑的干净模型，冲突也能显式重试。
      if (!this.pendingSubmission && !this.model.isDirty() && this.model.textEditorModel!.getVersionId() === version) await this.model.revert();
      this.changed.fire(false);
    }
  }
  unboundPluginParameters(): { packageId: string; pluginId: string; installation: 'declared' | 'unavailable' | 'unknown' }[] | undefined {
    const errors: ParseError[] = [];
    const payload: unknown = parse(this.model.textEditorModel!.getValue(), errors);
    if (errors.length) return undefined;
    const values = object(at(payload, ['plugins', 'configuration', 'values']));
    const bindings = at(this.document.source_effective, ['plugins', 'runtime', 'entries']);
    const bound = new Set(Array.isArray(bindings) ? bindings.flatMap(binding => {
      const entry = object(binding);
      return typeof entry.package_id === 'string' && typeof entry.plugin_id === 'string'
        ? [JSON.stringify([entry.package_id, entry.plugin_id])] : [];
    }) : []);
    return Object.entries(values).flatMap(([packageId, plugins]) => Object.entries(object(plugins)).flatMap(([pluginId, parameters]) => {
      if (!Object.keys(object(parameters)).length || bound.has(JSON.stringify([packageId, pluginId]))) return [];
      const installation = this.schemas === undefined ? 'unknown'
        : this.schemas.plugins.some(plugin => plugin.package_id === packageId && plugin.plugin_id === pluginId) ? 'declared' : 'unavailable';
      return [{ packageId, pluginId, installation }];
    }));
  }
  createModel(parent: IInstantiationService): Settings2EditorModel {
    const defaults = parent.invokeFunction(accessor => new SourceDefaults(this, [], ConfigurationTarget.USER_REMOTE, accessor.get(IConfigurationService)));
    this._register(defaults);
    const model = parent.createInstance(Settings2EditorModel, defaults);
    return model;
  }
  inspect(key: string, language?: string): IConfigurationValue<unknown> {
    const field = this.fields.get(key);
    if (!field || language) return {};
    const errors: ParseError[] = [], payload: unknown = parse(this.model.textEditorModel!.getValue(), errors);
    if (errors.length) throw new Error(t('settings.editor.invalidDraftForForm'));
    const local = at(payload, field.path), effective = at(this.document.source_effective, field.path);
    const selected = this.document.layers.find(layer => layer.source_key === this.location.source_key);
    let inherited = field.schema.default;
    for (const layer of this.document.layers.filter(layer => selected && layer.precedence < selected.precedence).sort((a, b) => a.precedence - b.precedence)) {
      const value = at(layer.payload, field.path);
      if (value !== undefined) inherited = value;
    }
    return { defaultValue: inherited, userRemoteValue: local, userRemote: { value: local },
      value: local !== undefined ? local : effective, default: { value: inherited } };
  }
  edit(key: string, value: unknown, language?: string): void {
    if (language) throw new Error(t('settings.editor.noLanguageOverride'));
    const field = this.fields.get(key);
    if (!field) throw new Error(t('settings.editor.fieldOutsideSchema'));
    if (!this.document.writable) throw new Error(t('settings.editor.currentSourceReadOnly'));
    if (field.schema.writeOnly === true) throw new Error(t('settings.editor.useProtectedSecretInput'));
    this.editPath(field.path, value);
  }
  readDraft(): Record<string, unknown> {
    const errors: ParseError[] = [], payload: unknown = parse(this.model.textEditorModel!.getValue(), errors);
    if (errors.length || !payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error(t('settings.editor.invalidDraftForForm'));
    return payload as Record<string, unknown>;
  }
  /** 实体表单与原设置控件只修改同一 JSONC 文本模型，保存继续交原来源 writer。 */
  editPath(path: (string | number)[], value: unknown): void {
    if (!this.document.writable) throw new Error(t('settings.editor.currentSourceReadOnly'));
    this.readDraft();
    const text = this.model.textEditorModel!, source = text.getValue();
    const edits = setProperty(source, path, value, { insertSpaces: true, tabSize: 2, eol: source.includes('\r\n') ? '\r\n' : '\n' });
    text.pushEditOperations(null, [{ range: text.getFullModelRange(), text: applyEdits(source, edits) }], () => null);
  }
  private buildFields(): void {
    this.fields.clear();
    this.groups.length = 0;
    const root = this.document.json_schema;
    if (!root) throw new Error(t('settings.editor.schemaMissing'));
    const visit = (schema: Schema, path: string[], fields: ISetting[], schemaRoot: Schema) => {
      if (typeof schema.$ref === 'string' && schema.$ref.startsWith('#/')) schema = object(at(schemaRoot, schema.$ref.slice(2).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))));
      const properties = object(schema.properties);
      if (schema.writeOnly !== true && Object.keys(properties).length) {
        for (const [name, child] of Object.entries(properties)) visit(object(child), [...path, name], fields, schemaRoot);
        return;
      }
      if (!path.length || schema.const !== undefined) return;
      // 点分键只作原设置树身份；保留真实 JSON 路径，避免包 ID 中的点与层级碰撞。
      const key = path.map(part => encodeURIComponent(part).replaceAll('.', '%2E')).join('.'), secret = schema.writeOnly === true;
      const setting: ISetting = { key, title: typeof schema.title === 'string' ? schema.title : path.at(-1), value: schema.default,
        description: [typeof schema.description === 'string' ? schema.description : '', ...(secret ? [t('settings.editor.secretInputDescription')] : [])],
        descriptionRanges: [], range: nullRange, keyRange: nullRange, valueRange: nullRange, overrides: [], scope: ConfigurationScope.MACHINE,
        type: secret ? undefined : schema.type as string | string[] | undefined, enum: schema.enum as string[] | undefined,
        arrayItemType: typeof object(schema.items).type === 'string' ? object(schema.items).type as string : undefined };
      this.fields.set(key, { path, schema }); fields.push(setting);
    };
    const gatewayGroups: Record<string, string> = {
      logging: 'settings.editor.groupLogging',
      telemetry: 'settings.editor.groupTelemetry',
      statistics: 'settings.editor.groupStatistics',
    };
    for (const [name, candidate] of Object.entries(object(root.properties))) {
      if (['$schema', 'config_version'].includes(name)) continue;
      const settings: ISetting[] = []; visit(object(candidate), [name], settings, root);
      if (settings.length) this.groups.push({ id: name,
        title: !this.location.workspace_id && gatewayGroups[name] ? t(gatewayGroups[name]) : name,
        sections: [{ settings }], range: nullRange, titleRange: nullRange });
    }
    const bindings = at(this.document.source_effective, ['plugins', 'runtime', 'entries']);
    for (const plugin of this.schemas?.plugins ?? []) {
      if (!Array.isArray(bindings) || !bindings.some(binding => binding?.package_id === plugin.package_id && binding?.plugin_id === plugin.plugin_id && binding?.package_version === plugin.package_version)) continue;
      if (!plugin.configuration_schema) throw new Error(t('settings.editor.manifestSchemaMissing'));
      const settings: ISetting[] = [];
      visit(plugin.configuration_schema, ['plugins', 'configuration', 'values', plugin.package_id, plugin.plugin_id], settings, plugin.configuration_schema);
      for (const setting of settings) setting.extensionInfo = { id: `${plugin.package_id}/${plugin.plugin_id}`, displayName: plugin.name };
      this.groups.push({ id: `${plugin.package_id}/${plugin.plugin_id}`, title: `${plugin.name} · ${plugin.package_version}`, sections: [{ settings }], range: nullRange, titleRange: nullRange,
        extensionInfo: { id: `${plugin.package_id}/${plugin.plugin_id}`, displayName: plugin.name } });
    }
  }
}


export async function prepareSettingsPane(parent: IInstantiationService, location: TargetSettingsLocation,
  document: SourceDocument | undefined, lifetime: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
  const controller = new AbortController(), cancel = token.onCancellationRequested(() => controller.abort(new CancellationError()));
  const entry = workspaceContext.getSnapshot().gatewayId;
  const invalidate = () => controller.abort(new CancellationError());
  const auth = onGatewayTokenInvalidated(0, invalidate), context = workspaceContext.subscribe(() => { if (entry && workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); });
  try {
    if (token.isCancellationRequested) throw new CancellationError();
    const files = parent.invokeFunction(accessor => accessor.get(IFileService));
    const owner = new ConfigurationSourceOwner(location);
    const loaded = document ?? await owner.read(location.source_key, undefined, controller.signal);
    controller.signal.throwIfAborted();
    const resource = configSourceFiles(files).prepareEditor(location, location.source_key, loaded);
    const reference = await parent.invokeFunction(accessor => accessor.get(ITextModelService)).createModelReference(resource);
    lifetime.add(reference);
    controller.signal.throwIfAborted();
    const textFiles = parent.invokeFunction(accessor => accessor.get(ITextFileService)).files;
    const model = textFiles.get(resource);
    if (!model) throw new Error(t('settings.editor.textModelMissing'));
    const notifications = parent.invokeFunction(accessor => accessor.get(INotificationService));
    const initial = Promise.withResolvers<void>();
    let source: SourceSettings | undefined, schemas: PluginConfigurationSchemasDTO | undefined;
    const subscription = lifetime.add(configSourceFiles(files).observeConfigurationSchemas(resource, owner, {
      next: value => { schemas = value; source?.replaceSchemas(value); initial.resolve(); },
      error: error => { if (source) { source.replaceSchemas(undefined); notifications.error(error); } else initial.reject(error); },
    }));
    const aborted = () => { subscription.dispose(); initial.reject(new CancellationError()); };
    controller.signal.addEventListener('abort', aborted, { once: true });
    try {
      await initial.promise;
      controller.signal.throwIfAborted();
      source = lifetime.add(new SourceSettings(location, model, files, schemas, textFiles));
    } finally { controller.signal.removeEventListener('abort', aborted); }
    lifetime.add({ dispose: onGatewayTokenInvalidated(0, () => { subscription.dispose(); source?.replaceSchemas(undefined); }) });
    return parent.createChild(new ServiceCollection([ITargetSettingsContext, { _serviceBrand: undefined, target: source }]), lifetime);
  } finally { auth(); context(); cancel.dispose(); controller.abort(); }
}
