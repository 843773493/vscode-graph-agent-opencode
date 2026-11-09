import { Severity, type INotificationHandle, type INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { getConfigSourceDocument, preflightConfigSource, saveConfigSource } from '../../../web/src/api/configSources.js';
import type { ConfigSourceDocumentDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

export const mcpConfigurationSourceKey = 'workspace_root_mutable_override';
export const mcpConfigurationErrorMessage = (cause: unknown) => cause instanceof HttpRequestError
  ? `${cause.message}${cause.requestId ? ` · request_id ${cause.requestId}` : ''}`
  : cause instanceof Error ? cause.message : String(cause);

export async function saveMcpWorkspaceConfiguration(route: GatewayWorkspaceRequestTarget, document: ConfigSourceDocumentDTO,
  jsonc: string, quickInput: IQuickInputService, notifications: INotificationService, signal: AbortSignal,
  check: () => void, summary: string): Promise<void> {
  const idempotencyKey = crypto.randomUUID();
  const intent = {
    gateway_id: route.gateway_id, workspace_id: route.workspace_id, source_key: mcpConfigurationSourceKey, jsonc,
    base_file_digest: document.file_digest, base_layer_revision: document.layer_revision,
    base_layer_digest: document.layer_digest, expected_active_revision: document.active_revision,
    expected_active_digest: document.active_digest, idempotency_key: idempotencyKey,
  };
  check();
  const preflight = await preflightConfigSource(0, route, intent, signal);
  check();
  const confirmation = await quickInput.pick([
    { label: '取消' }, { label: '确认保存工作区 MCP 配置', save: true, description: summary,
      detail: preflight.changed_paths.join(' · ') },
  ], { title: '确认 MCP 工作区配置' });
  check();
  if (!confirmation?.save) return;
  try {
    const saved = await saveConfigSource(0, route, intent, signal);
    check();
    notifications.info(`${saved.edit?.source_saved ? '来源已保存' : '来源未确认保存'} · ${saved.edit?.state ?? '未知状态'} · ${summary}`);
  } catch (cause) {
    check();
    let observation = '原来源提交状态尚未查询';
    try {
      const queried = await getConfigSourceDocument(0, route, mcpConfigurationSourceKey, { idempotencyKey, signal });
      check();
      observation = queried.edit ? `source 状态 ${queried.edit.state} · source_saved=${queried.edit.source_saved} · layer_imported=${queried.edit.layer_imported}`
        : `尚未查到原提交记录 · ${idempotencyKey}`;
      if (queried.edit) {
        notifications.info(`${observation} · ${summary}`);
        return;
      }
    } catch (readError) {
      check();
      observation = `原来源回读失败：${mcpConfigurationErrorMessage(readError)} · ${idempotencyKey}`;
    }
    if (cause instanceof HttpRequestError && cause.status >= 400 && cause.status < 500) {
      throw new Error(`MCP 配置保存被拒绝：${mcpConfigurationErrorMessage(cause)}；${observation}`, { cause });
    }
    if (!await notificationAction(notifications, Severity.Warning,
      `保存结果未确认，不会自动重发。${mcpConfigurationErrorMessage(cause)}；${observation}`, '查询原来源提交', signal)) return;
    check();
    for (;;) {
      try {
        const queried = await getConfigSourceDocument(0, route, mcpConfigurationSourceKey, { idempotencyKey, signal });
        check();
        if (queried.edit) {
          notifications.info(`source 状态 ${queried.edit.state} · source_saved=${queried.edit.source_saved} · layer_imported=${queried.edit.layer_imported} · ${summary}`);
          return;
        }
      } catch (readError) {
        check();
        observation = `原来源提交查询失败：${mcpConfigurationErrorMessage(readError)} · ${idempotencyKey}`;
      }
      if (!await notificationAction(notifications, Severity.Warning,
        `仍未查询到本次提交，不会重发。${observation}`, '继续查询原来源提交', signal)) return;
      check();
    }
  }
}

function notificationAction(notifications: INotificationService, severity: Severity, text: string, label: string,
  signal: AbortSignal): Promise<boolean> {
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
