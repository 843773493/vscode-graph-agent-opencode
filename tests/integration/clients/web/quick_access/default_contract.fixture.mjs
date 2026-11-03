import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { workspaceFileUri } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
import { HelpQuickAccessProvider } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/helpQuickAccess.js';
import { Registry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { Extensions } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { WorkspaceSymbolsQuickAccess } from '../../../../../src/clients/workbench/src/search/symbolsQuickAccess.js';
import { WorkspaceSymbolProviderRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/search/common/search.js';
import { invalidateGatewayUserSession } from '../../../../../src/clients/web/src/api/http.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { querySearchBranches } from '../../../../../src/clients/workbench/src/search/quickAccessBranches.js';
import { gateway } from '../../../../../src/clients/workbench/src/agentApi.js';
import { HttpRequestError } from '../../../../../src/clients/web/src/api/http.js';
import { DisposableStore } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
export { contentBranchContract } from './content_branches.fixture.mjs';

const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const uri = name => workspaceFileUri(owner, root + '/' + name, root);

/** 仅替原 Picker 事件和宿主服务；实际 Provider、取消及接受逻辑均来自生产源码。 */
function defaultAccess(options, history = [], opener = { open() { throw new Error('delegate 不应被 URI opener 抢占'); } }) {
  const changes = new Emitter(), accepts = new Emitter(), cancellation = new CancellationTokenSource();
  const picker = { value: '', items: [], activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false },
    buttons: [], onDidTriggerButton: Event.None,
    onDidHide: Event.None, onDidChangeActive: Event.None, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const groups = { groups: [], activeGroup: { id: 1 } }, editors = { openEditor() { throw new Error('只选择调用方不应打开编辑器'); } };
  const provider = new WorkspaceQuickAccess({ createInstance: () => new PickerEditorState(editors, groups) }, editors, groups,
    { getHistory: () => history }, { getValue: () => undefined }, {}, {}, {}, {},
    opener, { error(error) { throw error; } }, { enabled: false }, {});
  const lifetime = provider.provide(picker, cancellation.token, options);
  return { picker, accepts, change(value) { picker.value = value; changes.fire(); },
    dispose() { cancellation.dispose(true); lifetime.dispose(); provider.dispose(); changes.dispose(); accepts.dispose(); } };
}

export function uriContract() {
  const calls = [];
  for (const uri of [root + '/name%40target.txt', root + '/name%40target.txt#L2', 'https://example.test/file']) {
    const f = defaultAccess({ searchOwner: owner }, [], { open(resource, options) {
      calls.push({ uri: resource, group: options.boxteamTargetGroup, side: options.openToSide,
        pinned: options.editorOptions.pinned, preserveFocus: options.editorOptions.preserveFocus });
      return Promise.resolve(true);
    } });
    try {
      f.change(uri);
      f.picker.keyMods.alt = true;
      f.picker.activeItems = f.picker.selectedItems = [f.picker.items[0]];
      f.accepts.fire({ inBackground: true });
    } finally { f.dispose(); }
  }
  return calls;
}

export async function delegateContract() {
  const calls = [], observed = {};
  const f = defaultAccess({ includeOpenToSideButton: false, fileSearchDelegate: {
    search(pattern, token) { return new Promise(resolve => calls.push({ pattern, token, resolve })); },
    getItemDescription: () => '调用方目录',
  } });
  try {
    observed.initial = calls.map(call => call.pattern);
    f.change('https://example.test/file');
    observed.patterns = calls.map(call => call.pattern);
    observed.cancelledPrevious = calls[0].token.isCancellationRequested;
    f.change('delegate');
    calls[2].resolve({ results: [{ resource: uri('delegate.txt') }], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.picks = f.picker.items.map(item => ({ label: item.label, description: item.description, buttons: item.buttons }));
    calls[0].resolve({ results: [{ resource: uri('old.txt') }], messages: [] });
    calls[1].resolve({ results: [], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.afterLate = f.picker.items.map(item => item.label);
    f.change('cancel');
  } finally { f.dispose(); }
  observed.cancelledOnDispose = calls.at(-1).token.isCancellationRequested;
  calls.at(-1).resolve({ results: [], messages: [] });
  return observed;
}

export async function identityContract() {
  const results = [];
  for (const kind of ['user', 'gateway']) {
    const before = workspaceContext.getSnapshot();
    workspaceContext.publish({ ...before, gatewayId: 'gateway-a' });
    const calls = [];
    const f = defaultAccess({ fileSearchDelegate: { search(pattern, token) {
      return new Promise(resolve => calls.push({ pattern, token, resolve }));
    } } });
    try {
      if (kind === 'user') invalidateGatewayUserSession(0);
      else workspaceContext.publish({ ...before, gatewayId: 'gateway-b' });
      const cancelled = calls[0].token.isCancellationRequested;
      calls[0].resolve({ results: [{ resource: uri('old-user.txt') }], messages: [] });
      await Promise.resolve(); await Promise.resolve();
      f.change('new query');
      results.push({ kind, cancelled, calls: calls.length, busy: f.picker.busy,
        labels: f.picker.items.map(item => item.label), selected: f.picker.selectedItems.length });
    } finally { f.dispose(); workspaceContext.publish(before); }
  }
  // 已释放 Provider 不再订阅失效通知。
  invalidateGatewayUserSession(0);
  return results;
}

export async function branchContract(mode) {
  const originalRoutes = gateway.workspaceRoutes, originalRequest = gateway.request, originalTimer = globalThis.setTimeout;
  const token = new CancellationTokenSource(), lifetime = new DisposableStore();
  const calls = [], updates = [], drained = Promise.withResolvers(), entered = Promise.withResolvers();
  const owners = Array.from({ length: mode === 'retry' ? 2 : 4 }, (_, index) => ({ ...owner, workspaceId: `workspace-${index}` }));
  gateway.workspaceRoutes = async () => ({ gatewayId: owner.gatewayId, items: owners.map((target, index) => ({
    workspace_id: `route-${index}`, backend_workspace_id: target.workspaceId, connection_kind: 'local', status: 'ready', name: `owner-${index}`,
  })) });
  gateway.request = async (_path, options, route) => {
    const call = { route, signal: options.signal, deadline: Number(options.headers['X-BoxTeam-Name-Search-Deadline']) };
    calls.push(call);
    if (mode === 'retry') {
      if (route === 'route-0' && calls.filter(item => item.route === route).length === 1) {
        throw new HttpRequestError(503, 'Unavailable', '原 owner 读取失败', _path, 'request-failure');
      }
      options.onResponse('request-' + route);
      return { items: [{ node: { kind: 'session', session_id: route, name: 'needle-' + route }, relative_path: route }] };
    }
    if (calls.length === 3) entered.resolve();
    // 故意不合作的传输替身，用于证明迟到结果不会越过代际；产品 HTTP 自己响应 signal。
    return await new Promise(resolve => { call.release = resolve; });
  };
  if (mode === 'timeout') globalThis.setTimeout = (handler, ms, ...args) => originalTimer(handler, ms === 15000 ? 450 : ms, ...args);
  let busy = false;
  try {
    const branches = querySearchBranches(owners, ['会话'], 'needle', lifetime, token.token, {}, {}, {},
      item => ({ id: item.sessionId, label: item.title, accept() {} }),
      branches => updates.push(branches.map(branch => ({ count: branch.picks.length, label: branch.status.label }))),
      value => { busy = value; if (!value) drained.resolve(); }, () => {});
    if (mode === 'retry') {
      await drained.promise;
      const failed = branches[0].status, successful = branches[1].picks[0];
      await failed.trigger();
      return { counts: owners.map((_, index) => calls.filter(call => call.route === `route-${index}`).length),
        error: failed.label, errorDetail: failed.detail, successDetail: branches[1].status.detail,
        deadlines: calls.map(call => call.deadline),
        preserved: branches[1].picks[0] === successful, picks: branches.map(branch => branch.picks.map(pick => pick.id)), busy };
    }
    await entered.promise;
    const before = calls.length;
    if (mode === 'cancel') { token.cancel(); lifetime.dispose(); }
    else await drained.promise;
    await Promise.resolve(); await Promise.resolve();
    const beforeLate = JSON.stringify(updates);
    for (const call of calls) call.release({ items: [{ node: { kind: 'session', session_id: 'late', name: 'needle-late' }, relative_path: 'late' }] });
    await new Promise(resolve => originalTimer(resolve, 10));
    return { before, after: calls.length, aborted: calls.every(call => call.signal.aborted), unchanged: JSON.stringify(updates) === beforeLate,
      busy, labels: branches.map(branch => branch.status.label), picks: branches.flatMap(branch => branch.picks) };
  } finally {
    token.dispose(true); lifetime.dispose(); gateway.workspaceRoutes = originalRoutes; gateway.request = originalRequest; globalThis.setTimeout = originalTimer;
  }
}

export function selectContract() {
  const received = [], observed = {};
  const f = defaultAccess({ searchOwner: owner, additionPicks: [{ label: '附件来源', kind: 'contextPick' }, { label: '隐藏项' }],
    filter: item => item.label !== '隐藏项', handleAccept: (item, background) => received.push({ label: item.label, kind: item.kind, background }) },
  [{ resource: uri('history.txt') }, new ThreadInput({ ...owner, routeId: '', sessionId: 'session-a', threadId: 'thread-a', title: '历史 Thread' })]);
  try {
    observed.labels = f.picker.items.filter(item => item.type !== 'separator').map(item => item.label);
    f.picker.activeItems = [f.picker.items[0]];
    f.accepts.fire({ inBackground: true });
    observed.backgroundKeptOpen = !f.picker.hidden;
    f.accepts.fire({ inBackground: false });
    observed.foregroundClosed = f.picker.hidden;
    observed.received = received;
    return observed;
  } finally { f.dispose(); }
}

export async function symbolContract() {
  const calls = [], queried = Promise.withResolvers();
  const registration = WorkspaceSymbolProviderRegistry.register({ provideWorkspaceSymbols(query) { calls.push(query); queried.resolve(); return []; } });
  const provider = new WorkspaceSymbolsQuickAccess({}, {}, {}, { getValue: () => ({}) }, {}, {});
  const changes = new Emitter(), accepts = new Emitter(), token = new CancellationTokenSource();
  const picker = { value: '#needle', items: [], activeItems: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None };
  const lifetime = [];
  try {
    lifetime.push(provider.provide(picker, token.token, { searchOwner: owner }));
    // 原全局查询有 200ms debounce；等待越过该边界，不能在它尚未执行时宣称未查询。
    await new Promise(resolve => setTimeout(resolve, 250));
    const scopedLabel = picker.items[0].label;
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token, { searchOwnerError: '原 Gateway 身份失效' }));
    await new Promise(resolve => setTimeout(resolve, 250));
    const errorLabel = picker.items[0].label;
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token, { searchOwners: [owner, { ...owner, workspaceId: 'workspace-b' }] }));
    await new Promise(resolve => setTimeout(resolve, 250));
    const multiLabel = picker.items[0].label, multiPlaceholder = picker.placeholder;
    const scopedCalls = [...calls];
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token));
    await queried.promise;
    return { scopedLabel, errorLabel, multiLabel, multiPlaceholder, scopedCalls, nativeCalls: calls };
  } finally {
    token.dispose(true);
    lifetime.forEach(item => item.dispose());
    provider.dispose(); registration.dispose(); changes.dispose(); accepts.dispose();
  }
}

export function helpContract() {
  const registry = Registry.as(Extensions.Quickaccess), registrations = [], accepts = new Emitter(), changes = new Emitter();
  const shows = [], handleAccept = () => {}, options = { searchOwner: owner, handleAccept, includeHelp: true };
  for (const prefix of ['', '_contract ']) registrations.push(registry.registerQuickAccessProvider({ prefix,
    ctor: class {}, helpEntries: [{ description: '合同装具' }] }));
  const help = new HelpQuickAccessProvider({ quickAccess: { show: (value, options) => shows.push({ value, options }) } },
    { lookupKeybinding: () => undefined }, {});
  const picker = { onDidAccept: accepts.event, onDidChangeValue: changes.event, selectedItems: [], items: [] };
  const cancellation = new CancellationTokenSource(), lifetime = help.provide(picker, cancellation.token, options);
  try {
    picker.selectedItems = [picker.items.find(item => item.prefix === '')];
    accepts.fire();
    changes.fire('?_contract ');
    return { values: shows.map(show => show.value), preserve: shows.every(show => show.options.preserveValue),
      sameOwner: shows.every(show => show.options.providerOptions.searchOwner === owner),
      sameReceiver: shows.every(show => show.options.providerOptions.handleAccept === handleAccept),
      includeHelp: shows.map(show => show.options.providerOptions.includeHelp), sourceHelpUnchanged: options.includeHelp };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); accepts.dispose(); changes.dispose();
    for (const registration of registrations) registration.dispose();
  }
}
