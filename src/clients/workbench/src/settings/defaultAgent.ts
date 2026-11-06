import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { createScanner, parseTree, SyntaxKind, type ParseError } from '../../upstream/src/vs/base/common/json.js';
import { applyEdits } from '../../upstream/src/vs/base/common/jsonEdit.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { IQuickInputService, IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { discardConfigPending, getConfigSourceDocument, listConfigSources, preflightConfigSource, retryConfigPending, saveConfigSource } from '../../../web/src/api/configSources.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { ConfigSourceDocumentDTO, ConfigSourceEditRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

const valueText = (value: unknown) => value === undefined ? '未设置' : JSON.stringify(value);
const errorText = (error: unknown) => `${error instanceof Error ? error.message : String(error)}${error instanceof HttpRequestError ? ` · request_id=${error.requestId ?? '未返回'}` : ''}`;
const sourceValue = (document: ConfigSourceDocumentDTO): unknown => document.layers.find(layer => layer.source_key === document.source_key)?.payload?.default_agent;
const originText = (document: ConfigSourceDocumentDTO) => {
  const origin = document.origins.find(item => item.path === '/default_agent');
  return origin ? `来源 ${origin.source_key} · 被遮蔽 ${origin.shadowed_sources.join('、') || '无'}` : '后端未返回 default_agent 来源';
};

function editDefaultAgentText(text: string, value: string | undefined): string {
  const errors: ParseError[] = [], root = parseTree(text, errors);
  if (errors.length || root?.type !== 'object') throw new Error('原来源不是有效的 JSONC 对象，不能编辑 default_agent');
  const properties = root.children ?? [], selected = properties.filter(property => property.children?.[0].value === 'default_agent');
  if (selected.length > 1) throw new Error('原来源包含重复 default_agent，无法确定唯一编辑位置');
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
  return status ? `${status.restart_required ? '存在未生效变更，待重启 · ' : ''}状态 ${status.state ?? '未返回'} · active=${valueText(status.active_revision)} · pending=${valueText(status.pending_revision)} · restart_required=${status.restart_required}`
    : '后端未返回配置状态';
}

function facts(document: ConfigSourceDocumentDTO, key?: string): IQuickPickItem[] {
  const status = document.status, edit = document.edit;
  return [
    { label: '运行中 default_agent', detail: valueText(document.effective?.default_agent) },
    { label: '来源合并 default_agent', detail: valueText(document.source_effective?.default_agent) },
    { label: '所选来源覆盖', detail: valueText(sourceValue(document)) },
    { label: 'default_agent 来源与遮蔽', detail: originText(document) },
    { label: '来源文件与层基线', detail: `presence=${document.file_presence} · file=${valueText(document.file_digest)} · layer=${valueText(document.layer_revision)} / ${valueText(document.layer_digest)} · generation=${valueText(document.source_generation)}` },
    { label: '运行中基线', detail: `${valueText(document.active_revision)} / ${valueText(document.active_digest)}` },
    { label: '配置应用状态', detail: statusText(document) },
    { label: '应用结果', detail: status ? `applied=${status.applied_paths.join('、')} · deferred=${status.deferred_paths.join('、')} · healthy=${status.healthy} · ${status.reason ?? ''} · ${status.last_error ?? ''}` : '未返回' },
    { label: '原候选与应用', detail: status ? `candidate=${status.candidate_id ?? ''} · ref=${status.candidate_ref ?? ''} · attempt=${status.attempt_id ?? ''} · apply=${status.apply_id ?? ''}` : '未返回' },
    { label: '原来源提交', detail: edit ? `幂等键 ${edit.idempotency_key} · ${edit.state} · source_saved=${edit.source_saved} · layer_imported=${edit.layer_imported}` : key ? `幂等键 ${key} · 尚未查到原提交，保存结果未确认` : '尚未发起保存' },
  ];
}

/** 只编辑已有业务字段；有效值、合并、秘密还原和应用状态始终由原来源 owner 决定。 */
export async function manageDefaultAgentSource(workspace: GatewayWorkspaceRequestTarget, label: string, quickInput: IQuickInputService,
  notifications: INotificationService, assertAccess: () => void, signal: AbortSignal): Promise<void> {
  const route = Object.freeze({ gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id });
  const owner = `${label} · Gateway ${route.gateway_id} · 工作区 ${route.workspace_id}`;
  const cancellation = new CancellationTokenSource(), abort = () => cancellation.cancel();
  signal.addEventListener('abort', abort, { once: true });
  const check = () => { assertAccess(); signal.throwIfAborted(); };
  try {
    check();
    const sources = await listConfigSources(0, route, signal);
    check();
    const source = await quickInput.pick(sources.sources.map(item => ({ label: item.source_key ?? '无稳定来源标识',
      description: `${item.layer} · ${item.presence} · 优先级 ${item.precedence}`, detail: item.uri ?? '后端未提供文件 URI', source: item,
    })), { title: '选择默认 Agent 配置来源', prompt: owner, placeHolder: '选择来源后按后端 writable 决定可写性；默认和只读层仍可查看' }, cancellation.token);
    check();
    if (!source) return;
    if (!source.source.source_key) throw new Error('所选来源没有后端稳定标识，无法读取来源文档');
    const sourceKey = source.source.source_key;
    let document = await getConfigSourceDocument(0, route, sourceKey, { signal });
    let submissionKey: string | undefined;
    let unresolvedSubmission = false;
    const context = () => `${owner} · 来源 ${sourceKey} · ${document.writable ? '可写' : '只读'}${source.source.uri ? ` · ${source.source.uri}` : ''}`;
    const showStatus = () => notifications.info(`${context()} · ${statusText(document)} · ${facts(document, submissionKey).at(-1)!.detail}`);
    const read = () => getConfigSourceDocument(0, route, sourceKey, { idempotencyKey: submissionKey, signal });

    const edit = async (originalValue: string | undefined) => {
      let value = originalValue;
      for (;;) {
        check();
        if (!document.writable) throw new Error('原来源已变为只读，本次输入未提交');
        const jsonc = editDefaultAgentText(document.jsonc, value);
        if (jsonc === document.jsonc) { notifications.info(`${context()} · 本层 default_agent 无变化，未提交`); return; }
        const intent: ConfigSourceEditRequest = Object.freeze({ ...route, source_key: sourceKey, jsonc,
          base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
          expected_active_revision: document.active_revision, expected_active_digest: document.active_digest, idempotency_key: crypto.randomUUID() });
        let saving = false;
        try {
          const preflight = await preflightConfigSource(0, route, intent, signal);
          check();
          const confirmed = await quickInput.pick([{ label: '取消', save: false }, { label: '确认保存本层覆盖', save: true,
            description: value === undefined ? '删除所选来源的 default_agent 覆盖' : `default_agent = ${value}`,
            detail: `候选 default_agent=${valueText(preflight.candidate?.default_agent)} · 变更 ${preflight.changed_paths.join('、') || '无有效值变化'}`,
          }], { title: '确认默认 Agent 来源修改', prompt: `${context()} · 幂等键 ${intent.idempotency_key}`, placeHolder: '保存与生效分别报告；默认 Agent 只影响后续新建会话' }, cancellation.token);
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
            const choices = [{ label: '查询原来源与提交', action: 'query' }];
            if (!uncertain && latest?.writable) {
              choices.push({ label: '基于最新来源重新预检', action: 'rebase' });
              if (value !== undefined) choices.push({ label: '修改保留的默认 Agent ID', action: 'change' });
            }
            choices.push({ label: uncertain ? '返回来源（保留原提交标识）' : '返回来源（放弃本次输入）', action: 'back' });
            const selected = await quickInput.pick(choices, { title: uncertain ? '保存结果未确认：只查询原提交' : '来源修改未完成：保留本次输入',
              prompt: `${context()} · ${errorText(cause)}${readError ? ` · 回读失败：${errorText(readError)}` : ''}`,
              placeHolder: `${value === undefined ? '删除本层覆盖' : `保留输入 ${value}`} · 幂等键 ${intent.idempotency_key}` }, cancellation.token);
            check();
            if (!selected || selected.action === 'back') return;
            if (selected.action === 'query') continue;
            if (selected.action === 'change') {
              const changed = await quickInput.input({ title: '修改保留的默认 Agent ID', value,
                prompt: context(), placeHolder: '输入精确 Agent ID，由后端 schema 和预检校验', validateInput: async text => text.trim() ? undefined : 'Agent ID 不能为空' }, cancellation.token);
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
      if (document.writable && !unresolvedSubmission) actions.push({ label: '设置本层 default_agent', action: 'set', description: valueText(sourceValue(document)) },
        { label: '删除本层 default_agent 覆盖', action: 'remove', detail: '只删除所选来源中的键，继承结果由后端计算' });
      actions.push({ label: '查看默认 Agent 来源与配置状态', action: 'inspect',
        description: `运行中 ${valueText(document.effective?.default_agent)} · 来源合并 ${valueText(document.source_effective?.default_agent)}`, detail: originText(document) },
      { label: '刷新原来源', action: 'refresh', detail: statusText(document) });
      if (submissionKey) actions.push({ label: '查询原来源提交', action: 'query', detail: submissionKey });
      if (!unresolvedSubmission && document.status?.restart_required && document.status.candidate_ref) actions.push(
        { label: '重试原 pending 状态', action: 'retry', detail: '调用原 retry API；不执行 Backend 重启，不生成健康证明' },
        { label: '丢弃原 pending 候选', action: 'discard', detail: '以原 active CAS 请求原 owner 判断能否安全丢弃' });
      const selected = await quickInput.pick(actions, { title: '目标工作区默认 Agent 来源', prompt: context(), placeHolder: '设置、删除覆盖或查看后端来源与应用状态；Esc 退出' }, cancellation.token);
      check();
      if (!selected) return;
      if (selected.action === 'inspect') {
        await quickInput.pick(facts(document, submissionKey), { title: '默认 Agent 来源与应用状态', prompt: context(), placeHolder: '只读后端事实；选择任意行或 Esc 返回' }, cancellation.token);
      } else if (selected.action === 'set') {
        const current = sourceValue(document);
        const value = await quickInput.input({ title: '设置本层 default_agent', value: typeof current === 'string' ? current : '',
          prompt: context(), placeHolder: '输入精确 Agent ID，由后端 schema 和预检校验', validateInput: async text => text.trim() ? undefined : 'Agent ID 不能为空' }, cancellation.token);
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
        if (!candidateRef) throw new Error('原 pending 身份已不可用，请刷新原来源');
        const confirmed = await quickInput.pick([{ label: '取消', confirm: false }, { label: selected.label, confirm: true }], {
          title: '确认原 pending 操作', prompt: `${context()} · candidate_ref=${candidateRef}`, placeHolder: selected.detail,
        }, cancellation.token);
        check();
        if (!confirmed?.confirm) continue;
        try {
          if (selected.action === 'retry') await retryConfigPending(0, route, candidateRef, signal);
          else {
            if (typeof document.active_revision !== 'number' || typeof document.active_digest !== 'string') throw new Error('原来源没有完整 active CAS，不能丢弃 pending');
            await discardConfigPending(0, route, candidateRef, { active_revision: document.active_revision, active_digest: document.active_digest }, signal);
          }
        } catch (cause) {
          check();
          try { document = await read(); } catch (readError) { check(); throw new AggregateError([cause, readError], '原 pending 操作与来源回读均失败'); }
          check();
          notifications.error(`${context()} · 原 pending 操作失败：${errorText(cause)}`);
          continue;
        }
        document = await read(); check(); showStatus();
      }
    }
  } finally { signal.removeEventListener('abort', abort); cancellation.dispose(); }
}
