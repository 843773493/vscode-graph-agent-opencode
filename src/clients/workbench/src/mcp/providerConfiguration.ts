import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { getConfigSourceDocument, preflightConfigSource, saveConfigSource } from '../../../web/src/api/configSources.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { ConfigSourceDocumentDTO, ConfigSourceEditRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { mcpConfigurationErrorMessage, mcpConfigurationSourceKey } from './configuration.js';
import type { LiveMcpProviderDefinition } from './providerBridge.js';

/** Provider 配置沿原 source/CAS writer 保存引用，不伪装制品或手动 launch。 */
export async function addMcpProviderConfiguration(candidates: readonly LiveMcpProviderDefinition[], context: {
  route: GatewayWorkspaceRequestTarget; owner: string; check: () => void; document: ConfigSourceDocumentDTO; serverId: string;
  quickInput: IQuickInputService; notifications: INotificationService; token: CancellationToken; signal: AbortSignal;
}): Promise<void> {
  const { route, owner, document, serverId, quickInput, notifications, token, signal } = context;
  const selected = await quickInput.pick(candidates.map(candidate => ({ label: candidate.definition.label,
    description: `${candidate.reference.extension_id} · ${candidate.definition.transport}`,
    detail: `${candidate.reference.extension_key} · ${candidate.reference.collection_id} · 原定义 ${candidate.reference.definition_ref}`, candidate,
  })), { title: '选择原扩展的 MCP 定义', prompt: owner, placeHolder: '只列出原宿主当前提供且属于此工作区的定义' }, token);
  context.check();
  if (!selected) return;
  const candidate = selected.candidate;
  const check = () => { context.check(); candidate.check(); signal.throwIfAborted(); };
  check();
  const enabled = await quickInput.pick([{ label: '暂不启用此 Server', enabled: false }, { label: '启用此 Server', enabled: true }],
    { title: `选择 ${serverId} 的启用状态`, prompt: owner }, token);
  check();
  if (!enabled) return;
  const definition = { enabled: enabled.enabled, transport: candidate.definition.transport, provider_definition: candidate.reference };
  const source = document.jsonc || '{}';
  const jsonc = applyEdits(source, setProperty(source, ['mcp', 'servers', serverId], definition,
    { insertSpaces: true, tabSize: 2, eol: '\n' }));
  const intent: ConfigSourceEditRequest = { gateway_id: route.gateway_id, workspace_id: route.workspace_id,
    source_key: mcpConfigurationSourceKey, jsonc, base_file_digest: document.file_digest,
    base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: generateUuid() };
  const preflight = await preflightConfigSource(0, route, intent, signal);
  check();
  const confirmed = await quickInput.pick([{ label: '保存此 Provider 来源引用', save: true }, { label: '取消', save: false }],
    { title: '确认 MCP Provider 配置', prompt: `${owner} · Server ${serverId} · ${candidate.definition.label}`,
      placeHolder: `变更：${preflight.changed_paths.join('、')} · ${enabled.enabled ? '保存后启动前解析原对象' : '暂不连接'}` }, token);
  check();
  if (!confirmed?.save) return;
  let saved: ConfigSourceDocumentDTO;
  try { saved = await saveConfigSource(0, route, intent, signal); }
  catch (cause) {
    context.check();
    let observed: string;
    try {
      const current = await getConfigSourceDocument(0, route, intent.source_key, { idempotencyKey: intent.idempotency_key, signal });
      observed = current.edit ? `source 状态 ${current.edit.state} · source_saved=${current.edit.source_saved}` : '尚未查到原提交';
    } catch (readError) { observed = `原来源回读失败：${mcpConfigurationErrorMessage(readError)}`; }
    throw new Error(`Provider 配置保存未确认：${mcpConfigurationErrorMessage(cause)} · 提交 ${intent.idempotency_key} · ${observed}`, { cause });
  }
  context.check();
  notifications.info(`Provider 配置提交 · ${owner} · Server ${serverId} · source 状态 ${saved.edit?.state} · source_saved=${saved.edit?.source_saved} · layer_imported=${saved.edit?.layer_imported}`);
}
