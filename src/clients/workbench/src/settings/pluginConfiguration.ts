import { t } from '../../../shared/core/i18n/index.js';
import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { parse, parseTree, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import type { PluginBinding, PluginPrivateInputReference, PluginPrivateInputWriteRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gatewayWorkspaceRequestHeaders } from '../../../web/src/api/http.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { createManagementTargetLifecycle, managementGateway, managementTargetDisplayName } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { ConfigurationSourceOwner, sourceBaseline, sourceStatus, type SourceDocument, type SourceTarget } from './sourceOwner.js';

export type Scope = 'gateway' | 'workspace' | 'defaults';
type ConfigDocument = SourceDocument;
type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
type Source = { source_key?: string | null; layer: string; presence: string; uri?: string | null; writable?: boolean };
type PrivateParameter = { pointer: string; schema: Record<string, unknown> };

const targetHeader = (gatewayId: string) => ({ 'X-BoxTeam-Target-Gateway-Id': gatewayId });
const sameBinding = (a: Pick<PluginBinding, 'package_id' | 'plugin_id'>, b: Pick<PluginBinding, 'package_id' | 'plugin_id'>) =>
  a.package_id === b.package_id && a.plugin_id === b.plugin_id;

function updateJsonc(text: string, path: (string | number)[], value: unknown): string {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors);
  if (errors.length || root?.type !== 'object') throw new Error(t('plugins.invalidJsonc'));
  const edits = setProperty(text, path, value, { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' });
  return applyEdits(text, edits);
}

function sourceBindings(document: ConfigDocument, path: string[]): PluginBinding[] {
  return bindingsAt(parse(document.jsonc), path);
}

function bindingJsonc(document: ConfigDocument, path: string[], binding: ConfigurationBinding, enabled: boolean | null | 'preserve'): string {
  const updated = sourceBindings(document, path).slice();
  if (enabled === 'preserve') {
    const current = updated.find(item => sameBinding(item, binding));
    if (!current) throw new Error(t('plugins.bindingSourceMissing'));
    enabled = current.enabled;
  } else {
    const current = bindingsAt(document.source_effective, path).find(item => sameBinding(item, binding));
    if (current && current.package_version !== binding.package_version) {
      throw new Error(t('plugins.bindingVersionChanged', binding.package_id, binding.plugin_id, current.package_version, binding.package_version));
    }
  }
  const index = updated.findIndex(item => sameBinding(item, binding));
  if (enabled === null) { if (index >= 0) updated.splice(index, 1); }
  else if (index < 0) updated.push({ ...binding, enabled });
  else updated[index] = { ...binding, enabled };
  return updateJsonc(document.jsonc, path, updated);
}

function bindingsAt(value: unknown, path: string[]): PluginBinding[] {
  let current: unknown = value;
  for (const key of path) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
  if (current === undefined) return [];
  if (!Array.isArray(current) || current.some(item => !item || typeof item !== 'object')) throw new Error(t('plugins.invalidBindings'));
  return current as PluginBinding[];
}

function privateParameterLocation(document: ConfigDocument, binding: ConfigurationBinding, pointer: string): { path: (string | number)[]; current: unknown } {
  if (!/^(?:\/(?:[^~\/]|~[01])*)+$/.test(pointer)) throw new Error(t('plugins.invalidPointer'));
  const parts = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id,
    ...pointer.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))];
  const path: (string | number)[] = [];
  let current: unknown = parse(document.jsonc), effective: unknown = document.source_effective;
  for (const part of parts) {
    if (Array.isArray(current) || Array.isArray(effective)) {
      if (!Array.isArray(current)) throw new Error(t('plugins.arrayFromLowerSource'));
      if (!/^(0|[1-9][0-9]*)$/.test(part) || Number(part) >= current.length) throw new Error(t('plugins.invalidSecretIndex'));
      const index = Number(part);
      path.push(index); current = current[index]; effective = Array.isArray(effective) ? effective[index] : undefined;
    } else {
      path.push(part);
      current = current && typeof current === 'object' ? (current as Record<string, unknown>)[part] : undefined;
      effective = effective && typeof effective === 'object' ? (effective as Record<string, unknown>)[part] : undefined;
    }
  }
  return { path, current };
}

function privateParameterStatus(document: ConfigDocument, binding: ConfigurationBinding, pointer: string): string {
  const parts = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id,
    ...pointer.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))];
  const at = (root: unknown): unknown => parts.reduce<unknown>((value, part) =>
    value !== null && typeof value === 'object' ? (value as Record<string, unknown>)[part] : undefined, root);
  const state = (value: unknown): string => {
    if (value === undefined) return t('plugins.notConfigured');
    if (value && typeof value === 'object' && 'kind' in value && value.kind === 'plugin_private_file') return t('plugins.privateFileConfigured');
    if (typeof value === 'string' && /^(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|env:[A-Za-z_][A-Za-z0-9_]*)$/.test(value)) return t('plugins.environmentConfigured');
    return t('plugins.configured');
  };
  const path = '/' + parts.map(part => part.replace(/~/g, '~0').replace(/\//g, '~1')).join('/');
  // 归属由原 owner 的 origins 提供；引用对象逐叶标注，数组则整体归属，不能在客户端重新合并来源。
  const sources = [...new Set(document.origins.filter(origin => origin.path === path
    || origin.path.startsWith(path + '/') || path.startsWith(origin.path + '/')).map(origin => origin.source_key))];
  return t('plugins.privateSourceStatus', document.source_key, state(at(parse(document.jsonc))), state(at(document.source_effective)))
    + t('plugins.activeValueStatus', sources.length ? t('plugins.sourcesSuffix', sources.join(t('plugins.listSeparator'))) : '', state(at(document.effective)))
    + t('plugins.privateRevisionsStatus', document.active_revision ?? t('plugins.none'), document.status?.pending_revision ?? t('plugins.none'));
}

async function chooseSource(quickInput: IQuickInputService, sources: Source[], title: string,
  token?: CancellationToken): Promise<Source | undefined> {
  const selected = await quickInput.pick(sources.filter(item => item.source_key).map(source => ({
    label: source.source_key!, description: `${source.layer} · ${source.presence}${source.writable === false ? t('plugins.readOnlySuffix') : ''}`,
    detail: source.uri ?? undefined, source,
  })), { title, placeHolder: t('plugins.chooseConfigurationSource'), canPickMany: false }, token);
  return selected?.source;
}

type SourceSaveResult = { document: SourceDocument } | { error: string };

async function saveSourceEdit(owner: ConfigurationSourceOwner,
  intent: ReturnType<ConfigurationSourceOwner['intent']>): Promise<SourceSaveResult> {
  try {
    return { document: await owner.save(intent) };
  } catch (saveError) {
    const original = saveError instanceof Error ? saveError.message : String(saveError);
    try {
      const document = await owner.read(intent.source_key, intent.idempotency_key);
      if (document.edit) return { document };
      return { error: t('plugins.saveUnknownMissingEdit', intent.idempotency_key, original) };
    } catch (readError) {
      const query = readError instanceof Error ? readError.message : String(readError);
      return { error: t('plugins.saveUnknownQueryFailed', intent.idempotency_key, query, original) };
    }
  }
}

export async function readPluginBindings(scope: Scope, target: SourceTarget): Promise<PluginBinding[]> {
  const owner = new ConfigurationSourceOwner(target);
  // 任一正式来源文档都返回完整的来源合并值；无需先列目录再猜选最高层。
  const document = await owner.read(target.workspace_id ? 'inline' : 'inline:0');
  return bindingsAt(document.source_effective, ['plugins', scope === 'defaults' ? 'workspace_defaults' : 'runtime', 'entries'])
    .map(binding => target.workspace_id ? { ...binding, workspace_id: target.workspace_id } : binding);
}

export async function setPluginBindings(scope: Scope, target: ReturnType<typeof managementGateway>, workspace: GatewayWorkspaceRequestTarget | undefined,
  quickInput: IQuickInputService, bindings: ConfigurationBinding[], enabled: boolean, check: () => void,
  token: CancellationToken, signal: AbortSignal): Promise<{ binding: ConfigurationBinding; configuration: string }[] | undefined> {
  if (scope === 'workspace' && !workspace) throw new Error(t('plugins.noWorkspaceSelected'));
  check();
  const owner = new ConfigurationSourceOwner(scope === 'workspace' ? workspace! : target);
  let selected: Source | undefined;
  try {
    const listed = await owner.list(signal);
    check();
    selected = await chooseSource(quickInput, listed.sources,
      scope === 'workspace' ? t('plugins.chooseWorkspaceSource') : t('plugins.chooseGatewaySource'), token);
    check();
  } catch (error) {
    return bindings.map(binding => ({ binding, configuration: t('plugins.notSubmitted', error instanceof Error ? error.message : String(error)) }));
  }
  if (!selected?.source_key) return;
  const path = scope === 'defaults' ? ['plugins', 'workspace_defaults', 'entries'] : ['plugins', 'runtime', 'entries'];
  const results: { binding: ConfigurationBinding; configuration: string }[] = [];
  for (let index = 0; index < bindings.length; index++) {
    const binding = bindings[index];
    try { check(); }
    catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      results.push(...bindings.slice(index).map(item => ({ binding: item, configuration: t('plugins.notSubmitted', reason) })));
      break;
    }
    let intent: ReturnType<ConfigurationSourceOwner['intent']>;
    try {
      // 每项重新取得来源基线；独立提交保留部分成功，不重试冲突或回滚兄弟插件。
      const document = await owner.read(selected.source_key, undefined, signal);
      check();
      if (!document.writable) throw new Error(t('plugins.sourceReadOnly', selected.source_key));
      const jsonc = bindingJsonc(document, path, binding, enabled);
      if (jsonc === document.jsonc) {
        results.push({ binding, configuration: t('plugins.noChange', sourceStatus(document)) });
        continue;
      }
      intent = owner.intent(selected.source_key, jsonc, sourceBaseline(document));
      await owner.preflight(intent, signal);
      check();
    } catch (error) {
      results.push({ binding, configuration: t('plugins.notSubmitted', error instanceof Error ? error.message : String(error)) });
      continue;
    }
    const saved = await saveSourceEdit(owner, intent);
    results.push({ binding, configuration: 'document' in saved ? sourceStatus(saved.document) : saved.error });
  }
  return results;
}

export async function manageConfig(scope: Scope, gatewayTarget: ReturnType<typeof managementGateway>, workspaceTarget: GatewayWorkspaceRequestTarget | undefined,
  quickInput: IQuickInputService, notification: INotificationService, binding: ConfigurationBinding, enabled: boolean | null | 'preserve',
  editParameters: boolean, privateParameter: PrivateParameter | undefined, check: () => void, token: CancellationToken, signal: AbortSignal): Promise<void> {
  check();
  const path = scope === 'defaults' ? ['plugins', 'workspace_defaults', 'entries'] : ['plugins', 'runtime', 'entries'];
  if (scope === 'workspace' && !workspaceTarget) throw new Error(t('plugins.noWorkspaceSelected'));
  const owner = new ConfigurationSourceOwner(scope === 'workspace' ? workspaceTarget! : gatewayTarget);
  const listed = await owner.list(signal);
  check();
  const selected = await chooseSource(quickInput, listed.sources, scope === 'workspace' ? t('plugins.chooseWorkspaceSource') : t('plugins.chooseGatewaySource'), token);
  check();
  if (!selected) return;
  const source = selected;
  const document = await owner.read(source.source_key!, undefined, signal);
  check();
  if (privateParameter || editParameters) {
    const bound = bindingsAt(document.source_effective, ['plugins', 'runtime', 'entries']).find(item => sameBinding(item, binding));
    if (bound && bound.package_version !== binding.package_version) {
      throw new Error(t('plugins.parameterVersionMismatch', bound.package_version, binding.package_version));
    }
  }
  if (!document.writable) throw new Error(t('plugins.readOnlyNoChanges', source.source_key));
  let jsonc: string;
  let privateInputConfirmed = false;
  if (privateParameter) {
    if (!privateParameter.pointer) {
      const pointer = await quickInput.input({ title: t('plugins.chooseSecretLocation'),
        prompt: t('plugins.secretPointerPrompt'),
        validateInput: async value => {
          try { privateParameterLocation(document, binding, value); return null; }
          catch (error) { return error instanceof Error ? error.message : String(error); }
        } }, token);
      check();
      if (pointer === undefined) return;
      privateParameter = { pointer, schema: {} };
    }
    const { path: parameterPath, current } = privateParameterLocation(document, binding, privateParameter.pointer);
    const configured = current !== undefined;
    const action = await quickInput.pick([{ label: configured ? t('plugins.replaceSecret') : t('plugins.setSecret'), clear: false,
      detail: privateParameterStatus(document, binding, privateParameter.pointer) },
      ...(current !== undefined
        ? [{ label: t('plugins.clearSecretOverride'), detail: t('plugins.clearSecretDetail'), clear: true }] : [])],
    { title: `${binding.package_id}/${binding.plugin_id}${privateParameter.pointer}`,
      placeHolder: t('plugins.secretNoReadback') }, token);
    check();
    if (!action) return;
    if (action.clear && typeof parameterPath.at(-1) === 'number') throw new Error(t('plugins.secretArrayRemoval'));
    let storedValue: unknown;
    if (!action.clear) {
      const stringInput = privateParameter.schema.type === 'string';
      const text = await quickInput.input({ title: stringInput ? t('plugins.enterSecret') : t('plugins.enterSecretJson'), prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        password: true, validateInput: async value => {
          if (stringInput) return null;
          try { JSON.parse(value); return null; } catch { return t('plugins.secretJsonRequired'); }
        } }, token);
      check();
      if (text === undefined) return;
      storedValue = stringInput ? text : JSON.parse(text);
      const accepted = await quickInput.pick([{ label: t('plugins.cancel') }, { label: t('plugins.confirmSecret'), save: true,
        description: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key} · ${binding.package_id}@${binding.package_version}/${binding.plugin_id}${privateParameter.pointer}` }],
      { title: t('plugins.confirmSecretTarget') }, token);
      check();
      if (!accepted?.save) return;
      const writeTarget: PluginPrivateInputWriteRequest = { gateway_id: gatewayTarget.gateway_id, ...(gatewayTarget.gateway_connection_id
        ? { gateway_connection_id: gatewayTarget.gateway_connection_id } : {}),
        ...(workspaceTarget ? { workspace_id: workspaceTarget.workspace_id } : {}), package_id: binding.package_id,
        plugin_id: binding.plugin_id, package_version: binding.package_version, source_key: source.source_key!,
        parameter_pointer: privateParameter.pointer, value: storedValue };
      const privatePath = workspaceTarget
        ? `/api/v1/workspaces/${encodeURIComponent(workspaceTarget.workspace_id)}/config/plugin-private-inputs`
        : '/api/gateway/service-plugins/private-inputs';
      const reference = await gateway.request<PluginPrivateInputReference>(privatePath, {
        method: 'POST', headers: workspaceTarget ? gatewayWorkspaceRequestHeaders(workspaceTarget)
          : targetHeader(gatewayTarget.gateway_id),
        body: JSON.stringify(writeTarget),
      });
      if (reference.kind !== 'plugin_private_file' || reference.gateway_id !== gatewayTarget.gateway_id
        || (reference.workspace_id ?? null) !== (workspaceTarget?.workspace_id ?? null) || reference.package_id !== binding.package_id
        || reference.plugin_id !== binding.plugin_id || reference.parameter_pointer !== privateParameter.pointer
        || !reference.version) throw new Error(t('plugins.privateReceiptMismatch'));
      // 写入已受理；只向冻结的原来源提交其回执，避免留下无主秘密文件。
      privateInputConfirmed = true;
      storedValue = reference;
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, action.clear ? undefined : storedValue);
  } else if (editParameters) {
    if (scope === 'defaults') throw new Error(t('plugins.defaultsBindingOnly'));
    const parameterPath = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id];
    let current: unknown = parse(document.jsonc);
    for (const key of parameterPath) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
    const operation = await quickInput.pick([{ label: t('plugins.editSourceParameters'), remove: false }, { label: t('plugins.deleteParameterOverride'), remove: true }],
      { title: `${binding.package_id}/${binding.plugin_id} · ${binding.package_version}`, placeHolder: t('plugins.deleteParameterDetail') }, token);
    check();
    if (!operation) return;
    let values: unknown;
    if (!operation.remove) {
      const text = await quickInput.input({ title: t('plugins.editJsonObject'), prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        value: JSON.stringify(current ?? {}), validateInput: async value => {
          try { const parsed = JSON.parse(value); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? null : t('plugins.jsonObjectRequired'); }
          catch { return t('plugins.validJsonRequired'); }
        } }, token);
      check();
      if (text === undefined) return;
      values = JSON.parse(text);
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, values);
  } else {
    jsonc = bindingJsonc(document, path, binding, enabled);
  }
  if (jsonc === document.jsonc) return;
  if (!privateInputConfirmed) check();
  const intent = owner.intent(source.source_key!, jsonc, sourceBaseline(document));
  const change = privateParameter ? t('plugins.changeSecret') : editParameters ? t('plugins.changeParameters') : scope === 'defaults'
    ? enabled === 'preserve' ? t('plugins.switchDefaultVersion', binding.package_version)
      : enabled === null ? t('plugins.unbindDefaultTemplate') : enabled ? t('plugins.enableDefaultTemplate') : t('plugins.disableDefaultTemplate')
    : enabled === 'preserve' ? t('plugins.switchBindingVersion', binding.package_version)
      : enabled === null ? t('plugins.unbind') : enabled ? t('plugins.enableAction') : t('plugins.disableAction');
  const confirmation = privateParameter ? t('plugins.secretParameter') : editParameters ? t('plugins.parameters')
    : scope === 'defaults' ? t('plugins.defaultTemplate') : t('plugins.binding');
  let preflight: Awaited<ReturnType<typeof owner.preflight>>;
  try { preflight = await owner.preflight(intent, privateInputConfirmed ? undefined : signal); }
  catch (error) {
    if (!privateInputConfirmed) check();
    notification.error(t('plugins.notSubmitted', error instanceof Error ? error.message : String(error)));
    return;
  }
  if (!privateInputConfirmed) check();
  const accepted = privateInputConfirmed ? { save: true } : await quickInput.pick([{ label: t('plugins.cancel') }, { label: t('plugins.confirmSave', scope === 'workspace' ? t('plugins.workspace') : 'Gateway', confirmation), save: true,
    description: `${binding.package_id}/${binding.plugin_id} · ${change}`, detail: preflight.changed_paths.join(' · ') }],
  { title: t('plugins.confirmSubmission') }, token);
  if (!privateInputConfirmed) check();
  if (!accepted?.save) return;
  const result = await saveSourceEdit(owner, intent);
  if ('error' in result) { notification.error(result.error); return; }
  const saved = result.document;
  const state = saved.edit?.state;
  const detail = t('plugins.savedDetail', saved.edit?.source_saved ? t('plugins.sourceSaved') : t('plugins.sourceSaveUnconfirmed'), state,
    saved.status?.last_error ?? saved.status?.reason ?? '')
    + (privateParameter ? ` ${privateParameterStatus(saved, binding, privateParameter.pointer)}` : '');
  if (state === 'rejected' || state === 'recovery_required') {
    notification.error(detail);
  } else {
    notification.info(`${detail} ${scope === 'defaults' ? t('plugins.defaultsFutureOnly')
      : t('plugins.saveNotRuntime')}`);
  }
}

function containsWriteOnly(schema: unknown): boolean {
  return !!schema && typeof schema === 'object' && ((schema as Record<string, unknown>).writeOnly === true
    || Object.values(schema).some(containsWriteOnly));
}

export function writeOnlyFields(schema: Record<string, unknown> | undefined, prefix = ''): PrivateParameter[] {
  const properties = schema?.properties;
  const fields = properties && typeof properties === 'object' && !Array.isArray(properties)
    ? Object.entries(properties as Record<string, unknown>).flatMap(([key, candidate]) => {
      if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return [];
      const value = candidate as Record<string, unknown>;
      const pointer = `${prefix}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`;
      return value.writeOnly === true ? [{ pointer, schema: value }] : writeOnlyFields(value, pointer);
    }) : [];
  // 复杂位置由用户选择，确切分支、引用和类型统一交原配置 owner 校验。
  if (!prefix && containsWriteOnly(schema)) fields.push({ pointer: '', schema: {} });
  return fields;
}

/** 设置发现只消费安装清单和原配置 owner，不依赖插件管理权限或运行状态。 */
export async function configureServicePlugin(accessor: ServicesAccessor): Promise<void> {
  const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
  const dialogs = accessor.get(IDialogService);
  const initial = await workspaceContext.ensureReady();
  const lifecycle = createManagementTargetLifecycle(t('plugins.settingsTargetChanged'), initial.gatewayId ?? undefined);
  const target = managementGateway('settings');
  const { check, token, signal } = lifecycle;
  try {
    const scope = await quickInput.pick([{ label: t('plugins.gatewayParameters'), scope: 'gateway' as const },
      { label: t('plugins.workspaceParameters'), scope: 'workspace' as const }],
    { title: t('plugins.settingsTitle', managementTargetDisplayName(target), target.gateway_id), canPickMany: false }, token);
    check();
    if (!scope) return;
    const workspace = scope.scope === 'workspace' ? await choosePluginWorkspace(target, quickInput, token) : undefined;
    check();
    if (scope.scope === 'workspace' && !workspace) return;
    check();
    const schemas = await new ConfigurationSourceOwner({ ...target, workspace_id: workspace?.workspace_id }).configurationSchemas(signal);
    check();
    if (!schemas.plugins.length) { notifications.info(t('plugins.noConfigurationDeclarations')); return; }
    const selected = await quickInput.pick(schemas.plugins.map(plugin => ({ label: plugin.name,
      description: `${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
      detail: `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''} · ${plugin.instance_scope}`, plugin })),
    { title: t('plugins.chooseConfigurationVersion'), canPickMany: false }, token);
    check();
    if (!selected) return;
    const plugin = selected.plugin;
    const selectedAction = await quickInput.pick([{ label: t('plugins.viewSchema'), action: 'schema' },
      { label: t('plugins.editParametersJson'), action: 'json' },
      ...writeOnlyFields(plugin.configuration_schema).map(field => ({ label: field.pointer ? t('plugins.manageSecret', field.pointer) : t('plugins.manageSecretLocation'), action: 'secret', field }))],
    { title: `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''} · ${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
      canPickMany: false }, token);
    check();
    if (!selectedAction) return;
    if (selectedAction.action === 'schema') {
      await dialogs.info(`${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
        `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''}\n${JSON.stringify(plugin.configuration_schema, null, 2)}`);
      return;
    }
    await manageConfig(scope.scope, target, workspace, quickInput, notifications, { package_id: plugin.package_id,
      package_version: plugin.package_version, plugin_id: plugin.plugin_id, enabled: false }, null,
    selectedAction.action === 'json', 'field' in selectedAction ? selectedAction.field : undefined,
    check, token, signal);
  } finally { lifecycle.dispose(); }
}

export async function choosePluginWorkspace(target: ReturnType<typeof managementGateway>, quickInput: IQuickInputService,
  token?: CancellationToken): Promise<GatewayWorkspaceRequestTarget | undefined> {
  const workspaces = (await gateway.workspaces(false)).items.filter(item => item.gateway_id === target.gateway_id);
  const selected = await quickInput.pick(workspaces.map(workspace => ({ label: workspace.name, description: workspace.status,
    detail: workspace.workspace_id, workspace })), { title: t('plugins.managementWorkspace'), placeHolder: t('plugins.managementWorkspacePlaceholder'), canPickMany: false }, token);
  return selected ? { gateway_id: target.gateway_id, workspace_id: selected.workspace.workspace_id, gateway_connection_id: target.gateway_connection_id } : undefined;
}
