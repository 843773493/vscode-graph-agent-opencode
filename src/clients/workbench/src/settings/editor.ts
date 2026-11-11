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
import { onGatewayUserSessionInvalidated, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
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
    || location.gateway_connection_id !== undefined && typeof location.gateway_connection_id !== 'string') throw new Error('设置历史缺少正式来源和目标，请重新选择设置来源');
  return { version: 2, gateway_id: location.gateway_id as string, source_key: location.source_key as string, name: location.name as string,
    workspace_id: location.workspace_id as string | undefined, gateway_connection_id: location.gateway_connection_id as string | undefined };
}

/** 原目标选择只读来源目录，不再发现或准备 Extension Host 的 profile/view。 */
export async function captureSettingsLocation(parent: IInstantiationService, token: CancellationToken, target: ManagementTarget = managementGateway('settings')):
Promise<{ location: TargetSettingsLocation; document: SourceDocument } | undefined> {
  const quick = parent.invokeFunction(accessor => accessor.get(IQuickInputService));
  const initial = workspaceContext.getSnapshot(), frozen = { ...target };
  const controller = new AbortController(), cancellation = new CancellationTokenSource(token);
  const cancelled = cancellation.token.onCancellationRequested(() => controller.abort(new CancellationError()));
  const invalidate = () => { controller.abort(new Error('Gateway 或访问凭据已改变，请重新打开设置来源')); cancellation.cancel(); };
  const release = onGatewayTokenInvalidated(0, invalidate);
  const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
  try {
    if (token.isCancellationRequested) throw new CancellationError();
    const scope = await quick.pick([{ label: 'Gateway 配置', description: frozen.name, detail: frozen.gateway_id, workspaceId: undefined as string | undefined },
      ...initial.workspaces.filter(workspace => workspace.gateway_id === frozen.gateway_id).map(workspace => ({ label: workspace.name,
        description: '工作区 · ' + workspace.status, detail: workspace.workspace_id, workspaceId: workspace.workspace_id }))],
    { title: '选择设置来源的所属目标', prompt: `Gateway ${frozen.gateway_id}`, placeHolder: '读取来源不启动 Backend 或插件；离线来源由原 owner 明确报错' }, cancellation.token);
    controller.signal.throwIfAborted();
    if (!scope) return;
    const owner = new ConfigurationSourceOwner({ gateway_id: frozen.gateway_id, gateway_connection_id: frozen.gateway_connection_id, workspace_id: scope.workspaceId });
    const listed = await owner.list(controller.signal);
    controller.signal.throwIfAborted();
    const selected = await quick.pick(listed.sources.filter(source => source.source_key).map(source => ({ label: source.source_key!,
      description: `${source.layer} · ${source.presence} · 优先级 ${source.precedence}`, detail: source.uri ?? undefined })),
    { title: '选择正式配置来源', prompt: `${frozen.gateway_id} · ${scope.label}`, placeHolder: '内置与只读来源仍可查看；保存仅经原预检与 CAS' }, cancellation.token);
    controller.signal.throwIfAborted();
    if (!selected) return;
    const document = await owner.read(selected.label, undefined, controller.signal);
    controller.signal.throwIfAborted();
    return { location: { ...owner.target, version: 2, source_key: selected.label, name: scope.label }, document };
  } finally { release(); releaseContext(); cancelled.dispose(); cancellation.dispose(); controller.abort(); }
}

/** 原 Settings2EditorModel 的目标局部 schema 投影；不注册全局业务配置。 */
class SourceDefaults extends DefaultSettings {
  constructor(private readonly source: SourceSettings, ...args: ConstructorParameters<typeof DefaultSettings>) { super(...args); }
  override getSettingsGroups(): ISettingsGroup[] { return [{ id: 'mostCommonlyUsed', title: '常用', sections: [], range: nullRange, titleRange: nullRange }, ...this.source.groups]; }
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
    if (errors.length) throw new Error('来源草稿 JSONC 无效，请打开来源修正后继续编辑表单');
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
    if (language) throw new Error('业务配置不支持语言覆盖，请清除 @lang 过滤');
    const field = this.fields.get(key);
    if (!field) throw new Error('设置不属于当前来源 schema');
    if (!this.document.writable) throw new Error('当前配置来源只读');
    if (field.schema.writeOnly === true) throw new Error('秘密参数请使用受保护的插件参数入口');
    const text = this.model.textEditorModel!, source = text.getValue(), errors: ParseError[] = [];
    parse(source, errors);
    if (errors.length) throw new Error('来源草稿 JSONC 无效，未修改正文');
    const edits = setProperty(source, field.path, value, { insertSpaces: true, tabSize: 2, eol: source.includes('\r\n') ? '\r\n' : '\n' });
    text.pushEditOperations(null, [{ range: text.getFullModelRange(), text: applyEdits(source, edits) }], () => null);
  }
  private buildFields(): void {
    this.fields.clear();
    this.groups.length = 0;
    const root = this.document.json_schema;
    if (!root) throw new Error('配置 owner 没有返回来源 schema');
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
        description: [typeof schema.description === 'string' ? schema.description : '', ...(secret ? ['秘密值不回显；请使用“编辑目标插件参数”管理秘密。'] : [])],
        descriptionRanges: [], range: nullRange, keyRange: nullRange, valueRange: nullRange, overrides: [], scope: ConfigurationScope.MACHINE,
        type: secret ? undefined : schema.type as string | string[] | undefined, enum: schema.enum as string[] | undefined,
        arrayItemType: typeof object(schema.items).type === 'string' ? object(schema.items).type as string : undefined };
      this.fields.set(key, { path, schema }); fields.push(setting);
    };
    for (const [name, candidate] of Object.entries(object(root.properties))) {
      if (['$schema', 'config_version'].includes(name)) continue;
      const settings: ISetting[] = []; visit(object(candidate), [name], settings, root);
      if (settings.length) this.groups.push({ id: name, title: name, sections: [{ settings }], range: nullRange, titleRange: nullRange });
    }
    const bindings = at(this.document.source_effective, ['plugins', 'runtime', 'entries']);
    for (const plugin of this.schemas?.plugins ?? []) {
      if (!Array.isArray(bindings) || !bindings.some(binding => binding?.package_id === plugin.package_id && binding?.plugin_id === plugin.plugin_id && binding?.package_version === plugin.package_version)) continue;
      if (!plugin.configuration_schema) throw new Error('安装清单缺少配置 schema');
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
  const auth = onGatewayUserSessionInvalidated(0, invalidate), context = workspaceContext.subscribe(() => { if (entry && workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); });
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
    if (!model) throw new Error('设置来源没有原文本文件模型');
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
    lifetime.add({ dispose: onGatewayUserSessionInvalidated(0, () => { subscription.dispose(); source?.replaceSchemas(undefined); }) });
    return parent.createChild(new ServiceCollection([ITargetSettingsContext, { _serviceBrand: undefined, target: source }]), lifetime);
  } finally { auth(); context(); cancel.dispose(); controller.abort(); }
}
