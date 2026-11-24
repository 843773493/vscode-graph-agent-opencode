import { t } from '../../../shared/core/i18n/index.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { IQuickInputService, IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { discardConfigPending, getConfigSourceDocument, retryConfigPending } from '../../../web/src/api/configSources.js';
import { errorMessage } from '../../../web/src/utils/errorMessage.js';
import { failureSummary } from '../../../shared/core/failurePresentation.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { ConfigSourceDocumentDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { TargetSettingsLocation } from './scope.js';

const valueText = (value: unknown) => value === undefined ? t('settings.editor.notSet') : JSON.stringify(value);
const sourceValue = (document: ConfigSourceDocumentDTO): unknown => {
  const settings = document.layers.find(layer => layer.source_key === document.source_key)?.payload?.settings;
  return settings && typeof settings === 'object' && !Array.isArray(settings)
    && settings.workspace && typeof settings.workspace === 'object' && !Array.isArray(settings.workspace)
    ? settings.workspace.default_agent : undefined;
};
const originText = (document: ConfigSourceDocumentDTO) => {
  const origin = document.origins.find(item => item.path === '/default_agent');
  return origin ? t('settings.editor.origin', origin.source_key, origin.shadowed_sources.join(t('plugins.listSeparator')) || t('settings.editor.none')) : t('settings.editor.defaultAgentOriginMissing');
};

function statusText(document: ConfigSourceDocumentDTO): string {
  const status = document.status;
  return status ? t('settings.editor.configurationStatus', status.restart_required ? t('settings.editor.pendingRestartPrefix') : '', status.state ?? t('settings.editor.notReturned'), valueText(status.active_revision), valueText(status.pending_revision), status.restart_required)
    : t('settings.editor.configurationStatusMissing');
}

function facts(document: ConfigSourceDocumentDTO): IQuickPickItem[] {
  const status = document.status, edit = document.edit;
  return [
    { label: t('settings.editor.activeDefaultAgent'), detail: valueText(document.effective?.default_agent) },
    { label: t('settings.editor.mergedDefaultAgent'), detail: valueText(document.source_effective?.default_agent) },
    { label: t('settings.editor.selectedOverride'), detail: valueText(sourceValue(document)) },
    { label: t('settings.editor.defaultAgentOrigin'), detail: originText(document) },
    { label: t('settings.editor.sourceBaseline'), detail: `presence=${document.file_presence} · file=${valueText(document.file_digest)} · layer=${valueText(document.layer_revision)} / ${valueText(document.layer_digest)} · generation=${valueText(document.source_generation)}` },
    { label: t('settings.editor.activeBaseline'), detail: `${valueText(document.active_revision)} / ${valueText(document.active_digest)}` },
    { label: t('settings.editor.applicationStatus'), detail: statusText(document) },
    { label: t('settings.editor.applicationResult'), detail: status ? `applied=${status.applied_paths.join(t('plugins.listSeparator'))} · deferred=${status.deferred_paths.join(t('plugins.listSeparator'))} · healthy=${status.healthy} · ${status.reason ?? ''} · ${status.last_error ?? ''}` : t('settings.editor.notReturned') },
    { label: t('settings.editor.candidateAndApplication'), detail: status ? `candidate=${status.candidate_id ?? ''} · ref=${status.candidate_ref ?? ''} · attempt=${status.attempt_id ?? ''} · apply=${status.apply_id ?? ''}` : t('settings.editor.notReturned') },
    { label: t('settings.editor.originalSubmission'), detail: edit ? t('settings.editor.submissionDetails', edit.idempotency_key, edit.state, edit.source_saved, edit.layer_imported) : t('settings.editor.saveNotStarted') },
  ];
}

/** 保留原来源状态与 pending 操作；default_agent 正文只由 Agent 表单和 JSONC 共用模型编辑。 */
export async function inspectDefaultAgentSource(location: TargetSettingsLocation, quickInput: IQuickInputService,
  notifications: INotificationService, assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const route: GatewayWorkspaceRequestTarget = Object.freeze({ gateway_id: location.gateway_id,
    gateway_connection_id: location.gateway_connection_id, workspace_id: location.workspace_id! });
  const context = () => t('settings.editor.defaultAgentOwner', location.name, route.gateway_id, route.workspace_id);
  const cancellation = new CancellationTokenSource(), abort = () => cancellation.cancel();
  signal.addEventListener('abort', abort, { once: true });
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  const read = () => getConfigSourceDocument(0, route, location.source_key, { signal });
  try {
    check();
    let document = await read();
    check();
    for (;;) {
      check();
      const actions: (IQuickPickItem & { action: 'inspect' | 'refresh' | 'retry' | 'discard' })[] = [
        { label: t('settings.editor.inspectDefaultAgent'), action: 'inspect',
          description: t('settings.editor.activeAndMerged', valueText(document.effective?.default_agent), valueText(document.source_effective?.default_agent)), detail: originText(document) },
        { label: t('settings.editor.refreshSource'), action: 'refresh', detail: statusText(document) },
      ];
      if (document.status?.restart_required && document.status.candidate_ref) actions.push(
        { label: t('settings.editor.retryPending'), action: 'retry', detail: t('settings.editor.retryPendingDetail') },
        { label: t('settings.editor.discardPending'), action: 'discard', detail: t('settings.editor.discardPendingDetail') });
      const selected = await quickInput.pick(actions, { title: t('settings.editor.defaultAgentStatus'), prompt: context(),
        placeHolder: t('settings.editor.readOnlyFacts') }, cancellation.token);
      check();
      if (!selected) return;
      if (selected.action === 'inspect') {
        await quickInput.pick(facts(document), { title: t('settings.editor.defaultAgentStatus'), prompt: context(),
          placeHolder: t('settings.editor.readOnlyFacts') }, cancellation.token);
      } else if (selected.action === 'refresh') {
        document = await read(); check();
        notifications.info(`${context()} · ${statusText(document)}`);
      } else {
        const candidateRef = document.status?.candidate_ref;
        if (!candidateRef) throw new Error(t('settings.editor.pendingIdentityMissing'));
        const confirmed = await quickInput.pick([{ label: t('settings.editor.cancel'), confirm: false }, { label: selected.label, confirm: true }], {
          title: t('settings.editor.confirmPending'), prompt: `${context()} · candidate_ref=${candidateRef}`, placeHolder: selected.detail,
        }, cancellation.token);
        check();
        if (!confirmed?.confirm) continue;
        try {
          if (selected.action === 'retry') await retryConfigPending(0, route, candidateRef, signal);
          else {
            if (typeof document.active_revision !== 'number' || typeof document.active_digest !== 'string') throw new Error(t('settings.editor.activeCasMissing'));
            await discardConfigPending(0, route, candidateRef, { active_revision: document.active_revision, active_digest: document.active_digest }, signal);
          }
        } catch (cause) {
          check();
          try { document = await read(); } catch (readError) { check(); throw new AggregateError([cause, readError], t('settings.editor.pendingAndReadFailed')); }
          check();
          notifications.error(failureSummary(t('settings.editor.pendingFailed', location.name, errorMessage(cause))));
          continue;
        }
        document = await read(); check();
        notifications.info(`${context()} · ${statusText(document)}`);
      }
    }
  } finally { signal.removeEventListener('abort', abort); cancellation.dispose(); }
}
