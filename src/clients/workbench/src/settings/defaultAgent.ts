import { t } from '../../../shared/core/i18n/index.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { createScanner, parseTree, SyntaxKind, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { applyEdits } from '../../upstream/src/vs/base/common/jsonEdit.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { IQuickInputService, IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { discardConfigPending, getConfigSourceDocument, listConfigSources, preflightConfigSource, retryConfigPending, saveConfigSource } from '../../../web/src/api/configSources.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { ConfigSourceDocumentDTO, ConfigSourceEditRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

const valueText = (value: unknown) => value === undefined ? t('settings.editor.notSet') : JSON.stringify(value);
const errorText = (error: unknown) => `${error instanceof Error ? error.message : String(error)}${error instanceof HttpRequestError ? ` · request_id=${error.requestId ?? t('settings.editor.notReturned')}` : ''}`;
const sourceValue = (document: ConfigSourceDocumentDTO): unknown => document.layers.find(layer => layer.source_key === document.source_key)?.payload?.default_agent;
const originText = (document: ConfigSourceDocumentDTO) => {
  const origin = document.origins.find(item => item.path === '/default_agent');
  return origin ? t('settings.editor.origin', origin.source_key, origin.shadowed_sources.join(t('plugins.listSeparator')) || t('settings.editor.none')) : t('settings.editor.defaultAgentOriginMissing');
};

function editDefaultAgentText(text: string, value: string | undefined): string {
  const errors: ParseError[] = [], root = parseTree(text, errors);
  if (errors.length || root?.type !== 'object') throw new Error(t('settings.editor.invalidDefaultAgentSource'));
  const properties = root.children ?? [], selected = properties.filter(property => property.children?.[0].value === 'default_agent');
  if (selected.length > 1) throw new Error(t('settings.editor.duplicateDefaultAgent'));
  const node = selected[0]?.children?.[1];
  if (value !== undefined && node) return applyEdits(text, [{ offset: node.offset, length: node.length, content: JSON.stringify(value) }]);
  const edits: { offset: number; length: number; content: string }[] = [];
  const comma = (start: number, end: number) => {
    const scanner = createScanner(text, true);
    scanner.setPosition(start);
    while (scanner.getPosition() < end) {
      const token = scanner.scan();
      if (token === SyntaxKind.CommaToken) return scanner.getTokenOffset();
      if (token === SyntaxKind.EOF) break;
    }
    return undefined;
  };
  const closing = root.offset + root.length - 1;
  if (value === undefined) {
    const property = node?.parent;
    if (!property) return text;
    const index = properties.indexOf(property), previous = properties[index - 1], next = properties[index + 1];
    edits.push({ offset: property.offset, length: property.length, content: '' });
    // 仅删除语法逗号，原 editor 的区间删除会吞掉相邻注释并重排其它值。
    const following = comma(property.offset + property.length, next?.offset ?? closing);
    if (following !== undefined) edits.push({ offset: following, length: 1, content: '' });
    if (!next && previous) {
      const preceding = comma(previous.offset + previous.length, property.offset);
      if (preceding !== undefined) edits.push({ offset: preceding, length: 1, content: '' });
    }
  } else {
    const last = properties.at(-1), trailing = last ? comma(last.offset + last.length, closing) !== undefined : false;
    if (last && !trailing) edits.push({ offset: last.offset + last.length, length: 0, content: ',' });
    const eol = text.includes('\r\n') ? '\r\n' : '\n', indent = text.match(/\n([\t ]+)"/)?.[1] ?? '  ';
    const lineStart = text.lastIndexOf('\n', closing) + 1, ownLine = !text.slice(lineStart, closing).trim();
    edits.push({ offset: ownLine ? lineStart : closing, length: 0,
      content: `${ownLine ? '' : eol}${indent}"default_agent": ${JSON.stringify(value)}${trailing ? ',' : ''}${eol}` });
  }
  return applyEdits(text, edits);
}

function statusText(document: ConfigSourceDocumentDTO): string {
  const status = document.status;
  return status ? t('settings.editor.configurationStatus', status.restart_required ? t('settings.editor.pendingRestartPrefix') : '', status.state ?? t('settings.editor.notReturned'), valueText(status.active_revision), valueText(status.pending_revision), status.restart_required)
    : t('settings.editor.configurationStatusMissing');
}

function facts(document: ConfigSourceDocumentDTO, key?: string): IQuickPickItem[] {
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
    { label: t('settings.editor.originalSubmission'), detail: edit ? t('settings.editor.submissionDetails', edit.idempotency_key, edit.state, edit.source_saved, edit.layer_imported) : key ? t('settings.editor.submissionNotFound', key) : t('settings.editor.saveNotStarted') },
  ];
}

/** 只编辑已有业务字段；有效值、合并、秘密还原和应用状态始终由原来源 owner 决定。 */
export async function manageDefaultAgentSource(workspace: GatewayWorkspaceRequestTarget, label: string, quickInput: IQuickInputService,
  notifications: INotificationService, assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const route = Object.freeze({ gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id });
  const owner = t('settings.editor.defaultAgentOwner', label, route.gateway_id, route.workspace_id);
  const cancellation = new CancellationTokenSource(), abort = () => cancellation.cancel();
  signal.addEventListener('abort', abort, { once: true });
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  try {
    check();
    const sources = await listConfigSources(0, route, signal);
    check();
    const source = await quickInput.pick(sources.sources.map(item => ({ label: item.source_key ?? t('settings.editor.sourceIdentityMissing'),
      description: t('settings.editor.layerPresencePriority', item.layer, item.presence, item.precedence), detail: item.uri ?? t('settings.editor.fileUriMissing'), source: item,
    })), { title: t('settings.editor.chooseDefaultAgentSource'), prompt: owner, placeHolder: t('settings.editor.defaultAgentAccess') }, cancellation.token);
    check();
    if (!source) return;
    if (!source.source.source_key) throw new Error(t('settings.editor.selectedSourceIdentityMissing'));
    const sourceKey = source.source.source_key;
    let document = await getConfigSourceDocument(0, route, sourceKey, { signal });
    let submissionKey: string | undefined;
    let unresolvedSubmission = false;
    const context = () => t('settings.editor.sourceContext', owner, sourceKey, document.writable ? t('settings.editor.writable') : t('settings.editor.readOnly'), source.source.uri ? ` · ${source.source.uri}` : '');
    const showStatus = () => notifications.info(`${context()} · ${statusText(document)} · ${facts(document, submissionKey).at(-1)!.detail}`);
    const read = () => getConfigSourceDocument(0, route, sourceKey, { idempotencyKey: submissionKey, signal });

    const edit = async (originalValue: string | undefined) => {
      let value = originalValue;
      for (;;) {
        check();
        if (!document.writable) throw new Error(t('settings.editor.becameReadOnly'));
        const jsonc = editDefaultAgentText(document.jsonc, value);
        if (jsonc === document.jsonc) { notifications.info(t('settings.editor.defaultAgentUnchanged', context())); return; }
        const intent: ConfigSourceEditRequest = Object.freeze({ ...route, source_key: sourceKey, jsonc,
          base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
          expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: crypto.randomUUID() });
        let saving = false;
        try {
          const preflight = await preflightConfigSource(0, route, intent, signal);
          check();
          const confirmed = await quickInput.pick([{ label: t('settings.editor.cancel'), save: false }, { label: t('settings.editor.confirmLayerSave'), save: true,
            description: value === undefined ? t('settings.editor.removeDefaultAgent') : `default_agent = ${value}`,
            detail: t('settings.editor.defaultAgentCandidate', valueText(preflight.candidate?.default_agent), preflight.changed_paths.join(t('plugins.listSeparator')) || t('settings.editor.noEffectiveChange')),
          }], { title: t('settings.editor.confirmDefaultAgent'), prompt: t('settings.editor.contextSubmission', context(), intent.idempotency_key), placeHolder: t('settings.editor.defaultAgentSaveBoundary') }, cancellation.token);
          check();
          if (!confirmed?.save) return;
          saving = true;
          submissionKey = intent.idempotency_key;
          document = await saveConfigSource(0, route, intent, signal);
          check();
          unresolvedSubmission = false;
          showStatus();
          return;
        } catch (cause) {
          check();
          // 未知保存结果只能查询本次幂等身份；409／校验拒绝也必须先读原来源，显式重新预检。
          const uncertain = saving && !(cause instanceof HttpRequestError && cause.status >= 400 && cause.status < 500);
          unresolvedSubmission = uncertain;
          let latest: ConfigSourceDocumentDTO | undefined, readError: unknown;
          for (;;) {
            latest = undefined;
            try { latest = await getConfigSourceDocument(0, route, sourceKey, { idempotencyKey: intent.idempotency_key, signal }); check(); document = latest; readError = undefined; }
            catch (error) { check(); readError = error; }
            if (saving && latest?.edit) { unresolvedSubmission = false; showStatus(); return; }
            const choices = [{ label: t('settings.editor.querySourceSubmission'), action: 'query' }];
            if (!uncertain && latest?.writable) {
              choices.push({ label: t('settings.editor.preflightLatest'), action: 'rebase' });
              if (value !== undefined) choices.push({ label: t('settings.editor.changeRetainedAgent'), action: 'change' });
            }
            choices.push({ label: uncertain ? t('settings.editor.backPreserveSubmission') : t('settings.editor.backDiscardInput'), action: 'back' });
            const selected = await quickInput.pick(choices, { title: uncertain ? t('settings.editor.unknownQueryOnly') : t('settings.editor.incompletePreserveInput'),
              prompt: `${context()} · ${errorText(cause)}${readError ? t('settings.editor.readbackFailedSuffix', errorText(readError)) : ''}`,
              placeHolder: t('settings.editor.contextSubmission', value === undefined ? t('settings.editor.removeLayerOverride') : t('settings.editor.retainedInput', value), intent.idempotency_key) }, cancellation.token);
            check();
            if (!selected || selected.action === 'back') return;
            if (selected.action === 'query') continue;
            if (selected.action === 'change') {
              const changed = await quickInput.input({ title: t('settings.editor.changeRetainedAgent'), value,
                prompt: context(), placeHolder: t('settings.editor.agentIdPrompt'), validateInput: async text => text.trim() ? undefined : t('settings.editor.agentIdRequired') }, cancellation.token);
              check();
              if (changed === undefined) return;
              value = changed;
            }
            break;
          }
        }
      }
    };

    for (;;) {
      check();
      const actions: (IQuickPickItem & { action: string })[] = [];
      if (document.writable && !unresolvedSubmission) actions.push({ label: t('settings.editor.setLayerDefaultAgent'), action: 'set', description: valueText(sourceValue(document)) },
        { label: t('settings.editor.deleteLayerDefaultAgent'), action: 'remove', detail: t('settings.editor.deleteLayerDetail') });
      actions.push({ label: t('settings.editor.inspectDefaultAgent'), action: 'inspect',
        description: t('settings.editor.activeAndMerged', valueText(document.effective?.default_agent), valueText(document.source_effective?.default_agent)), detail: originText(document) },
      { label: t('settings.editor.refreshSource'), action: 'refresh', detail: statusText(document) });
      if (submissionKey) actions.push({ label: t('settings.editor.querySubmission'), action: 'query', detail: submissionKey });
      if (!unresolvedSubmission && document.status?.restart_required && document.status.candidate_ref) actions.push(
        { label: t('settings.editor.retryPending'), action: 'retry', detail: t('settings.editor.retryPendingDetail') },
        { label: t('settings.editor.discardPending'), action: 'discard', detail: t('settings.editor.discardPendingDetail') });
      const selected = await quickInput.pick(actions, { title: t('settings.editor.targetDefaultAgent'), prompt: context(), placeHolder: t('settings.editor.defaultAgentActions') }, cancellation.token);
      check();
      if (!selected) return;
      if (selected.action === 'inspect') {
        await quickInput.pick(facts(document, submissionKey), { title: t('settings.editor.defaultAgentStatus'), prompt: context(), placeHolder: t('settings.editor.readOnlyFacts') }, cancellation.token);
      } else if (selected.action === 'set') {
        const current = sourceValue(document);
        const value = await quickInput.input({ title: t('settings.editor.setLayerDefaultAgent'), value: typeof current === 'string' ? current : '',
          prompt: context(), placeHolder: t('settings.editor.agentIdPrompt'), validateInput: async text => text.trim() ? undefined : t('settings.editor.agentIdRequired') }, cancellation.token);
        check();
        if (value !== undefined) await edit(value);
      } else if (selected.action === 'remove') await edit(undefined);
      else if (selected.action === 'refresh' || selected.action === 'query') {
        document = await read(); check();
        if (document.edit) unresolvedSubmission = false;
        showStatus();
      }
      else {
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
          notifications.error(t('settings.editor.pendingFailed', context(), errorText(cause)));
          continue;
        }
        document = await read(); check(); showStatus();
      }
    }
  } finally { signal.removeEventListener('abort', abort); cancellation.dispose(); }
}
