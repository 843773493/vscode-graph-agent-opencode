import { ContentQuickAccess } from '../../../../../src/clients/workbench/src/search/nameModes.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { invalidateGatewayUserSession } from '../../../../../src/clients/web/src/api/http.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';

/** 真实正文 Provider 与联邦 HTTP 客户端；仅宿主 Picker 事件和 Gateway 响应为装具。 */
export async function contentSelectionContract(mode) {
  const originalFetch = globalThis.fetch, before = workspaceContext.getSnapshot();
  const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001' };
  const otherOwner = { ...owner, workspaceId: '019a0000-0000-7000-8000-000000000002' };
  const owners = mode === 'logs' ? [owner, otherOwner] : [owner];
  const wireOwners = owners.map(item => ({ gateway_id: item.gatewayId, workspace_id: item.workspaceId }));
  const query = 'content 原始😀查询', categories = mode === 'logs' ? ['运行日志', 'Gateway 日志'] : ['会话消息', '工具结果'];
  const hosts = [], choices = [], requests = [], shows = [], errors = [];
  let current, closing = false;
  const until = async predicate => {
    for (let count = 0; count < 200; count++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('正文类别合同未收敛');
  };
  workspaceContext.publish({ ...before, gatewayId: owner.gatewayId, workspaces: [owner, otherOwner,
    { ...owner, workspaceId: '019a0000-0000-7000-8000-000000000003' }].map((item, index) => ({
      gateway_id: item.gatewayId, workspace_id: item.workspaceId, name: `工作区 ${index}`, connection_kind: 'local', status: 'ready', remote_gateways: [],
    })) });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname;
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'content-selection-test' } });
    if (path !== '/api/gateway/search') throw new Error(`正文类别出现额外读取：${path}`);
    const request = JSON.parse(init.body); requests.push(request);
    const id = `content-selection-${requests.length}`;
    const scope = { version: 1, gateway_id: owner.gatewayId, resolved_at: '2026-11-05T00:00:00Z', mode: 'selected', owners: wireOwners };
    const branches = request.queries.flatMap(item => {
      const keys = item.diagnostic_targets.length ? item.diagnostic_targets.map(diagnostics => ({ diagnostics })) : wireOwners.map(owner => ({ owner }));
      return keys.map(key => {
        const failed = mode === 'logs' && requests.length === 1 && key.diagnostics?.scope.source === 'gateway';
        return { branch: { ...key, category: item.category, operation: item.operation },
          state: failed ? 'failed' : 'complete', retryable: failed, allocated_results: 50, returned_results: 0,
          ...(failed ? { code: 'upstream_error', message: 'Gateway 原日志读取失败' } : {}) };
      });
    });
    const events = [{ type: 'scope', scope: { scope, branches: wireOwners.map(owner => ({ owner, state: 'registered' })) } },
      ...branches.map(branch => ({ type: 'branch', branch })),
      { type: 'end', summary: { state: branches.every(branch => branch.state === 'complete') ? 'complete' : 'incomplete', order: 'category-scope-owner-v1', returned_results: 0, branches } }];
    return new Response(events.map((event, index) => `id: ${index + 1}\nevent: search\ndata: ${JSON.stringify({ sequence: index + 1, request_id: id, ...event })}\n\n`).join(''),
      { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': id } });
  };
  const quickInput = {
    currentQuickInput: undefined,
    quickAccess: { show(value, options) {
      shows.push({ value, preserveValue: options.preserveValue, ...options.providerOptions });
      if (!closing && value.startsWith('content ')) open(value, options.providerOptions);
    } },
    pick(items, options, token) {
      current.picker.hide();
      const marker = {}; quickInput.currentQuickInput = marker;
      return new Promise(resolve => {
        const choice = { items, options, token, resolve(result) {
          if (quickInput.currentQuickInput === marker) quickInput.currentQuickInput = undefined;
          cancellation.dispose(); resolve(result);
        } };
        const cancellation = token.onCancellationRequested(() => choice.resolve(undefined));
        choices.push(choice);
      });
    },
  };
  function open(value, options) {
    current?.lifetime.dispose();
    const changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
    const picker = { value, items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: false, ctrlCmd: false },
      onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerButton: buttons.event,
      onDidHide: Event.None, onDidChangeActive: Event.None, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None,
      hide() { if (quickInput.currentQuickInput === picker) quickInput.currentQuickInput = undefined; host.lifetime?.dispose(); } };
    const group = { id: 7 }, groups = { activeGroup: group, groups: [], getGroup: id => id === group.id ? group : undefined };
    const editors = { openEditor() { throw new Error('类别选择不应打开编辑器'); } };
    const provider = new ContentQuickAccess({ createInstance: () => new PickerEditorState(editors, groups) }, editors, groups,
      { getHistory: () => [] }, { getValue: () => undefined }, quickInput, {}, { lookupKeybinding: () => undefined }, {},
      { error: error => errors.push(String(error)) }, { enabled: false }, {});
    const host = { picker, buttons, accepts, provider, cancellation, changes };
    current = host; hosts.push(host); quickInput.currentQuickInput = picker;
    host.lifetime = provider.provide(picker, cancellation.token, options);
  }
  const accept = id => {
    const item = current.picker.items.find(item => item.id === id);
    if (!item) throw new Error(`正文入口不存在：${id}`);
    current.picker.activeItems = current.picker.selectedItems = [item]; current.accepts.fire({ inBackground: false });
  };
  try {
    open(query, { searchOwners: owners, searchGroup: 7, ...(mode === 'cancel' ? { contentCategories: categories } : {}) });
    if (mode === 'file') {
      accept('content-category-file');
      return { shows, requests, choices: choices.length };
    }
    if (mode === 'cancel') {
      await until(() => requests.length === 1 && !current.picker.busy);
      current.buttons.fire(current.picker.buttons.find(button => button.tooltip === '选择正文类别'));
    } else accept(mode === 'logs' ? 'content-category-runtime-logs' : 'content-category-message');
    await until(() => choices.length === 1);
    const choice = choices[0], beforeConfirm = requests.length, preset = choice.items.filter(item => item.picked).map(item => item.label);
    if (mode === 'user' || mode === 'gateway') {
      if (mode === 'user') invalidateGatewayUserSession(0);
      else workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' });
      await until(() => errors.length === 1);
      return { requests, shows, errors, cancelled: choice.token.isCancellationRequested, beforeConfirm, preset };
    }
    choice.resolve(mode === 'cancel' ? undefined : choice.items.filter(item => categories.includes(item.label)));
    await until(() => requests.length === (mode === 'cancel' ? 2 : 1) && !current.picker.busy);
    let retryLabel;
    if (mode === 'logs') {
      const retry = current.picker.items.find(item => item.label.includes('Gateway 日志 · 查询失败'));
      retryLabel = retry.label; await retry.trigger();
      await until(() => requests.length === 2 && !current.picker.busy);
    }
    return { requests, shows, errors, beforeConfirm, preset, canPickMany: choice.options.canPickMany,
      choices: choice.items.map(item => item.label), placeholder: current.picker.placeholder, query: current.picker.value, retryLabel,
      labels: current.picker.items.map(item => item.label) };
  } finally {
    closing = true;
    for (const choice of choices) choice.resolve(undefined);
    for (const host of hosts) {
      host.lifetime.dispose(); host.provider.dispose(); host.cancellation.dispose(true);
      for (const event of [host.changes, host.accepts, host.buttons]) event.dispose();
    }
    workspaceContext.publish(before); globalThis.fetch = originalFetch;
  }
}
