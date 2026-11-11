import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IDialogService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/dialogs/common/dialogs.js';
import { INotificationService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/notification/common/notification.js';
import { IEditorService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { resourceActionCommand } from '../../../../../../src/clients/workbench/src/resources/runtimeActions.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayUserSession } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
const thread = { session_id: 'ses_source', thread_id: 'thr_child' };
const record = (kind, index) => ({ resource_id: `${kind}_same`, workspace_id: owners[index].workspaceId, kind,
  retention_scope: 'workspace', source_thread: { session_id: 'ses_old', thread_id: 'thr_old' }, thread_associations: [thread],
  owner_revision: 7, status: 'running', reconciliation_status: 'verified', available_actions: ['promote', 'attach', 'detach', 'stop', 'delete'],
  metadata: { title: 'Needle 同名资源' }, created_at: '2026-11-06T00:00:00Z', updated_at: '2026-11-06T00:00:00Z' });

/** 真实 Provider／Action／HTTP 客户端；仅宿主服务与后端回执为显式装具。 */
export async function runtimeActionContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], errors = [], shows = [], choices = [], notices = [], confirmations = [], sourceCalls = [];
  const targets = owners.map((owner, i) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: `工作区 ${i}`, status: 'ready' }));
  const initial = { ...previous, gatewayId: 'control-gateway', loading: false, error: null, workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]) };
  workspaceContext.publish(initial);
  let records = owners.map((_, i) => ['terminal', 'browser'].map(kind => record(kind, i))), releaseRoutes, releaseWrite, releaseChoice, releaseRead, aborted = false;
  if (mode === 'empty-title') for (const list of records) list.find(item => item.kind === 'browser').metadata.title = '';
  let active = 0, maximum = 0;
  const ok = data => {
    const response = () => Response.json({ code: 0, request_id: 'runtime-contract', data }, { headers: { 'X-Request-ID': 'runtime-contract' } });
    if (mode !== 'parallel') return response();
    active++; maximum = Math.max(maximum, active);
    return new Promise(resolve => setTimeout(() => { active--; resolve(response()); }, 20));
  };
  installSessionApiFetchMock(({ path, url, method, init }) => {
    const headers = new Headers(init?.headers), index = headers.get('X-BoxTeam-Target-Gateway-Id') === owners[1].gatewayId ? 1 : 0;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, body, gateway: headers.get('X-BoxTeam-Target-Gateway-Id'), query: new URL(url, location.href).search });
    if (path === '/api/gateway/config/sources' || path === '/api/v1/config/sources') return ok({ sources: [] });
    if (path === '/api/gateway/workspaces') {
      if (mode === 'freeze' && releaseRoutes === undefined && requests.some(item => item.phase === 'command')) return new Promise(resolve => { releaseRoutes = () => resolve(ok({ items: targets })); });
      return ok({ items: targets });
    }
    if (path === '/api/v1/resources/gateway-skills') return ok({ gateway_id: owners[index].gatewayId, workspace_id: owners[index].workspaceId, revision: 'fixed', available: true, entries: [] });
    if (path === '/api/v1/resources/agent-spec') return ok({ node: { uri: 'boxteam://workspace/x/resources/agent-spec/root/AGENTS.md', resource_shape: 'file', operations: ['read'] } });
    if (path.endsWith('skill-sources') || path.endsWith('local-skills')) return ok({ workspace_id: owners[index].workspaceId, entries: [] });
    if (path === '/api/v1/resources') {
      const kind = new URL(url, location.href).searchParams.get('kind');
      const response = () => ok({ workspace_id: mode === 'mismatch' && index === 0 && kind === 'terminal' ? 'wrong-owner' : owners[index].workspaceId,
        items: records[index].filter(item => item.kind === kind) });
      if (kind === 'terminal' && index === 0 && mode.startsWith('pending-')) {
        init.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { releaseRead = () => resolve(response()); });
      }
      return response();
    }
    const match = path.match(/^\/api\/v1\/resources\/(terminal_same|browser_same)\/(promote|attach|detach|stop|delete)$/);
    if (match && method === 'POST') {
      if (mode === 'conflict') return Response.json({ detail: '资源 revision 冲突', request_id: 'runtime-conflict' }, { status: 409, headers: { 'X-Request-ID': 'runtime-conflict' } });
      const resource = records[index].find(item => item.resource_id === match[1]);
      const response = () => {
        const updated = { ...resource, owner_revision: body.expected_revision + 1,
          status: match[2] === 'stop' ? resource.kind === 'terminal' ? 'terminated' : 'closed' : match[2] === 'delete' ? 'deleted' : resource.status,
          thread_associations: match[2] === 'detach' ? [] : resource.thread_associations };
        records[index] = records[index].map(item => item.resource_id === updated.resource_id ? updated : item);
        if (mode === 'unknown') throw new TypeError('连接在写入后断开');
        return ok(updated);
      };
      if (mode === 'accepted-close') {
        init.signal?.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { releaseWrite = () => resolve(response()); });
      }
      return response();
    }
    throw new Error(`非预期资源请求：${method} ${path}`);
  });
  const quick = { quickAccess: { show: (value, options) => shows.push({ value, options }) }, async pick(items, options) {
    choices.push({ title: options.title, items: items.map(item => ({ label: item.label, action: item.action, thread: item.thread })) });
    if (mode === 'action-cancel') return undefined;
    if (mode === 'identity') return new Promise(resolve => { releaseChoice = () => resolve(items.find(item => item.action === 'stop')); });
    if (items[0]?.action) return items.find(item => item.action === (mode === 'picker-attach' ? 'attach' : 'stop'));
    return items[0];
  } };
  const origin = new ThreadInput({ ...owners[1], workspaceKey: gatewayWorkspaceKey(owners[1]), sessionId: thread.session_id, threadId: thread.thread_id, title: '精确 child' });
  const editors = { activeEditor: origin, editors: [origin] };
  const notifications = { error: cause => errors.push(String(cause)), info: value => notices.push(value),
    status: value => { notices.push(value); return { close() { notices.push('pending-closed'); } }; } };
  const commands = { executeCommand(id, ...args) {
    let valid = true;
    const accessor = { get(service) {
      if (!valid) throw new Error('异步使用 ServicesAccessor');
      if (service === IQuickInputService) return quick;
      if (service === IEditorService) return editors;
      if (service === INotificationService) return notifications;
      if (service === IDialogService) return { confirm: async options => { confirmations.push(options); return { confirmed: mode !== 'delete-cancel' }; } };
      throw new Error(`额外服务：${service}`);
    } };
    try { return CommandsRegistry.getCommand(id).handler(accessor, ...args); }
    finally { valid = false; }
  } };
  const picker = { value: mode === 'empty-title' ? 'resource browser_same' : 'resource Needle', items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerButton: buttons.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const provider = new ResourceQuickAccess(quick, {}, { activeGroup: { id: 7 } }, {}, notifications, commands, editors);
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: owners });
  const until = async predicate => { for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error(`运行资源合同未收敛：${JSON.stringify({ errors, requests, choices, rows: picker.items.map(item => item.label) })}`); };
  let result;
  try {
    if (mode.startsWith('pending-')) {
      await until(() => releaseRead);
      if (mode === 'pending-close') lifetime.dispose(); else invalidateGatewayUserSession(0);
      releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
      return { requests, aborted, rows: picker.items.map(item => ({ id: item.id, label: item.label })), errors };
    }
    await until(() => !picker.busy && picker.items.length === (mode === 'empty-title' ? 2 : 4));
    const rows = picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail }));
    const discoveryCount = requests.length;
    if (mode === 'empty-title' || mode === 'id-search') {
      picker.value = 'resource browser_same'; changes.fire(picker.value); await until(() => !picker.busy && picker.items.length === 2);
      result = picker.items.map(item => ({ id: item.id, label: item.label, highlights: item.highlights }));
      lifetime.dispose();
    } else if (['browse', 'parallel', 'mismatch'].includes(mode)) {
      picker.value = 'resource 同名'; changes.fire(picker.value); await until(() => !picker.busy);
      lifetime.dispose();
    } else if (['action-cancel', 'identity', 'picker-attach', 'accepted-close'].includes(mode)) {
      picker.selectedItems = picker.activeItems = [picker.items[2]]; accepts.fire({ inBackground: false });
      if (mode === 'identity') { await until(() => releaseChoice); invalidateGatewayUserSession(0); releaseChoice(); }
      if (mode === 'accepted-close') { await until(() => releaseWrite); lifetime.dispose(); cancellation.cancel(); releaseWrite(); }
      await until(() => errors.length || shows.length || notices.includes('pending-closed'));
    } else {
      const action = ['promote', 'attach', 'detach', 'delete'].includes(mode) ? mode : mode === 'delete-cancel' ? 'delete' : 'stop';
      const target = { owner: { ...owners[1] }, resource: structuredClone(records[1][0]) };
      if (mode === 'illegal-attach') { target.resource.retention_scope = 'thread'; target.thread = { session_id: 'ses_wrong', thread_id: 'thr_wrong' }; }
      if (mode === 'source' || mode === 'source-mismatch') target.source = { workspaceKey: gatewayWorkspaceKey(owners[1]), resource: { ...target.resource, owner_revision: mode === 'source' ? 7 : 8 },
        async run(action, ref) { sourceCalls.push({ action, ref }); return { ...target.resource, owner_revision: 8 }; } };
      requests.push({ phase: 'command' });
      const operation = commands.executeCommand(resourceActionCommand(mode === 'illegal-attach' ? 'attach' : action), target).catch(cause => errors.push(String(cause)));
      if (mode === 'freeze') {
        await until(() => releaseRoutes); target.owner.gatewayId = owners[0].gatewayId; target.resource.owner_revision = 99;
        editors.activeEditor = undefined; workspaceContext.publish({ ...initial, activeId: gatewayWorkspaceKey(owners[0]) }); releaseRoutes();
      }
      result = await operation;
    }
    return { requests, rows, choices, shows, errors, notices, confirmations, sourceCalls, result, discoveryCount, aborted, records, maximum };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose(); buttons.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
