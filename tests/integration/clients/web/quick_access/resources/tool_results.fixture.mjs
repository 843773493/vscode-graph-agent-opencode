import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const OWNER_A = { gatewayId: `gateway_${'a'.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' };
const OWNER_B = { gatewayId: `gateway_${'b'.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000002' };
const SESSION = 'ses_tool_result_child';
const THREAD = 'thr_tool_result_child';
const ITEMS = [
  { item_id: 'tool-result-item-a', turn_id: 'turn-a', tool_call_id: 'call-a', tool_name: 'test_tool_2', status: 'completed' },
  { item_id: 'tool-result-item-b', turn_id: 'turn-b', tool_call_id: 'call-b', tool_name: 'large_test_output', status: 'completed' },
];

/** 运行原 ResourceQuickAccess Provider；HTTP 只作为具名回执边界。 */
export async function toolResultsContract(mode = 'browse') {
  const previous = workspaceContext.getSnapshot();
  const changes = new Emitter(), accepts = new Emitter(), itemButtons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], opened = [], errors = [];
  const owners = [OWNER_A, OWNER_B];
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
    name: `工具结果目标 ${index}`, status: 'ready', connection_kind: 'local' }));
  workspaceContext.publish({ ...previous, gatewayId: OWNER_A.gatewayId, workspaces: targets,
    activeId: gatewayWorkspaceKey(OWNER_A), loading: false, error: null });
  const ok = data => Response.json({ code: 0, data, request_id: 'tool-results-contract' },
    { headers: { 'X-Request-ID': 'tool-results-contract' } });
  const child = new ThreadInput({ ...OWNER_B, sessionId: SESSION, threadId: THREAD, workspaceKey: '', title: 'child 工具结果' });
  const editors = { activeEditor: mode === 'no-thread' ? undefined : child,
    async openEditor(input, options, group) { opened.push({ owner: { ...input.owner }, options, group }); } };
  let groupsAdded = 0;
  const groups = { activeGroup: { id: 7 }, getGroup: id => id === 7 ? { id } : undefined,
    findGroup: () => undefined, addGroup: () => { groupsAdded += 1; return { id: 8 }; } };
  let releaseRead, aborted = false, pageRequestCount = 0;
  installSessionApiFetchMock(async ({ path, url, init, method }) => {
    const parsed = new URL(url, 'http://127.0.0.1'), query = parsed.searchParams;
    const headers = new Headers(init?.headers);
    const gatewayId = headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    requests.push({ path, query: parsed.search, method, gatewayId, workspaceId });
    if (path === '/api/gateway/workspaces') return ok({ items: targets });
    if (path.endsWith('/config/sources')) return ok({ sources: [] });
    if (path.endsWith('/configuration-schemas')) return ok({ items: [] });
    if (path.endsWith('/attachments')) return ok({ session_id: SESSION, thread_id: THREAD, items: [] });
    if (path.endsWith('/changesets')) return ok({ session_id: SESSION, items: [] });
    if (path.endsWith('/agent-spec')) return ok({ node: { uri: 'AGENTS', resource_shape: 'file', operations: ['read'] } });
    if (path.endsWith('/skill-sources') || path.endsWith('/local-skills')) return ok({ workspace_id: workspaceId, entries: [] });
    if (path.endsWith('/gateway-skills')) return ok({ gateway_id: gatewayId, workspace_id: workspaceId, available: true, entries: [] });
    if (path === '/api/v1/resources') return ok({ workspace_id: workspaceId, items: [] });
    if (path.endsWith('/mcp/servers')) return Response.json({ code: 0, request_id: 'tool-results-mcp', protocol_version: 1,
      target: { kind: 'workspace', gateway_id: gatewayId, workspace_id: workspaceId }, catalog_revision: `sha256:${'0'.repeat(64)}`,
      catalog_generation: 1, data: [] }, { headers: { 'X-Request-ID': 'tool-results-mcp' } });
    if (path.endsWith('/tool-results')) {
      pageRequestCount += 1;
      const after = query.get('after');
      if (mode === 'cancel' || mode === 'page-cancel' && after === '2') {
        return await new Promise(resolve => {
          releaseRead = () => resolve(ok({ session_id: SESSION, thread_id: THREAD, items: ITEMS.slice(1),
            source_view_id: 'view-child', source_view_revision: 12 }));
          init.signal?.addEventListener('abort', () => { aborted = true; }, { once: true });
        });
      }
      if (mode === 'wrong-thread') return ok({ session_id: SESSION, thread_id: 'thr-not-the-child', items: ITEMS,
        source_view_id: 'view-child', source_view_revision: 12 });
      if (mode === 'empty') return ok({ session_id: SESSION, thread_id: THREAD, items: [], next_after: null,
        source_view_id: null, source_view_revision: 0 });
      if (after !== null) {
        if (after !== '2' || query.get('view_id') !== 'view-child' || query.get('view_revision') !== '12') {
          throw new Error(`续页未冻结原 history view：${parsed.search}`);
        }
        return ok({ session_id: SESSION, thread_id: THREAD, items: [ITEMS[1]], next_after: null,
          source_view_id: 'view-child', source_view_revision: 12 });
      }
      return ok({ session_id: SESSION, thread_id: THREAD, items: [ITEMS[0]], next_after: 2,
        source_view_id: 'view-child', source_view_revision: 12 });
    }
    if (path === '/api/v1/resources/skill-sources') return ok({ workspace_id: workspaceId, entries: [] });
    throw new Error(`未声明的工具结果 Provider 请求 ${method} ${path}`);
  });
  const picker = { value: 'resource ', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: false, ctrlCmd: false }, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerButton: Event.None, onDidTriggerItemButton: itemButtons.event, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const provider = new ResourceQuickAccess({}, {}, groups, { getValue: () => true },
    { error: cause => errors.push(String(cause)) }, {}, editors);
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: owners });
  const until = async predicate => {
    for (let index = 0; index < 400; index += 1) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    throw new Error('工具结果 Provider 合同未收敛');
  };
  try {
    if (mode === 'cancel') {
      await until(() => releaseRead !== undefined);
      lifetime.dispose();
      releaseRead();
      await new Promise(resolve => setTimeout(resolve, 20));
    } else {
      await until(() => !picker.busy && (mode === 'empty' || picker.items.length > 0));
      if (mode === 'browse') {
        const count = requests.filter(item => item.path.endsWith('/tool-results')).length;
        picker.value = 'resource tool'; changes.fire(picker.value);
        await until(() => !picker.busy);
        if (requests.filter(item => item.path.endsWith('/tool-results')).length !== count) throw new Error('工具名筛选重复读取目录');
      } else if (mode === 'page') {
        const more = picker.items.find(item => item.label === '更多工具结果…');
        itemButtons.fire({ item: more, button: more.buttons[0] });
        itemButtons.fire({ item: more, button: more.buttons[0] });
        await until(() => picker.items.some(item => item.resourceId === ITEMS[1].item_id));
      } else if (mode === 'page-cancel') {
        const more = picker.items.find(item => item.label === '更多工具结果…');
        itemButtons.fire({ item: more, button: more.buttons[0] });
        await until(() => releaseRead !== undefined);
        lifetime.dispose(); releaseRead();
      } else if (mode === 'select' || mode === 'side' || mode === 'freeze') {
        await until(() => picker.items.some(item => item.resourceId === ITEMS[0].item_id));
        const selected = picker.items.find(item => item.resourceId === ITEMS[0].item_id);
        if (mode === 'freeze') editors.activeEditor = new ThreadInput({ ...OWNER_A,
          sessionId: 'ses-later', threadId: 'thr-later', workspaceKey: '', title: '后来打开的 Thread' });
        selected.accept({ alt: mode === 'side', ctrlCmd: false }, { inBackground: false });
        await until(() => opened.length > 0 || errors.length > 0);
      }
    }
    return { requests, opened, errors, aborted, pageRequestCount, groupsAdded,
      rows: picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail,
        resourceId: item.resourceId, selectable: typeof item.accept === 'function' })),
      owner: OWNER_B, sessionId: SESSION, threadId: THREAD, items: ITEMS };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose(); itemButtons.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
