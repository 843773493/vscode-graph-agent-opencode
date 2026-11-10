import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { parse, parseTree, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import type { PluginBinding, PluginPrivateInputReference, PluginPrivateInputWriteRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gatewayWorkspaceRequestHeaders, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { managementGateway } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { ConfigurationSourceOwner, sourceBaseline, sourceStatus, type SourceDocument } from './sourceOwner.js';

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
  if (errors.length || root?.type !== 'object') throw new Error('配置来源不是有效 JSONC 对象，未提交插件变更');
  const edits = setProperty(text, path, value, { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' });
  return applyEdits(text, edits);
}

function sourceBindings(document: ConfigDocument, path: string[]): PluginBinding[] {
  return bindingsAt(parse(document.jsonc), path);
}

function bindingJsonc(document: ConfigDocument, path: string[], binding: ConfigurationBinding, enabled: boolean | null): string {
  const updated = sourceBindings(document, path).slice();
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
  if (!Array.isArray(current) || current.some(item => !item || typeof item !== 'object')) throw new Error('插件绑定配置不是有效数组，未提交修改');
  return current as PluginBinding[];
}

async function chooseSource(quickInput: IQuickInputService, sources: Source[], title: string): Promise<Source | undefined> {
  const selected = await quickInput.pick(sources.filter(item => item.source_key).map(source => ({
    label: source.source_key!, description: `${source.layer} · ${source.presence}${source.writable === false ? ' · 只读' : ''}`,
    detail: source.uri ?? undefined, source,
  })), { title, placeHolder: '选择原配置来源；写入仍由来源 owner 的 CAS 决定', canPickMany: false });
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
      return { error: `保存结果未确认 · 原提交 ${intent.idempotency_key} 查询未找到 edit 记录 · 原错误：${original} · 不会重放` };
    } catch (readError) {
      const query = readError instanceof Error ? readError.message : String(readError);
      return { error: `保存结果未确认 · 原提交 ${intent.idempotency_key} 查询失败：${query} · 原错误：${original} · 不会重放` };
    }
  }
}

export async function readWorkspaceDefaults(target: ReturnType<typeof managementGateway>): Promise<PluginBinding[]> {
  const owner = new ConfigurationSourceOwner(target);
  const sources = await owner.list();
  const source = [...sources.sources].filter(item => item.loaded && item.source_key).sort((a, b) => b.precedence - a.precedence)[0];
  if (!source?.source_key) throw new Error('Gateway 没有可读取 workspace_defaults 的配置来源');
  const document = await owner.read(source.source_key);
  return bindingsAt(document.source_effective, ['plugins', 'workspace_defaults', 'entries']);
}

export async function readWorkspaceBindings(target: GatewayWorkspaceRequestTarget): Promise<PluginBinding[]> {
  const owner = new ConfigurationSourceOwner(target);
  const listed = await owner.list();
  const source = listed.sources.filter(item => item.loaded && item.source_key).sort((a, b) => b.precedence - a.precedence)[0];
  if (!source?.source_key) throw new Error('工作区没有可读取插件绑定的配置来源');
  const document = await owner.read(source.source_key);
  // 期望由原配置 owner 返回；Gateway 的异步交接回执只用于显示实际状态。
  return bindingsAt(document.source_effective, ['plugins', 'runtime', 'entries'])
    .map(binding => ({ ...binding, workspace_id: target.workspace_id }));
}

export async function setPluginBindings(scope: Scope, target: ReturnType<typeof managementGateway>, workspace: GatewayWorkspaceRequestTarget | undefined,
  quickInput: IQuickInputService, bindings: ConfigurationBinding[], enabled: boolean): Promise<{ binding: ConfigurationBinding; configuration: string }[] | undefined> {
  if (scope === 'workspace' && !workspace) throw new Error('当前没有已选择的工作区');
  const owner = new ConfigurationSourceOwner(scope === 'workspace' ? workspace! : target);
  let selected: Source | undefined;
  try {
    selected = await chooseSource(quickInput, (await owner.list()).sources,
      scope === 'workspace' ? '选择工作区插件配置来源' : '选择 Gateway 插件配置来源');
  } catch (error) {
    return bindings.map(binding => ({ binding, configuration: `未提交 · ${error instanceof Error ? error.message : String(error)}` }));
  }
  if (!selected?.source_key) return;
  const path = scope === 'defaults' ? ['plugins', 'workspace_defaults', 'entries'] : ['plugins', 'runtime', 'entries'];
  const results: { binding: ConfigurationBinding; configuration: string }[] = [];
  for (const binding of bindings) {
    let intent: ReturnType<ConfigurationSourceOwner['intent']>;
    try {
      // 每项重新取得来源基线；独立提交保留部分成功，不重试冲突或回滚兄弟插件。
      const document = await owner.read(selected.source_key);
      if (!document.writable) throw new Error(`配置来源 ${selected.source_key} 只读`);
      const jsonc = bindingJsonc(document, path, binding, enabled);
      if (jsonc === document.jsonc) {
        results.push({ binding, configuration: `无需修改 · ${sourceStatus(document)}` });
        continue;
      }
      intent = owner.intent(selected.source_key, jsonc, sourceBaseline(document));
      await owner.preflight(intent);
    } catch (error) {
      results.push({ binding, configuration: `未提交 · ${error instanceof Error ? error.message : String(error)}` });
      continue;
    }
    const saved = await saveSourceEdit(owner, intent);
    results.push({ binding, configuration: 'document' in saved ? sourceStatus(saved.document) : saved.error });
  }
  return results;
}

export async function manageConfig(scope: Scope, gatewayTarget: ReturnType<typeof managementGateway>, workspaceTarget: GatewayWorkspaceRequestTarget | undefined,
  quickInput: IQuickInputService, notification: INotificationService, binding: ConfigurationBinding, enabled: boolean | null,
  editParameters = false, privateParameter?: PrivateParameter, check = () => {}): Promise<void> {
  check();
  const path = scope === 'defaults' ? ['plugins', 'workspace_defaults', 'entries'] : ['plugins', 'runtime', 'entries'];
  if (scope === 'workspace' && !workspaceTarget) throw new Error('当前没有已选择的工作区');
  const owner = new ConfigurationSourceOwner(scope === 'workspace' ? workspaceTarget! : gatewayTarget);
  const selected = await chooseSource(quickInput, (await owner.list()).sources, scope === 'workspace' ? '选择工作区插件配置来源' : '选择 Gateway 插件配置来源');
  if (!selected) return;
  const source = selected;
  const document = await owner.read(source.source_key!);
  check();
  if (privateParameter || editParameters) {
    const bound = bindingsAt(document.effective, ['plugins', 'runtime', 'entries']).find(item => sameBinding(item, binding));
    if (bound && bound.package_version !== binding.package_version) {
      throw new Error(`当前绑定使用 ${bound.package_version}，所选 schema 为 ${binding.package_version}；请按绑定版本重新打开参数`);
    }
  }
  if (!document.writable) throw new Error(`配置来源 ${source.source_key} 只读，未提交插件变更`);
  let jsonc: string;
  let privateInputConfirmed = false;
  if (privateParameter) {
    const parameterPath = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id,
      ...privateParameter.pointer.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))];
    let current: unknown = parse(document.jsonc);
    for (const key of parameterPath) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
    const configured = current !== undefined;
    const action = await quickInput.pick([{ label: configured ? '更换已设置秘密参数' : '设置秘密参数', clear: false },
      ...(current !== undefined
        ? [{ label: '清除此来源的秘密覆盖', clear: true }] : [])],
    { title: `${binding.package_id}/${binding.plugin_id}${privateParameter.pointer}`, placeHolder: '原值不会回读；更换会创建新的不可变秘密版本' });
    if (!action) return;
    let storedValue: unknown;
    if (!action.clear) {
      const text = await quickInput.input({ title: '输入插件秘密参数', prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        password: true, validateInput: async value => {
          if (privateParameter.schema.type === 'string' || !value) return null;
          try { JSON.parse(value); return null; } catch { return '此秘密字段需要输入有效 JSON'; }
        } });
      if (text === undefined) return;
      storedValue = privateParameter.schema.type === 'string' ? text : JSON.parse(text);
      const accepted = await quickInput.pick([{ label: '取消' }, { label: '确认保存秘密', save: true,
        description: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key} · ${binding.package_id}@${binding.package_version}/${binding.plugin_id}${privateParameter.pointer}` }],
      { title: '确认目标、来源与秘密参数' });
      if (!accepted?.save) return;
      check();
      privateInputConfirmed = true;
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
        || !reference.version) throw new Error('插件秘密输入写入回执归属或引用版本无效');
      storedValue = reference;
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, action.clear ? undefined : storedValue);
  } else if (editParameters) {
    if (scope === 'defaults') throw new Error('新建工作区默认值只保存绑定；参数由目标工作区配置 owner 管理');
    const parameterPath = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id];
    let current: unknown = parse(document.jsonc);
    for (const key of parameterPath) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
    const operation = await quickInput.pick([{ label: '编辑此来源的参数', remove: false }, { label: '删除此来源的参数覆盖', remove: true }],
      { title: `${binding.package_id}/${binding.plugin_id} · ${binding.package_version}`, placeHolder: '删除只影响当前来源；原 owner 计算下层有效值' });
    if (!operation) return;
    let values: unknown;
    if (!operation.remove) {
      const text = await quickInput.input({ title: '编辑插件参数 JSON 对象', prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        value: JSON.stringify(current ?? {}), validateInput: async value => {
          try { const parsed = JSON.parse(value); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? null : '参数必须是 JSON 对象'; }
          catch { return '请输入有效 JSON；确切版本 schema 由原配置 owner 预检'; }
        } });
      if (text === undefined) return;
      values = JSON.parse(text);
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, values);
  } else {
    jsonc = bindingJsonc(document, path, binding, enabled);
  }
  if (jsonc === document.jsonc) return;
  check();
  const intent = owner.intent(source.source_key!, jsonc, sourceBaseline(document));
  const change = privateParameter ? '修改秘密参数' : editParameters ? '修改参数' : enabled === null ? '解除绑定' : enabled ? '启用' : '停用';
  const confirmation = privateParameter ? '插件秘密参数' : editParameters ? '插件参数' : '插件绑定';
  let preflight: Awaited<ReturnType<typeof owner.preflight>>;
  try { preflight = await owner.preflight(intent); }
  catch (error) {
    notification.error(`未提交 · ${error instanceof Error ? error.message : String(error)}`);
    return;
  }
  check();
  const accepted = privateInputConfirmed ? { save: true } : await quickInput.pick([{ label: '取消' }, { label: `确认保存${scope === 'workspace' ? '工作区' : ' Gateway '}${confirmation}`, save: true,
    description: `${binding.package_id}/${binding.plugin_id} · ${change}`, detail: preflight.changed_paths.join(' · ') }],
  { title: '确认配置提交' });
  if (!accepted?.save) return;
  check();
  const result = await saveSourceEdit(owner, intent);
  if ('error' in result) { notification.error(result.error); return; }
  const saved = result.document;
  const state = saved.edit?.state;
  const detail = `${saved.edit?.source_saved ? '来源已保存' : '来源未确认保存'} · ${state}。${saved.status?.last_error ?? saved.status?.reason ?? ''}`;
  if (state === 'rejected' || state === 'recovery_required') {
    notification.error(detail);
  } else {
    notification.info(`${detail} ${scope === 'defaults' ? '默认值只影响之后新建的工作区。'
      : '配置保存与实例启动分开；运行状态以目标 Gateway 回读为准。'}`);
  }
}

export function writeOnlyFields(schema: Record<string, unknown> | undefined, prefix = ''): PrivateParameter[] {
  // TODO：补齐组合 schema 与数组项的秘密位置选择；当前表单枚举对象属性，写入口仍按完整 schema 校验。
  const properties = schema?.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return [];
  return Object.entries(properties as Record<string, unknown>).flatMap(([key, candidate]) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return [];
    const value = candidate as Record<string, unknown>;
    const pointer = `${prefix}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`;
    return value.writeOnly === true ? [{ pointer, schema: value }] : writeOnlyFields(value, pointer);
  });
}

/** 设置发现只消费安装清单和原配置 owner，不依赖插件管理权限或运行状态。 */
export async function configureServicePlugin(accessor: ServicesAccessor): Promise<void> {
  const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
  const dialogs = accessor.get(IDialogService);
  const target = managementGateway('settings');
  const initialGateway = workspaceContext.getSnapshot().gatewayId;
  const cancellation = new CancellationTokenSource();
  const invalidate = () => cancellation.cancel();
  const releaseAuth = onGatewayTokenInvalidated(0, invalidate);
  const releaseContext = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== initialGateway) invalidate();
  });
  const check = () => {
    if (cancellation.token.isCancellationRequested) throw new Error('原 Gateway 或访问凭据已改变，请重新打开插件设置');
  };
  try {
    const scope = await quickInput.pick([{ label: 'Gateway 插件参数', scope: 'gateway' as const },
      { label: '工作区插件参数', scope: 'workspace' as const }],
    { title: `插件设置 · ${target.name} · ${target.gateway_id}`, canPickMany: false }, cancellation.token);
    if (!scope) return;
    const workspace = scope.scope === 'workspace' ? await choosePluginWorkspace(target, quickInput) : undefined;
    if (scope.scope === 'workspace' && !workspace) return;
    check();
    const schemas = await new ConfigurationSourceOwner({ ...target, workspace_id: workspace?.workspace_id }).configurationSchemas();
    check();
    if (!schemas.plugins.length) { notifications.info('目标范围没有已安装的插件配置声明。'); return; }
    const selected = await quickInput.pick(schemas.plugins.map(plugin => ({ label: plugin.name,
      description: `${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
      detail: `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''} · ${plugin.instance_scope}`, plugin })),
    { title: '选择已安装插件的确切配置版本', canPickMany: false }, cancellation.token);
    if (!selected) return;
    const plugin = selected.plugin;
    const selectedAction = await quickInput.pick([{ label: '查看参数 schema', action: 'schema' },
      { label: '编辑参数 JSON', action: 'json' },
      ...writeOnlyFields(plugin.configuration_schema).map(field => ({ label: `管理秘密参数 ${field.pointer}`, action: 'secret', field }))],
    { title: `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''} · ${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
      canPickMany: false }, cancellation.token);
    if (!selectedAction) return;
    check();
    if (selectedAction.action === 'schema') {
      await dialogs.info(`${plugin.package_id}/${plugin.plugin_id}@${plugin.package_version}`,
        `${schemas.gateway_id}${workspace ? ` · ${workspace.workspace_id}` : ''}\n${JSON.stringify(plugin.configuration_schema, null, 2)}`);
      return;
    }
    await manageConfig(scope.scope, target, workspace, quickInput, notifications, { package_id: plugin.package_id,
      package_version: plugin.package_version, plugin_id: plugin.plugin_id, enabled: false }, null,
    selectedAction.action === 'json', 'field' in selectedAction ? selectedAction.field : undefined, check);
  } finally { releaseAuth(); releaseContext(); cancellation.dispose(); }
}

export async function choosePluginWorkspace(target: ReturnType<typeof managementGateway>, quickInput: IQuickInputService): Promise<GatewayWorkspaceRequestTarget | undefined> {
  const workspaces = (await gateway.workspaces(false)).items.filter(item => item.gateway_id === target.gateway_id);
  const selected = await quickInput.pick(workspaces.map(workspace => ({ label: workspace.name, description: workspace.status,
    detail: workspace.workspace_id, workspace })), { title: '插件管理工作区', placeHolder: '本次只修改所选工作区配置 owner', canPickMany: false });
  return selected ? { gateway_id: target.gateway_id, workspace_id: selected.workspace.workspace_id, gateway_connection_id: target.gateway_connection_id } : undefined;
}
