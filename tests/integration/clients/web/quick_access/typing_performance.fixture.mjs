import * as services from './query.fixture.ts';
import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { Emitter, Event } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { invalidateGatewayToken } from '../../../../../src/clients/web/src/api/http.js';

/** 真实逐键 Provider、QueryBuilder、评分与传输；仅替换 HTTP、Picker 和编辑器宿主。 */
export async function typingSearchContract(mode) {
  const before = workspaceContext.getSnapshot(), originalFetch = globalThis.fetch;
  const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001' };
  const wireOwner = { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId };
  const target = { ...wireOwner, status: 'ready', name: '逐键工作区', remote_gateways: [] };
  const root = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
  const frozen = { version: 1, gateway_id: owner.gatewayId, resolved_at: '2026-11-12T00:00:00Z', mode: 'selected', owners: [wireOwner] };
  const requests = [], metadata = [], errors = [], roots = [], changes = new Emitter(), accepts = new Emitter();
  const cancellation = new CancellationTokenSource(), deferredRoot = Promise.withResolvers();
  const config = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
    search: { useIgnoreFiles: false, useGlobalIgnoreFiles: false, useParentIgnoreFiles: false, followSymlinks: true } };
  const instantiation = new services.InstantiationService(new services.ServiceCollection(
    [services.IConfigurationService, { getValue: () => config }], [services.IEditorGroupsService, { groups: [] }],
    [services.ILogService, new services.NullLogService()], [services.IPathService, { userHome: () => services.URI.parse('file:///home/native') }],
    [services.IUriIdentityService, { extUri: services.extUri, onDidChange: Event.None }],
  ), true);
  const editors = { openEditor() { throw new Error('逐键搜索不得打开编辑器'); } };
  const group = { id: 7 }, groups = { groups: [], activeGroup: group, getGroup: () => group };
  let items = [], updates = 0;
  const picker = { value: '', buttons: [], activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false },
    get items() { return items; }, set items(value) { updates++; items = value; this.selectedItems = []; },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidHide: Event.None, onDidChangeActive: Event.None,
    onDidTriggerButton: Event.None, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const history = [{ resource: services.workspaceFileUri(owner, `${root}/main-history.txt`, root), label: 'main-history.txt' }];
  const provider = new WorkspaceQuickAccess({
    createInstance: (ctor, ...args) => ctor === services.PickerEditorState ? new services.PickerEditorState(editors, groups) : instantiation.createInstance(ctor, ...args),
    createChild: (...args) => instantiation.createChild(...args),
  }, editors, groups, { getHistory: () => history }, { getValue: () => undefined }, {}, {},
  { lookupKeybinding: () => undefined }, {}, { error: error => errors.push(String(error)) }, { enabled: false }, {});
  const ok = data => Response.json({ code: 0, data, request_id: 'typing-metadata' }, { headers: { 'X-Request-ID': 'typing-metadata' } });
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const until = async predicate => {
    for (let i = 0; i < 400; i++) { if (predicate()) return; await wait(5); }
    throw new Error(`逐键条件未收敛：${JSON.stringify({ requests: requests.length, metadata, items: items.map(item => item.label), busy: picker.busy })}`);
  };
  const change = value => { picker.value = value; changes.fire(); };
  workspaceContext.publish({ ...before, gatewayId: owner.gatewayId, workspaces: [target] });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname;
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'typing-contract' } });
    if (path === '/api/gateway/workspaces') { metadata.push(path); return ok({ items: [target] }); }
    if (path === '/api/v1/workspace/files/roots') {
      metadata.push(path); roots.push(init.signal);
      if (mode === 'in-flight' || mode === 'close-root') await deferredRoot.promise;
      if (mode === 'retry' && roots.length === 1) return Response.json({ detail: '文件根暂时不可用' }, { status: 503, headers: { 'X-Request-ID': 'root-failure' } });
      return ok({ workspace_uri: root });
    }
    if (path !== '/api/gateway/search') throw new Error(`逐键搜索出现额外请求：${path}`);
    const connection = { request: JSON.parse(init.body), signal: init.signal, id: `typing-${requests.length}`, sequence: 0 };
    requests.push(connection);
    return new Response(new ReadableStream({ start(controller) { connection.controller = controller; } }),
      { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': connection.id } });
  };
  const finish = connection => {
    const branches = connection.request.queries.map(query => ({ branch: { owner: wireOwner, category: query.category, operation: query.operation,
      ...(query.category === 'file' ? { resource_uri: root } : {}) }, state: 'complete', allocated_results: 50,
      returned_results: query.category === 'file' ? 2 : 0, retryable: false }));
    const events = [{ type: 'scope', scope: { scope: frozen, branches: [{ owner: wireOwner, state: 'registered' }] } }];
    for (const branch of branches) {
      if (branch.branch.category === 'file') events.push({ type: 'page', branch, page: { provider_id: 'files', workspace_id: owner.workspaceId,
        category: 'file', operation: 'name', contract_version: 1, state: 'complete', files: {
          node: { uri: root, resource_shape: 'directory', operations: ['search'] },
          entries: ['src/main.ts', 'tests/main.ts'].map(path => ({ uri: `${root}/${path}`, resource_shape: 'file', operations: ['read'] })),
          matches: [], contexts: [], view: 'live', truncated: false,
        } } });
      events.push({ type: 'branch', branch });
    }
    events.push({ type: 'end', summary: { state: 'complete', order: 'category-scope-owner-v1', returned_results: 2, branches } });
    connection.controller.enqueue(new TextEncoder().encode(events.map(event => {
      const sequence = ++connection.sequence;
      return `id: ${sequence}\nevent: search\ndata: ${JSON.stringify({ sequence, request_id: connection.id, ...event })}\n\n`;
    }).join('')));
    connection.controller.close();
  };
  let lifetime;
  try {
    lifetime = provider.provide(picker, cancellation.token, { searchOwner: owner });
    change('m');
    const synchronousHistory = picker.items.some(item => item.label === 'main-history.txt');
    if (mode === 'identity' || mode === 'close') {
      if (mode === 'identity') invalidateGatewayToken(0); else lifetime.dispose();
      await wait(260);
      return { requests: requests.length, metadata, synchronousHistory, labels: picker.items.map(item => item.label), busy: picker.busy, errors };
    }
    if (mode === 'in-flight' || mode === 'close-root') {
      await until(() => roots.length === 1 && requests.length === 1);
      const first = requests[0];
      if (mode === 'close-root') {
        lifetime.dispose();
        const aborted = roots[0].aborted;
        deferredRoot.resolve(); await wait(50);
        return { aborted, requests: requests.length, busy: picker.busy, errors };
      }
      change('main'); await wait(240);
      const retained = !roots[0].aborted, oldCancelled = first.signal.aborted;
      deferredRoot.resolve();
      await until(() => requests.length === 3);
      requests.slice(1).forEach(finish); await until(() => !picker.busy);
      return { retained, oldCancelled, rootReads: roots.length, registryReads: metadata.filter(path => path === '/api/gateway/workspaces').length,
        labels: picker.items.map(item => item.label), requests: requests.map(item => item.request), errors };
    }
    await wait(35); change('ma'); await wait(35); change('main');
    const earlyRequests = requests.length;
    await until(() => requests.length === (mode === 'retry' ? 1 : 2) && (mode !== 'retry' || items.some(item => item.label.includes('文件根暂时不可用'))));
    requests.forEach(finish); await until(() => !picker.busy);
    const firstCalls = requests.length, firstRoots = roots.length;
    const countBefore = updates;
    change('main src');
    await until(() => requests.length === firstCalls + 2);
    requests.slice(firstCalls).forEach(finish); await until(() => !picker.busy);
    const candidates = picker.items.filter(item => item.resource).map(item => ({ label: item.label, description: item.description, highlights: item.highlights }));
    const publications = updates - countBefore;
    const afterSecond = requests.length;
    change('main'); await wait(35); change('main src');
    await until(() => requests.length === afterSecond + 2);
    requests.slice(afterSecond).forEach(finish); await until(() => !picker.busy);
    lifetime.dispose();
    const beforeClose = requests.length;
    lifetime = provider.provide(picker, cancellation.token, { searchOwner: owner });
    await until(() => requests.length === beforeClose + 2);
    requests.slice(beforeClose).forEach(finish); await until(() => !picker.busy);
    return { synchronousHistory, earlyRequests, firstCalls, firstRoots, candidates, publications,
      metadata, rootReads: roots.length, requests: requests.map(item => item.request), errors };
  } finally {
    lifetime?.dispose(); cancellation.dispose(true); provider.dispose(); instantiation.dispose(); changes.dispose(); accepts.dispose();
    deferredRoot.resolve(); globalThis.fetch = originalFetch; workspaceContext.publish(before);
  }
}
