import * as queryFixture from './query.fixture.ts';
import { queryFederatedSearchBranches } from '../../../../../src/clients/workbench/src/search/quickAccessFederation.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { DisposableStore } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { IconLabel } from '../../../../../src/clients/workbench/upstream/src/vs/base/browser/ui/iconLabel/iconLabel.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { invalidateGatewayUserSession } from '../../../../../src/clients/web/src/api/http.js';

/** 真实名称／正文分支与共享 SSE 消费器，只有 Gateway 网络响应为显式装具。 */
export function fileBranchContract(mode) { return federatedBranchContract(mode, '文件'); }
export function contentBranchContract(mode) { return federatedBranchContract(mode, '会话消息'); }
export function nameBranchContract({ mode, category }) { return federatedBranchContract(mode, category); }
export function cursorRejectionContract(rejection) { return federatedBranchContract('cursor-rejection', rejection.category, rejection); }
export function branchStateContract(diagnostic) { return federatedBranchContract('terminal-state', '会话消息', diagnostic); }

async function federatedBranchContract(mode, category, diagnostic) {
  const originalFetch = globalThis.fetch, before = workspaceContext.getSnapshot();
  const names = { '会话消息': 'message', '会话': 'catalog', 'Thread': 'thread', '文件': 'file', 'LLM 请求记录': 'llm_request', 'Trace': 'trace' };
  const file = category === '文件';
  const content = category === '会话消息' || category === 'LLM 请求记录' || category === 'Trace', operation = content ? 'content' : 'name';
  const owners = ['gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']
    .map(gatewayId => ({ gatewayId, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
  const roots = owners.map(owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`);
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
    connection_kind: index === 0 ? 'local' : 'remote_gateway', status: 'ready', name: `名称 ${index === 0 ? 'A' : 'B'}` }));
  const frozen = { version: 1, gateway_id: owners[0].gatewayId, resolved_at: '2026-11-03T00:00:00Z', mode: 'selected',
    owners: owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId })) };
  const configuration = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
    search: { useIgnoreFiles: false, useGlobalIgnoreFiles: false, useParentIgnoreFiles: false, followSymlinks: true } };
  const instantiation = file ? new queryFixture.InstantiationService(new queryFixture.ServiceCollection(
    [queryFixture.IConfigurationService, { getValue: () => configuration }], [queryFixture.IEditorGroupsService, { groups: [] }],
    [queryFixture.ILogService, new queryFixture.NullLogService()], [queryFixture.IPathService, { userHome: () => queryFixture.URI.parse('file:///home/native') }],
    [queryFixture.IUriIdentityService, { extUri: queryFixture.extUri, onDidChange: queryFixture.Event.None }],
  ), true) : undefined;
  const lifetime = new DisposableStore(), token = new CancellationTokenSource(), connections = [], paths = [], rootRequests = [];
  workspaceContext.publish({ ...before, gatewayId: owners[0].gatewayId, workspaces: targets });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname; paths.push(path);
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'content-test' } });
    const metadata = data => Response.json({ code: 0, message: 'ok', data, request_id: 'metadata' }, { headers: { 'X-Request-ID': 'metadata' } });
    if (file && ['/api/gateway/users/current', '/api/gateway/users/guest'].includes(path)) return metadata({ kind: 'guest', user_id: null });
    if (file && path === '/api/gateway/health') return metadata({ gateway_id: owners[0].gatewayId });
    if (file && path === '/api/gateway/workspaces') return metadata({ items: targets, active_workspace: frozen.owners[0] });
    if (file && path === '/api/v1/workspace/files/roots') {
      const headers = new Headers(init.headers), requested = { gateway_id: headers.get('X-BoxTeam-Target-Gateway-Id'),
        workspace_id: headers.get('X-BoxTeam-Workspace-Id') };
      rootRequests.push(requested);
      const index = owners.findIndex(owner => owner.gatewayId === requested.gateway_id && owner.workspaceId === requested.workspace_id);
      if (index < 0) throw new Error('文件根请求丢失完整 owner');
      return metadata({ workspace_uri: roots[index] });
    }
    if (path !== '/api/gateway/search') throw new Error(`正文搜索出现额外请求：${path}`);
    if (new Headers(init.headers).get('Content-Type') !== 'application/json') throw new Error('搜索请求缺少 JSON Content-Type');
    const connection = { signal: init.signal, request: JSON.parse(init.body), sequence: 0, cancelled: false, id: `request-${connections.length}` };
    connections.push(connection);
    const response = new Response(new ReadableStream({ start(controller) { connection.controller = controller; }, cancel() { connection.cancelled = true; } }),
      { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': connection.id } });
    if (mode === 'late') return new Promise(resolve => { connection.release = () => resolve(response); });
    return response;
  };
  const until = async predicate => {
    for (let count = 0; count < 200; count++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('正文分支条件未收敛');
  };
  const send = (connection, type, payload) => {
    const sequence = ++connection.sequence;
    connection.controller.enqueue(new TextEncoder().encode(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify({ sequence, request_id: connection.id, type, ...payload })}\n\n`));
  };
  const scope = connection => send(connection, 'scope', { scope: { scope: frozen,
    branches: frozen.owners.map(owner => ({ owner, state: 'registered' })) } });
  const state = (index, status = 'complete') => ({ branch: { owner: frozen.owners[index], category: names[category], operation, ...(file ? { resource_uri: roots[index] } : {}) },
    state: status, retryable: status === 'failed', allocated_results: 50, returned_results: status === 'failed' ? 0 : 1,
    ...(status === 'limited' ? { code: 'result_limit', cursor: 'A-original-cursor' } : {}),
    ...(status === 'failed' ? { code: 'upstream_error', message: 'B 原始读取失败' } : {}) });
  const page = (connection, index, start, status = 'complete') => {
    const cursor = status === 'limited' ? 'A-original-cursor' : null;
    send(connection, 'page', { branch: state(index, status), page: { provider_id: 'canonical-history', workspace_id: owners[index].workspaceId,
      category: names[category], operation, contract_version: 1, state: status,
      ...(status === 'limited' ? { limited_reason: 'result_limit' } : {}),
      ...(file ? { files: { node: { uri: roots[index], resource_shape: 'directory', operations: ['search'] },
        entries: [{ uri: `${roots[index]}/needle-${start}.txt`, resource_shape: 'file', operations: ['read'] }],
        matches: [], contexts: [], view: 'live', truncated: status === 'limited', next_cursor: cursor } } : content ? { messages: { catalog_revision: 1, cursor, complete: status === 'complete', limited_reason: cursor ? 'result_limit' : null,
        scanned_sessions: 1, scanned_threads: 1, scanned_items: 1, scanned_bytes: 5,
        items: [{ thread: { workspace_id: owners[index].workspaceId, session_id: 'session', thread_id: 'thread',
          thread_uri: 'boxteam://session/session', kind: 'child', title: '同名 Thread', session_title: '同名 Session', created_at: '2026-11-03T00:00:00Z' },
          match: { turn_id: 'turn', message_id: 'same-message', role: 'user', preview: '相同😀命中', preview_start_codepoint: 0,
            reference: { item_id: 'same-item', content_hash: 'same-hash', start_codepoint: start, end_codepoint: start + 2, category: 'message' } } }] } }
        : category === '会话' ? { catalog: { revision: 'r1', catalog_revision: 1, cursor, total: 2, items: [
          { node: { node_id: 'same-session', kind: 'session', name: `needle Session ${start}`, session_id: 'same-session',
            has_children: false, node_revision: 1, state: 'active' }, breadcrumb: [], relative_path: 'needle Session' },
          { node: { node_id: 'same-folder', kind: 'folder', name: `needle Folder ${start}`, folder_id: 'same-folder',
            has_children: true, node_revision: 1, state: 'active' }, breadcrumb: [], relative_path: 'needle Folder' },
        ] } } : { threads: { catalog_revision: 1, cursor, complete: status === 'complete', limited_reason: cursor ? 'result_limit' : null,
          scanned_sessions: 1, scanned_threads: 1, items: [{ workspace_id: owners[index].workspaceId,
            session_id: 'same-session', thread_id: `same-child-${start}`, thread_uri: 'boxteam://session/same-session',
            kind: 'child', title: `needle Child ${start}`, session_title: 'needle Session', created_at: '2026-11-03T00:00:00Z' }] } }),
    } });
    send(connection, 'branch', { branch: state(index, status) });
  };
  const end = (connection, branches) => {
    send(connection, 'end', { summary: { state: branches.every(branch => branch.state === 'complete') ? 'complete' : 'incomplete',
      order: 'category-scope-owner-v1', returned_results: branches.reduce((count, branch) => count + branch.returned_results, 0), branches } });
    connection.controller.close();
  };
  let busy = false, updates = 0, label;
  try {
    if (mode === 'multi-selection') {
      const changes = lifetime.add(new Emitter()), accepts = lifetime.add(new Emitter());
      let items = [];
      const picker = { value: 'needle', canSelectMany: true, selectedItems: [], activeItems: [], buttons: [],
        keyMods: { alt: false, ctrlCmd: false }, onDidChangeValue: changes.event, onDidAccept: accepts.event,
        onDidHide: Event.None, onDidChangeActive: Event.None, onDidTriggerButton: Event.None,
        onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None,
        get items() { return items; },
        // 原 QuickPick 重建列表会清除旧勾选；不能用不清选择的普通数组掩盖 publish 缺口。
        set items(value) { items = value; this.selectedItems = []; },
      };
      const group = { id: 1 }, groups = { groups: [], activeGroup: group, getGroup: () => group };
      const editors = { openEditor() { throw new Error('多选 Provider 合同不得打开编辑器'); } };
      const provider = lifetime.add(new WorkspaceQuickAccess({
        createInstance: (ctor, ...args) => ctor === PickerEditorState ? new PickerEditorState(editors, groups) : instantiation.createInstance(ctor, ...args),
        createChild: (...args) => instantiation.createChild(...args),
      }, editors, groups, { getHistory: () => [] }, { getValue: () => undefined }, {}, {},
      { lookupKeybinding: () => undefined }, {}, { error(error) { throw error; } }, { enabled: false }, {}));
      lifetime.add(provider.provide(picker, token.token, { filesOnly: true, includeOpenToSideButton: false, searchOwners: owners }));
      await until(() => connections.length === 1); const first = connections[0]; scope(first);
      page(first, 0, 2, 'limited');
      const emptyB = { ...state(1), returned_results: 0 };
      send(first, 'branch', { branch: emptyB }); end(first, [state(0, 'limited'), emptyB]);
      await until(() => !picker.busy && picker.items.some(item => item.resource));
      const original = picker.items.find(item => item.resource), firstId = original.id;
      const initiallyPickable = original.pickable;
      const statusPickable = picker.items.filter(item => item.type !== 'separator' && !item.resource).map(item => item.pickable);
      picker.selectedItems = [original];
      const continuing = picker.items.find(item => item.buttons?.some(button => button.tooltip === '继续查询此类别')).trigger();
      await until(() => connections.length === 2); const next = connections[1]; scope(next);
      page(next, 0, 4); end(next, [state(0)]); await continuing;
      const resources = picker.items.filter(item => item.resource), currentFirst = resources.find(item => item.id === firstId);
      const selectedIds = picker.selectedItems.map(item => item.id), currentSelectedObject = picker.selectedItems[0] === currentFirst;
      const currentStatusPickable = picker.items.filter(item => item.type !== 'separator' && !item.resource).map(item => item.pickable);
      const query = picker.value;
      invalidateGatewayUserSession(0);
      await until(() => picker.items.some(item => item.id === 'identity-invalidated'));
      return { initiallyPickable, statusPickable, currentStatusPickable, firstId, selectedIds, currentSelectedObject,
        replacedFirstObject: original !== currentFirst, resourceIds: resources.map(item => item.id), resourcePickable: resources.map(item => item.pickable),
        query, requests: connections.map(connection => connection.request), invalidatedSelectedIds: picker.selectedItems.map(item => item.id),
        invalidatedActiveIds: picker.activeItems.map(item => item.id) };
    }
    const branches = queryFederatedSearchBranches(owners, [category], content ? '😀命中' : 'needle', lifetime, token.token, {},
      (item, owner) => ({ id: `${owner.gatewayId}/${owner.workspaceId}/${content ? item.message.reference.start_codepoint : `${item.kind}/${item.title}`}`, label: item.title, accept() {} }),
      () => updates++, value => { busy = value; }, value => { label = value; }, instantiation ?? {});
    await until(() => connections.length === 1); const first = connections[0];
    if (mode === 'disconnect-before-scope') {
      first.controller.close();
      await until(() => !busy);
      const details = branches.map(branch => branch.status.detail), labels = branches.map(branch => branch.status.label);
      const retry = branches[1].status.trigger();
      await until(() => connections.length === 2); const next = connections[1]; scope(next);
      page(next, 0, 2); page(next, 1, 2); end(next, [state(0), state(1)]); await retry;
      return { details, labels, requests: connections.map(connection => connection.request), paths,
        pending: branches.map(branch => branch.pending), busy, picks: branches.map(branch => branch.picks.map(pick => pick.id)) };
    }
    scope(first);
    if (mode === 'terminal-state') {
      page(first, 0, 2, 'limited'); page(first, 1, 2); end(first, [state(0, 'limited'), state(1)]);
      await until(() => !busy);
      const original = branches.map(branch => branch.picks[0]);
      const continuing = branches[0].status.trigger();
      await until(() => connections.length === 2); const next = connections[1]; scope(next);
      const terminal = { ...state(0), ...diagnostic, returned_results: 0, cursor: 'A-original-cursor' };
      send(next, 'branch', { branch: terminal }); end(next, [terminal]); await continuing;
      const statuses = branches.map(branch => ({ label: branch.status.label, detail: branch.status.detail,
        buttons: branch.status.buttons?.map(button => button.tooltip) ?? [], hasTrigger: typeof branch.status.trigger === 'function' }));
      const retainedPicks = branches.map(branch => branch.picks.map(pick => pick.id));
      const originalRetained = original.map((pick, index) => pick === branches[index].picks[0]);
      const callsBeforeRetry = connections.length;
      if (diagnostic.retryable) {
        const retry = branches[0].status.trigger();
        await until(() => connections.length === 3); const last = connections[2]; scope(last);
        page(last, 0, 4); end(last, [state(0)]); await retry;
      }
      return { statuses, retainedPicks, originalRetained, callsBeforeRetry, requests: connections.map(connection => connection.request),
        otherRetainedAfterRetry: original[1] === branches[1].picks[0], picks: branches.map(branch => branch.picks.map(pick => pick.id)),
        pending: branches.map(branch => branch.pending), busy };
    }
    if (mode === 'cursor-rejection') {
      const completed = index => ({ ...state(index), returned_results: 0 });
      const limited = { ...state(1, 'limited'), cursor: 'B-rejected-cursor', returned_results: 0 };
      send(first, 'branch', { branch: completed(0) }); send(first, 'branch', { branch: limited });
      end(first, [completed(0), limited]); await until(() => !busy);
      const continuation = branches[1].status.trigger();
      await until(() => connections.length === 2); const next = connections[1]; scope(next);
      const failure = { ...state(1, 'failed'), code: diagnostic.code, error_owner: diagnostic.owner, reason: diagnostic.reason,
        http_status: diagnostic.status, cursor: limited.cursor, message: '原 owner 拒绝此续点 [执行](command:workbench.action.closeWindow) <b>诊断</b>' };
      send(next, 'branch', { branch: failure }); end(next, [failure]); await continuation;
      const failureLabel = branches[1].status.label, failureDetail = branches[1].status.detail,
        retryTooltip = branches[1].status.buttons[0].tooltip;
      // 使用 Quick Input 原标签渲染器核对文本，不在测试内复制 Markdown 或命令解析。
      const rendering = lifetime.add(new IconLabel(document.createElement('div'), { supportHighlights: true, supportIcons: true }));
      rendering.setLabel(failureLabel, failureDetail);
      const diagnosticText = rendering.element.textContent, diagnosticLinks = rendering.element.querySelectorAll('a[href]').length,
        diagnosticMarkup = rendering.element.querySelectorAll('b').length;
      const retry = branches[1].status.trigger();
      await until(() => connections.length === 3); const last = connections[2]; scope(last);
      send(last, 'branch', { branch: completed(1) }); end(last, [completed(1)]); await retry;
      return { requests: connections.map(connection => connection.request), paths, failureLabel, failureDetail, retryTooltip, busy, diagnosticText, diagnosticLinks, diagnosticMarkup,
        pending: branches.map(branch => branch.pending), status: branches.map(branch => branch.status.detail) };
    }
    if (mode === 'cancel' || mode === 'late') {
      const count = updates; lifetime.dispose();
      if (mode === 'late') { page(first, 0, 2); page(first, 1, 2); end(first, [state(0), state(1)]); first.release(); }
      if (mode === 'late') await new Promise(resolve => setTimeout(resolve, 20));
      else await until(() => first.cancelled);
      return { cancelled: first.signal.aborted, unchanged: count === updates, calls: connections.length, paths };
    }
    if (mode === 'disconnect') {
      page(first, 0, 2); first.controller.close();
      await until(() => !busy);
      return { labels: branches.map(branch => branch.status.label), details: branches.map(branch => branch.status.detail),
        picks: branches.map(branch => branch.picks.map(pick => pick.id)) };
    }
    page(first, 0, 2, 'limited'); await until(() => !branches[0].pending);
    const originalPick = branches[0].picks[0];
    const continuing = branches[0].status.trigger();
    await until(() => connections.length === 2); const next = connections[1]; scope(next);
    send(first, 'branch', { branch: state(1, 'failed') }); end(first, [state(0, 'limited'), state(1, 'failed')]);
    await until(() => !branches[1].pending); await new Promise(resolve => setTimeout(resolve, 5));
    const pendingPreserved = branches[0].pending, partialFailure = branches[1].status.label;
    page(next, 0, 4); end(next, [state(0)]); await continuing;
    const retry = branches[1].status.trigger();
    await until(() => connections.length === 3); const last = connections[2]; scope(last); page(last, 1, 2); end(last, [state(1)]); await retry;
    return { requests: connections.map(connection => connection.request), paths, rootRequests, pendingPreserved, partialFailure,
      originalPreserved: originalPick === branches[0].picks[0], picks: branches.map(branch => branch.picks.map(pick => pick.id)), busy, label,
      status: branches.map(branch => branch.status.detail), highlights: branches.map(branch => branch.picks.map(pick => pick.highlights)) };
  } finally { instantiation?.dispose(); lifetime.dispose(); token.dispose(true); globalThis.fetch = originalFetch; workspaceContext.publish(before); }
}
