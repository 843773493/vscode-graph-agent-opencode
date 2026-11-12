import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ResourceOpener } from '../../../../../src/clients/workbench/src/resourceOpener.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { workspaceFileSource } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { DisposableStore } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import * as queryFixture from './query.fixture.ts';
import { setLocaleResources } from '../../../../../src/clients/shared/core/i18n/index.js';
import english from '../../../../../src/clients/shared/core/i18n/messages/en.json';
import chinese from '../../../../../src/clients/shared/core/i18n/messages/zh-cn.json';

/** 真实 Provider、SSE 消费器和 Opener；HTTP 回执及编辑器宿主为明确装具。 */
export async function canonicalSearchContract({ category, mismatch = false, omitUri = false, locale = 'zh-cn' }) {
  setLocaleResources(locale, locale === 'en' ? english : chinese);
  const previous = workspaceContext.getSnapshot(), fetch = globalThis.fetch, lifetime = new DisposableStore();
  const cancellation = new CancellationTokenSource();
  const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
  const targets = owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: '同名工作区', status: 'ready', connection_kind: 'local' }));
  const names = { '会话': 'catalog', 'Thread': 'thread', '文件': 'file', '会话消息': 'message', '工具结果': 'tool_result', 'LLM 请求记录': 'llm_request', 'Trace': 'trace' };
  const content = !['会话', 'Thread', '文件'].includes(category), operation = content ? 'content' : 'name';
  const roots = owners.map(owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`);
  // Session 沿原本机简称，Thread 沿原带 Gateway 地址；接受时必须保留相应 owner。
  const sessionUri = index => `boxteam://workspace/${owners[index].workspaceId}/resources/session/session`;
  const threadUri = index => `boxteam://${owners[index].gatewayId}/workspace/${owners[index].workspaceId}/resources/session/session/threads/child`;
  const uris = owners.map((_, index) => category === '文件' ? roots[index] + '/needle-canonical.ts'
    : category === 'Thread' || category === '会话消息' || category === '工具结果' ? threadUri(index) : sessionUri(index));
  const thread = index => ({ workspace_id: owners[index].workspaceId, session_id: 'session', thread_id: 'child', thread_uri: omitUri ? '' : threadUri(index),
    kind: 'child', title: 'needle Thread', session_title: 'needle Session', created_at: '2026-11-12T00:00:00Z' });
  const matches = [2, 8].map(start => ({ turn_id: 'turn', message_id: 'message', role: category === '工具结果' ? 'tool' : 'user', preview: 'needle 命中',
    preview_start_codepoint: 0, tool_name: category === '工具结果' ? 'tool' : null, tool_call_id: category === '工具结果' ? 'call' : null,
    reference: { item_id: 'item', content_hash: 'a'.repeat(64), start_codepoint: start, end_codepoint: start + 2, category: names[category] } }));
  const reference = start => ({ session_id: mismatch ? 'other-session' : 'session', content_version: 'b'.repeat(64), start_codepoint: start, end_codepoint: start + 2 });
  const domain = index => category === '文件' ? { files: { node: { uri: roots[index], resource_shape: 'directory', operations: ['search'] },
    entries: [{ uri: roots[index] + '/needle-alias.ts', canonical_uri: uris[index], resource_shape: 'file', operations: ['read'] }], matches: [], contexts: [], view: 'live', truncated: false } }
    : category === '会话' ? { catalog: { revision: 'r1', catalog_revision: 1, total: 2, items: [
      { node: { node_id: 'folder', folder_id: 'folder', kind: 'folder', name: 'needle Folder', node_revision: 1 }, breadcrumb: [], relative_path: 'needle Folder' },
      { node: { node_id: 'session', session_id: 'session', kind: 'session', name: 'needle Session', node_revision: 1,
        session: { session_id: 'session', workspace_id: owners[index].workspaceId, uri: omitUri ? '' : uris[index], resource_identity: 'stable-session', scope: 'workspace',
          title: 'needle Session', current_agent_id: 'agent', created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z' } },
      breadcrumb: [], relative_path: 'needle Folder/needle Session' },
    ] } }
      : category === 'Thread' ? { threads: { catalog_revision: 1, items: [thread(index)], complete: true, scanned_sessions: 1, scanned_threads: 1 } }
        : category === '会话消息' || category === '工具结果' ? { messages: { catalog_revision: 1, cursor: null, limited_reason: null, items: matches.map(match => ({ thread: thread(index), match })),
          complete: true, scanned_sessions: 1, scanned_threads: 1, scanned_items: 1, scanned_bytes: 10 } }
          : category === 'LLM 请求记录' ? { llm_requests: { catalog_revision: 1, complete: true, scanned_nodes: 1, scanned_records: 1, scanned_bytes: 10,
            items: [2, 8].map(start => ({ session_uri: uris[index], session_title: 'needle Session', match: {
              reference: { ...reference(start), file_name: '1.json' }, timestamp: 1, preview: 'needle 命中', preview_start_codepoint: 0 } })) } }
            : { traces: { catalog_revision: 1, complete: true, scanned_nodes: 1, scanned_records: 1, scanned_bytes: 10,
              items: [2, 8].map(start => ({ session_uri: uris[index], session_title: 'needle Session', match: {
                reference: { ...reference(start), thread_id: 'child', event_id: 'event', line_start: 0, line_end: 10 },
                event_type: 'agent_end', timestamp: '2026-11-12T00:00:00Z', preview: 'needle 命中', preview_start_codepoint: 0 } })) } };
  const paths = [], parses = [], requests = [], opened = [], errors = [], group = { id: 7 };
  let addedGroups = 0;
  const groups = { activeGroup: group, groups: [], getGroup: id => ({ id }), findGroup: () => undefined,
    addGroup: () => { addedGroups++; return { id: 8 }; } };
  const configuration = { getValue: key => key === 'search.quickOpen.includeHistory' ? false : key === 'workbench.editor.openSideBySideDirection' ? 'right' : undefined };
  const editors = { async openEditor(input, options, id) {
    if (input instanceof ThreadInput) { lifetime.add(input); opened.push({ owner: { ...input.owner }, label: input.getName(), options, group: id }); }
    else opened.push({ owner: workspaceFileSource(input.resource), options: input.options, group: options });
  } };
  const ok = data => Response.json({ code: 0, message: 'ok', request_id: 'canonical-search', data }, { headers: { 'X-Request-ID': 'canonical-search' } });
  workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, workspaces: targets });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname; paths.push(path);
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'canonical-search' } });
    if (path === '/api/gateway/workspaces') return ok({ items: targets });
    if (path === '/api/gateway/health') return ok({ gateway_id: owners[0].gatewayId });
    if (path === '/api/v1/workspace/files/roots') return ok({ workspace_uri: roots[new Headers(init.headers).get('X-BoxTeam-Target-Gateway-Id') === owners[0].gatewayId ? 0 : 1] });
    if (path === '/api/gateway/resource-addresses/parse') {
      const body = JSON.parse(init.body); parses.push(body);
      return ok({ gateway_id: body.resource_gateway_id, scope: 'workspace', scope_id: owners[1].workspaceId, uri: body.uri, reference_uri: body.uri,
        kind: category === '文件' ? 'files' : 'session', session_id: category === '文件' ? null : 'session',
        thread_id: ['Thread', '会话消息', '工具结果'].includes(category) ? 'child' : null, file_root_uri: category === '文件' ? roots[1] : null });
    }
    if (path === '/api/v1/sessions/session/main-thread') return ok({ session_id: 'session', thread_id: 'main' });
    if (path !== '/api/gateway/search') throw new Error(`规范引用出现额外读取：${path}`);
    requests.push(JSON.parse(init.body));
    const scope = { version: 1, gateway_id: owners[0].gatewayId, mode: 'selected', resolved_at: '2026-11-12T00:00:00Z',
      owners: owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId })) };
    const branches = scope.owners.map((owner, index) => ({ branch: { owner, category: names[category], operation, ...(category === '文件' ? { resource_uri: roots[index] } : {}) },
      state: 'complete', retryable: false, returned_results: content || category === '会话' ? 2 : 1, allocated_results: 50 }));
    const events = [{ type: 'scope', scope: { scope, branches: scope.owners.map(owner => ({ owner, state: 'registered' })) } },
      ...branches.flatMap((branch, index) => [{ type: 'page', branch, page: { provider_id: 'original-owner', workspace_id: owners[index].workspaceId,
        category: names[category], operation, contract_version: 1, state: 'complete', ...domain(index) } }, { type: 'branch', branch }]),
      { type: 'end', summary: { state: 'complete', order: 'category-scope-owner-v1', returned_results: branches.reduce((sum, branch) => sum + branch.returned_results, 0), branches } }];
    return new Response(events.map((event, index) => `id: ${index + 1}\nevent: search\ndata: ${JSON.stringify({ ...event, sequence: index + 1, request_id: 'canonical-search' })}\n\n`).join(''),
      { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': 'canonical-search' } });
  };
  const opener = lifetime.add(new ResourceOpener({ registerOpener: () => ({ dispose() {} }) }, editors, groups, configuration,
    { createModelReference() { throw new Error('搜索接受不得提前读取只读模型'); } }, {}, { getAssociationsForResource: () => [] }));
  const changes = lifetime.add(new Emitter()), accepts = lifetime.add(new Emitter()), buttons = lifetime.add(new Emitter());
  const prefix = content ? 'content ' : category === '会话' ? 'session ' : category === 'Thread' ? 'thread ' : '';
  class Provider extends WorkspaceQuickAccess {
    static PREFIX = prefix;
    get category() { return content ? undefined : category; }
    get contentMode() { return content; }
  }
  const fileInstantiation = new queryFixture.InstantiationService(new queryFixture.ServiceCollection(
    [queryFixture.IConfigurationService, { getValue: () => ({ editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
      search: { useIgnoreFiles: false, useGlobalIgnoreFiles: false, useParentIgnoreFiles: false, followSymlinks: true } }) }],
    [queryFixture.IEditorGroupsService, { groups: [] }], [queryFixture.ILogService, new queryFixture.NullLogService()],
    [queryFixture.IPathService, { userHome: () => queryFixture.URI.parse('file:///home/native') }],
    [queryFixture.IUriIdentityService, { extUri: queryFixture.extUri, onDidChange: Event.None }],
  ), true);
  const instantiation = { createInstance: (ctor, ...args) => ctor === PickerEditorState ? new PickerEditorState(editors, groups) : fileInstantiation.createInstance(ctor, ...args),
    createChild: (...args) => fileInstantiation.createChild(...args) };
  const provider = lifetime.add(new Provider(instantiation, editors, groups, { getHistory: () => [] }, configuration, {}, {}, {}, opener,
    { error: error => errors.push(String(error)) }, { enabled: false }, {}));
  const picker = { value: prefix + 'needle', items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerItemButton: buttons.event, onDidHide: Event.None,
    onDidChangeActive: Event.None, onDidTriggerButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const until = async predicate => {
    for (let count = 0; count < 200; count++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error(`规范引用接受未收敛：${JSON.stringify({ errors, paths, labels: picker.items.map(item => item.label) })}`);
  };
  try {
    lifetime.add(provider.provide(picker, cancellation.token, { searchOwners: owners, ...(content ? { contentCategories: [category] } : {}) }));
    await until(() => requests.length === 1 && !picker.busy);
    if (omitUri) return { errors, paths, opened, labels: picker.items.map(item => item.label) };
    const candidates = picker.items.filter(item => item.resource?.authority === owners[1].gatewayId || item.sessionTarget?.owner.gatewayId === owners[1].gatewayId);
    if (!candidates.length) throw new Error(`未取得 B 的原搜索候选：${JSON.stringify(picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail })))}`);
    const beforeAccept = [...paths], ids = candidates.map(item => item.id);
    groups.activeGroup = { id: 99 };
    // 首个命中侧开，第二位置后台接受；发起组及原位置分别保持。
    buttons.fire({ item: candidates[0], button: candidates[0].buttons[0] });
    await until(() => opened.length === 1 || errors.length);
    if (mismatch) return { errors, paths, parses, opened, addedGroups };
    if (candidates.length > 1) { picker.selectedItems = [candidates[1]]; accepts.fire({ inBackground: true }); await until(() => opened.length === 2 || errors.length); }
    return { errors, beforeAccept, paths, parses, opened, ids, expectedUri: uris[1], expectedOwner: owners[1], requests,
      labels: picker.items.map(item => item.label), placeholder: picker.placeholder, buttonTooltips: candidates[0].buttons.map(button => button.tooltip),
      folderHasResource: picker.items.some(item => item.id?.startsWith('folder:') && item.resource) };
  } finally { cancellation.dispose(true); lifetime.dispose(); fileInstantiation.dispose(); globalThis.fetch = fetch; workspaceContext.publish(previous); setLocaleResources('zh-cn', chinese); }
}
