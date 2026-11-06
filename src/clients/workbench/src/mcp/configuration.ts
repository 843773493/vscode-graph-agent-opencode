import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Severity, type INotificationHandle, type INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { getConfigSourceDocument } from '../../../web/src/api/configSources.js';
import { draftMcpCandidateConfiguration, planMcpInstallation, saveMcpCandidateConfiguration } from '../../../web/src/api/mcpConnections.js';
import type { ConfigSourceDocumentDTO, McpCandidateConfigurationRequestDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { PublishedMcpArtifact } from './installation.js';

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

/** 制品发布后的可选纵向配置；正文由原候选 owner 生成，不消费脱敏 plan.parameters。 */
export async function offerMcpArtifactConfiguration(published: PublishedMcpArtifact, workspace: GatewayWorkspaceRequestTarget, label: string,
  quickInput: IQuickInputService, notifications: INotificationService, assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const route = Object.freeze({ ...workspace });
  const { artifact, candidate: original } = structuredClone(published);
  const binding = artifact.binding!;
  const owner = `${label} · ${route.gateway_id} · 工作区 ${route.workspace_id} · Backend ${binding.backend_id}`;
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  const cancellation = new CancellationTokenSource();
  const abortInput = () => cancellation.cancel();
  signal.addEventListener('abort', abortInput, { once: true });
  try {
    check();
    if (!await notificationAction(notifications, Severity.Info,
      `MCP 制品 ${artifact.identifier} ${artifact.version} 已在 ${label} 发布。工作区尚未配置或连接；generation ${binding.generation}`,
      '配置此工作区', signal)) return;
    check();
    const serverId = await quickInput.input({ title: '配置已发布 MCP 制品', prompt: owner,
      placeHolder: '输入精确 Server ID', validateInput: async value => /^[a-zA-Z][a-zA-Z0-9._-]{1,63}$/.test(value)
        ? undefined : 'Server ID 须以字母开头，长度 2–64，仅含字母、数字、点、下划线或连字符' }, cancellation.token);
    check();
    if (serverId === undefined) return;
    const selected = await quickInput.pick([
      { label: '暂不启用此 Server', enabled: false }, { label: '启用此 Server', enabled: true },
    ], { title: `选择 ${serverId} 的启用状态`, placeHolder: owner }, cancellation.token);
    check();
    if (!selected) return;
    const document = await getConfigSourceDocument(0, route, sourceKey, { signal });
    check();
    if (!document.writable) throw new Error('原工作区配置来源不可写');
    const candidate = { ...original, server_id: serverId };
    const planned = await planMcpInstallation(0, route, candidate, signal);
    check();
    if (!planned.data || planned.data.disposition !== 'requires_artifact') throw new Error('原候选不再对应已发布制品，必须重新选择');
    const plan = { candidate, plan_digest: planned.data.plan_digest };
    const target = { kind: 'workspace', ...route };
    const idempotencyKey = crypto.randomUUID();
    const draft = await draftMcpCandidateConfiguration(0, route, { target, server_id: serverId, plan, managed_launch: binding,
      enabled: selected.enabled, source_edit: { ...route, source_key: sourceKey, jsonc: document.jsonc,
        base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
        expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: idempotencyKey } }, signal);
    check();
    if (!draft.source_edit || !draft.preflight) throw new Error('原目标没有返回完整配置提案');
    const confirmed = await quickInput.pick([
      { label: '取消', save: false }, { label: '确认保存工作区配置', description: `${serverId} · ${selected.enabled ? '启用' : '暂不启用'}`,
        detail: sourceKey, save: true },
    ], { title: '确认 MCP 工作区配置提案', placeHolder: '选择确认保存工作区配置，或取消',
      prompt: `${owner} · ${artifact.identifier} ${artifact.version} · generation ${binding.generation} · 来源 ${sourceKey} · 变更：${draft.preflight.changed_paths.join('、')}` }, cancellation.token);
    check();
    if (!confirmed?.save) return;
    const intent: McpCandidateConfigurationRequestDTO = { target, server_id: serverId, plan, managed_launch: binding, source_edit: draft.source_edit };
    let saved: ConfigSourceDocumentDTO;
    try { saved = await saveMcpCandidateConfiguration(0, route, intent, signal); }
    catch (cause) {
      check();
      // 失败只回读原 source；未知结果始终保留本次幂等键，不重放 PUT。
      let observation: string;
      try { observation = sourceStatus(await getConfigSourceDocument(0, route, sourceKey, { idempotencyKey, signal }), idempotencyKey); }
      catch (readError) { observation = `原来源回读失败：${message(readError)}`; }
      check();
      if (cause instanceof HttpRequestError && cause.status >= 400 && cause.status < 500) {
        throw new Error(`配置保存被拒绝：${message(cause)}；${sourceKey} · 提交 ${idempotencyKey}；${observation}`, { cause });
      }
      for (;;) {
        if (!await notificationAction(notifications, Severity.Warning,
          `保存结果未确认，不会自动重发。${sourceKey} · 提交 ${idempotencyKey} · ${owner} · ${message(cause)}；${observation}`, '查询原来源提交', signal)) return;
        check();
        try {
          const queried = await getConfigSourceDocument(0, route, sourceKey, { idempotencyKey, signal });
          check();
          observation = sourceStatus(queried, idempotencyKey);
          if (queried.edit) { notifications.info(`${observation} · ${owner}`); return; }
        } catch (readError) { check(); observation = `原来源提交 ${idempotencyKey} 查询失败：${message(readError)}`; }
      }
    }
    check();
    notifications.info(`${sourceStatus(saved, idempotencyKey)} · ${owner}`);
  } finally { signal.removeEventListener('abort', abortInput); cancellation.dispose(); }
}
