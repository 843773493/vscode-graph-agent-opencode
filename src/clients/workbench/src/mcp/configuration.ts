import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Severity, type INotificationHandle, type INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { getConfigSourceDocument } from '../../../web/src/api/configSources.js';
import { draftMcpCandidateConfiguration, planMcpCandidateConfiguration, saveMcpCandidateConfiguration, writeMcpPrivateEnv,
  type McpInstallationRequestTarget } from '../../../web/src/api/mcpConnections.js';
import type { ConfigSourceDocumentDTO, McpCandidateConfigurationRequestDTO, McpCandidateInputDTO, McpPrivateEnvRefDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { PublishedMcpArtifact } from './installation.js';
import { workspaceContext } from '../workspaceContext.js';

const sourceKey = 'workspace_root_mutable_override';
const message = (cause: unknown) => cause instanceof HttpRequestError
  ? `${cause.message}${cause.requestId ? ` · request_id ${cause.requestId}` : ''}`
  : cause instanceof Error ? cause.message : String(cause);

/** 通知与后续 Quick Input 共用原身份信号，关闭通知即释放本次选择。 */
function notificationAction(notifications: INotificationService, severity: Severity, text: string, label: string, signal: AbortSignal): Promise<boolean> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    let handle: INotificationHandle;
    const finish = (selected: boolean) => { signal.removeEventListener('abort', abort); resolve(selected); };
    const abort = () => { signal.removeEventListener('abort', abort); reject(signal.reason); handle.close(); };
    handle = notifications.prompt(severity, text, [{ label, run: () => finish(true) }], { sticky: true, onCancel: () => finish(false) });
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

function sourceStatus(document: ConfigSourceDocumentDTO, key: string): string {
  const edit = document.edit;
  return `${edit
    ? `source 状态 ${edit.state} · source_saved=${edit.source_saved} · layer_imported=${edit.layer_imported}`
    : '尚未查到原提交记录，保存结果仍未确认'} · ${document.source_key} · 提交 ${key}`;
}

async function inputValue(input: McpCandidateInputDTO, owner: string, quickInput: IQuickInputService,
  token: CancellationToken, check: () => void): Promise<string | undefined> {
  const title = `配置 MCP 输入 ${input.key}`;
  const prompt = `${owner}${input.description ? ` · ${input.description}` : ''}`;
  const choices: string[] | undefined = input.choices ?? (input.format === 'boolean' ? ['true', 'false'] : undefined);
  if (choices) {
    const selected = await quickInput.pick(choices.filter(value => !input.is_required || value.length > 0).map(value => ({ label: value || '空字符串', value })),
      { title, prompt, placeHolder: '选择一个值' }, token);
    check();
    return selected?.value;
  }
  const value = await quickInput.input({ title, prompt, password: input.is_secret,
    placeHolder: input.is_required ? '输入值，不能为空字符串' : '输入值，可以明确留空',
    validateInput: async value => input.is_required && value.length === 0 ? '此输入不能为空字符串' : undefined }, token);
  check();
  return value;
}

/** 全部交互完成前只保留本次内存输入；重复项逐次收集，不能经多选丢失顺序或重复值。 */
async function configurationInputs(definitions: McpCandidateInputDTO[], owner: string, quickInput: IQuickInputService,
  token: CancellationToken, check: () => void): Promise<{ values: Record<string, string | string[]>; secrets: Map<string, string> } | undefined> {
  const values: Record<string, string | string[]> = Object.create(null), secrets = new Map<string, string>();
  for (const input of definitions) {
    const title = `配置 MCP 输入 ${input.key}`;
    if (input.default_value != null || input.can_skip) {
      const selected = await quickInput.pick([
        { label: '输入值', enter: true },
        { label: input.default_value != null ? '使用默认值' : '跳过此输入', enter: false },
      ], { title, prompt: `${owner}${input.description ? ` · ${input.description}` : ''}` }, token);
      check();
      if (!selected) return;
      if (!selected.enter) continue;
    }
    let value: string | string[] | undefined;
    if (input.is_repeated) {
      const repeated: string[] = [];
      for (;;) {
        const selected = await quickInput.pick([
          { label: '添加一个值', add: true },
          ...(!input.is_required || repeated.length ? [{ label: '完成输入', add: false }] : []),
        ], { title, placeHolder: `已添加 ${repeated.length} 个值，按加入顺序保存`, prompt: owner }, token);
        check();
        if (!selected) return;
        if (!selected.add) break;
        const item = await inputValue(input, owner, quickInput, token, check);
        if (item === undefined) return;
        repeated.push(item);
      }
      value = repeated;
    } else {
      value = await inputValue(input, owner, quickInput, token, check);
      if (value === undefined) return;
    }
    if (input.is_secret) {
      if (Array.isArray(value)) throw new Error('MCP 私有 env 输入必须是单个字符串');
      secrets.set(input.key, value);
    } else values[input.key] = value;
  }
  return { values, secrets };
}

/** 制品发布后的可选纵向配置；正文由原候选 owner 生成，不消费脱敏 plan.parameters。 */
export async function offerMcpArtifactConfiguration(published: PublishedMcpArtifact, installation: McpInstallationRequestTarget, label: string,
  quickInput: IQuickInputService, notifications: INotificationService, assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const installationTarget = Object.freeze({ ...installation });
  const { artifact, candidate: original, configuration_inputs, configuration_unavailable_reason } = structuredClone(published);
  const binding = artifact.binding!;
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  const cancellation = new CancellationTokenSource();
  const abortInput = () => cancellation.cancel();
  signal.addEventListener('abort', abortInput, { once: true });
  try {
    check();
    if (configuration_unavailable_reason != null) {
      notifications.warn(`MCP 制品 ${artifact.identifier} ${artifact.version} 已在 ${label} 发布。工作区配置不可用：${configuration_unavailable_reason}`);
      return;
    }
    if (!await notificationAction(notifications, Severity.Info,
      `MCP 制品 ${artifact.identifier} ${artifact.version} 已在 ${label} 发布。尚未配置或连接工作区；generation ${binding.generation}`,
      '选择工作区配置', signal)) return;
    check();
    await workspaceContext.refresh();
    check();
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.error) throw new Error(snapshot.error);
    const workspaces = snapshot.workspaces.filter(workspace => workspace.gateway_id === installationTarget.gateway_id
      && workspace.backend_id === installationTarget.backend_id);
    if (!workspaces.length) throw new Error('原安装域没有已确认归属的工作区，请先明确登记并刷新目标；制品已经发布');
    const workspace = await quickInput.pick(workspaces.map(workspace => ({
      label: workspace.name, description: workspace.status, detail: workspace.workspace_id, workspace,
    })), { title: '选择 MCP 配置工作区', placeHolder: `${label} · Backend ${installationTarget.backend_id} · 仅列出原安装域的工作区` }, cancellation.token);
    check();
    if (!workspace) return;
    const route: GatewayWorkspaceRequestTarget = Object.freeze({ gateway_id: workspace.workspace.gateway_id,
      workspace_id: workspace.workspace.workspace_id, gateway_connection_id: installationTarget.gateway_connection_id });
    const owner = `${workspace.label} · ${route.gateway_id} · 工作区 ${route.workspace_id} · Backend ${binding.backend_id}`;
    const checkWorkspace = () => {
      check();
      const registered = workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === route.gateway_id
        && item.workspace_id === route.workspace_id && item.backend_id === installationTarget.backend_id);
      if (registered?.status !== 'ready') throw new Error('原目标工作区尚未就绪或已不属于原安装域，请显式连接并重新选择配置目标');
    };
    checkWorkspace();
    const serverId = await quickInput.input({ title: '配置已发布 MCP 制品', prompt: owner,
      placeHolder: '输入精确 Server ID', validateInput: async value => /^[a-zA-Z][a-zA-Z0-9._-]{1,63}$/.test(value)
        ? undefined : 'Server ID 须以字母开头，长度 2–64，仅含字母、数字、点、下划线或连字符' }, cancellation.token);
    checkWorkspace();
    if (serverId === undefined) return;
    const selected = await quickInput.pick([
      { label: '暂不启用此 Server', enabled: false }, { label: '启用此 Server', enabled: true },
    ], { title: `选择 ${serverId} 的启用状态`, placeHolder: owner }, cancellation.token);
    checkWorkspace();
    if (!selected) return;
    const document = await getConfigSourceDocument(0, route, sourceKey, { signal });
    checkWorkspace();
    if (!document.writable) throw new Error('原工作区配置来源不可写');
    const collected = await configurationInputs(configuration_inputs, owner, quickInput, cancellation.token, checkWorkspace);
    checkWorkspace();
    if (!collected) return;
    const inputs: Record<string, string | string[] | McpPrivateEnvRefDTO> = Object.assign(Object.create(null), collected.values);
    const candidate = { ...original, server_id: serverId, inputs };
    const target = { kind: 'workspace', gateway_id: route.gateway_id, workspace_id: route.workspace_id };
    for (const [input_key, value] of collected.secrets) {
      const reference = await writeMcpPrivateEnv(0, route, { target, server_id: serverId, input_key,
        candidate: structuredClone(candidate), value }, signal);
      checkWorkspace();
      inputs[input_key] = reference;
    }
    collected.secrets.clear();
    const planned = await planMcpCandidateConfiguration(0, route, candidate, signal);
    checkWorkspace();
    if (!planned.data || planned.data.disposition !== 'requires_artifact') throw new Error('原候选不再对应已发布制品，必须重新选择');
    const plan = { candidate, plan_digest: planned.data.plan_digest };
    const idempotencyKey = crypto.randomUUID();
    const draft = await draftMcpCandidateConfiguration(0, route, { target, server_id: serverId, plan, managed_launch: binding,
      enabled: selected.enabled, source_edit: { gateway_id: route.gateway_id, workspace_id: route.workspace_id, source_key: sourceKey, jsonc: document.jsonc,
        base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
        expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: idempotencyKey } }, signal);
    checkWorkspace();
    if (!draft.source_edit || !draft.preflight) throw new Error('原目标没有返回完整配置提案');
    const confirmed = await quickInput.pick([
      { label: '取消', save: false }, { label: '确认保存工作区配置', description: `${serverId} · ${selected.enabled ? '启用' : '暂不启用'}`,
        detail: sourceKey, save: true },
    ], { title: '确认 MCP 工作区配置提案', placeHolder: '选择确认保存工作区配置，或取消',
      prompt: `${owner} · ${artifact.identifier} ${artifact.version} · generation ${binding.generation} · 来源 ${sourceKey} · 变更：${draft.preflight.changed_paths.join('、')}` }, cancellation.token);
    checkWorkspace();
    if (!confirmed?.save) return;
    const intent: McpCandidateConfigurationRequestDTO = { target, server_id: serverId, plan, managed_launch: binding, source_edit: draft.source_edit };
    let saved: ConfigSourceDocumentDTO;
    try { saved = await saveMcpCandidateConfiguration(0, route, intent, signal); }
    catch (cause) {
      checkWorkspace();
      // 失败只回读原 source；未知结果始终保留本次幂等键，不重放 PUT。
      let observation: string;
      try { observation = sourceStatus(await getConfigSourceDocument(0, route, sourceKey, { idempotencyKey, signal }), idempotencyKey); }
      catch (readError) { observation = `原来源回读失败：${message(readError)}`; }
      checkWorkspace();
      if (cause instanceof HttpRequestError && cause.status >= 400 && cause.status < 500) {
        throw new Error(`配置保存被拒绝：${message(cause)}；${sourceKey} · 提交 ${idempotencyKey}；${observation}`, { cause });
      }
      for (;;) {
        if (!await notificationAction(notifications, Severity.Warning,
          `保存结果未确认，不会自动重发。${sourceKey} · 提交 ${idempotencyKey} · ${owner} · ${message(cause)}；${observation}`, '查询原来源提交', signal)) return;
        checkWorkspace();
        try {
          const queried = await getConfigSourceDocument(0, route, sourceKey, { idempotencyKey, signal });
          checkWorkspace();
          observation = sourceStatus(queried, idempotencyKey);
          if (queried.edit) { notifications.info(`${observation} · ${owner}`); return; }
        } catch (readError) { checkWorkspace(); observation = `原来源提交 ${idempotencyKey} 查询失败：${message(readError)}`; }
      }
    }
    checkWorkspace();
    notifications.info(`${sourceStatus(saved, idempotencyKey)} · ${owner}`);
  } finally { signal.removeEventListener('abort', abortInput); cancellation.dispose(); }
}
