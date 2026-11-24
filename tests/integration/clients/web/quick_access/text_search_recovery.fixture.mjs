import '../../../../harness/js/workbench-locale.ts';
import * as q from './query.fixture.ts';
import * as s from './search.fixture.ts';
import { PickerHost } from './callers/picker.fixture.mjs';
import { Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IContextKeyService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IWebWorkerService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import { QuickAccessController } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/quickAccess.js';
import { Extensions } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { Registry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { IEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ISearchService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/search/common/search.js';
import { IModelService } from '../../../../../src/clients/workbench/upstream/src/vs/editor/common/services/model.js';
import { IFileService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { ITelemetryService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IExtensionService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { ILabelService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/label/common/label.js';
import { IViewsService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/views/common/viewsService.js';
import { IReplaceService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/search/browser/replace.js';
import { INotebookSearchService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/search/common/notebookSearch.js';
import { INotebookEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/notebook/browser/services/notebookEditorService.js';
import { WorkspaceTextQuickAccess } from '../../../../../src/clients/workbench/src/search/textQuickAccess.js';
import { QuickInputController } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/quickInputController.js';
import { ContextKeyService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/browser/contextKeyService.js';
import { IContextMenuService, IContextViewService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/contextview/browser/contextView.js';
import { ContextViewService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/contextview/browser/contextViewService.js';
import { ILayoutService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/layout/browser/layoutService.js';
import { IListService, ListService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/list/browser/listService.js';
import { IKeybindingService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IThemeService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/theme/common/themeService.js';
import { IAccessibilityService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/accessibility/common/accessibility.js';
import { InMemoryStorageService, IStorageService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/storage/common/storage.js';

const owners = [{ gatewayId: 'gateway-a', workspaceId: 'workspace-a' }, { gatewayId: 'gateway-b', workspaceId: 'workspace-a' }];
const root = owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
const uri = (owner, name) => s.workspaceFileUri(owner, root(owner) + '/' + name, root(owner));
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function until(predicate, diagnostic = () => undefined) {
  const deadline = Date.now() + 3000;
  while (!predicate()) { if (Date.now() > deadline) throw new Error('原 Picker 未在期限内达到预期状态：' + JSON.stringify(diagnostic())); await tick(); }
}
const hit = (target, name) => ({ uri: target + '/' + name, line: 1, column: 1, end_line: 1, end_column: 7,
  utf16_column: 1, utf16_end_column: 7, snippet_start_utf16_column: 1, text: 'needle ' + name, snippet_start_column: 1, truncated: false });
const page = (target, name) => ({ node: { uri: target, resource_shape: 'directory', operations: ['search'] },
  entries: [], matches: [hit(target, name)], contexts: [], truncated: false, view: 'live' });
const labels = picker => picker.items.map(item => item.label);
const matched = picker => picker.items.filter(item => item.match).map(item => item.match.parent().resource.toString());
const diagnosticItems = picker => picker.items.filter(item => item.buttons?.some(button => button.tooltip === '复制安全诊断'));
async function copyDiagnostics(picker) {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard'), diagnostics = [];
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { diagnostics.push(text); } } });
  try {
    for (const item of diagnosticItems(picker)) {
      const count = diagnostics.length;
      picker.press(item, item.buttons.findIndex(button => button.tooltip === '复制安全诊断'));
      await until(() => diagnostics.length === count + 1);
    }
    return diagnostics;
  } finally {
    if (original) Object.defineProperty(navigator, 'clipboard', original); else delete navigator.clipboard;
  }
}

/** 宿主事件及 HTTP 页是明确替身；原 Controller、TextQuickAccess、QueryBuilder、SearchModel 和 Provider 实际运行。 */
async function fixture(asset, searchOverride) {
  const lifetime = new q.DisposableStore(), host = lifetime.add(new PickerHost()), calls = [], buttons = [], opened = [];
  const create = host.createQuickPick.bind(host);
  host.createQuickPick = () => {
    const picker = create(), pressed = lifetime.add(new Emitter());
    picker.onWillHide = q.Event.None; picker.onDidTriggerItemButton = pressed.event;
    picker.press = (item, index = 0) => { buttons.push(item); pressed.fire({ item, button: item.buttons[index] }); };
    return picker;
  };
  const context = lifetime.add(new q.SearchWorkspaceContext({ version: 1, workspaces: owners.map((owner, index) => ({ ...owner, rootUri: root(owner), label: String(index) })) }));
  const configuration = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
    search: { useIgnoreFiles: false, followSymlinks: true, searchOnType: true, quickAccess: { preserveInput: false }, sortOrder: 'default', maxResults: 100 },
    workbench: { editor: { enablePreview: true, enablePreviewFromQuickOpen: true } } };
  const editors = { editors: [], visibleEditors: [], activeEditor: undefined, openEditor: async (...args) => { opened.push(args); } };
  const services = new q.ServiceCollection(
    [q.IConfigurationService, { getValue: key => typeof key === 'string' ? key.split('.').reduce((value, part) => value?.[part], configuration) : configuration }],
    [q.IWorkspaceContextService, context], [q.IEditorGroupsService, { groups: [], activeGroup: { id: 1 }, getGroups: () => [] }],
    [q.ILogService, new q.NullLogService()], [q.IPathService, { userHome: () => q.URI.parse('file:///home/native') }],
    [q.IUriIdentityService, { extUri: q.extUri, onDidChange: q.Event.None }], [IQuickInputService, host], [IEditorService, editors],
    [IContextKeyService, { getContextKeyValue: () => undefined }],
    [IWebWorkerService, { createWorkerClient() { throw new Error('业务查询不应启动本地文件worker'); } }],
    [IModelService, { getModels: () => [], getModel: () => undefined, onModelAdded: q.Event.None }],
    [IFileService, { exists: async () => true, hasProvider: () => true }],
    [ITelemetryService, { publicLog() {}, publicLog2() {} }],
    [IExtensionService, { activateByEvent: async () => {}, whenInstalledExtensionsRegistered: async () => {} }],
    [ILabelService, { getUriLabel: resource => resource.path, getUriBasenameLabel: resource => resource.path.split('/').at(-1) }],
    [IReplaceService, {}], [IViewsService, {}],
    [INotebookEditorService, { onDidAddNotebookEditor: q.Event.None, retrieveExistingWidgetFromURI: () => undefined }],
    [INotebookSearchService, { notebookSearch: () => ({ openFilesToScan: new s.ResourceSet(),
      allScannedFiles: Promise.resolve(new s.ResourceSet()), completeData: Promise.resolve({ results: [], messages: [] }) }) }],
  );
  const instantiation = lifetime.add(new q.InstantiationService(services, true));
  services.set(ISearchService, searchOverride ?? instantiation.createInstance(s.WorkspaceSearchService));
  const registry = Registry.as(Extensions.Quickaccess);
  lifetime.add(registry.registerQuickAccessProvider({ prefix: '%', ctor: WorkspaceTextQuickAccess, helpEntries: [] }));
  const controller = lifetime.add(instantiation.createInstance(QuickAccessController)); host.connect(controller);
  const workspaces = s.gateway.workspaces, request = s.gateway.request;
  s.gateway.workspaces = async () => ({ items: owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
    id: owner.workspaceId, name: String(index), status: 'ready' })) });
  // 根回执按真实 owner 分区，不把B的URI误写成A。
  s.gateway.request = async (path, _init, key) => {
    if (path !== '/api/v1/workspace/files/roots') throw new Error('意外目录元数据请求：' + path);
    const [gatewayId, workspaceId] = JSON.parse(key);
    return { workspace_uri: root({ gatewayId, workspaceId }) };
  };
  s.installSessionApiFetchMock(async ({ path, init }) => {
    if (path !== '/api/gateway/search') throw new Error('意外查询请求：' + path);
    const request = JSON.parse(init.body), id = 'picker-request-' + calls.length;
    calls.push({ request, signal: init.signal });
    const result = await asset(request, calls.length - 1, init.signal);
    if (result.http) return new Response(JSON.stringify({ detail: result.detail }), { status: result.http,
      headers: { 'content-type': 'application/json', 'X-Request-ID': id } });
    if (result.throw) throw result.throw;
    const scope = request.scope.scope ?? { version: 1, gateway_id: 'gateway-current', resolved_at: '2026-11-12T00:00:00Z', mode: 'selected', owners: request.scope.owners };
    let sequence = 0; const frames = [], states = [];
    const emit = (type, payload) => { const event = { sequence: ++sequence, request_id: id, type, ...payload };
      frames.push(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify(event)}\n\n`); };
    emit('scope', { scope: { scope, branches: scope.owners.map(owner => ({ owner, state: 'registered' })) } });
    for (const query of request.queries) for (const target of query.file_targets) {
      const branch = { owner: target.owner, category: 'file', operation: query.operation, resource_uri: target.target.uri };
      const selected = request.continuations.find(item => JSON.stringify(item.branch) === JSON.stringify(branch));
      if (request.continuations.length && !selected) continue;
      const failed = result.failures?.find(failure => failure.owner === target.owner.gateway_id);
      const state = { branch, state: failed?.state ?? (result.limited ? 'limited' : 'complete'),
        ...(result.limited && !failed ? { code: 'result_limit' } : {}), retryable: !!failed?.retryable, allocated_results: query.limit,
        returned_results: failed ? 0 : 1, ...(failed ? { code: 'execution_failed', message: '原分支 [不执行](command:close) <b>正文</b>',
          error_owner: 'workspace_files', reason: failed.state === 'cancelled' ? 'request_cancelled' : 'read_failed', ...failed, owner: undefined } : {}) };
      states.push(state);
      if (!failed) emit('page', { branch: state, page: { provider_id: 'workspace-files-content', workspace_id: target.owner.workspace_id,
        category: 'file', operation: 'content', contract_version: 1, state: 'complete', files: page(target.target.uri, result.name ?? 'ok.txt') } });
      emit('branch', { branch: state });
    }
    if (!result.incomplete) emit('end', { summary: { state: states.every(state => state.state === 'complete') ? 'complete' : 'incomplete',
      order: 'category-scope-owner-v1', returned_results: states.reduce((count, state) => count + state.returned_results, 0), branches: states } });
    return new Response(frames.join(''), { headers: { 'content-type': 'text/event-stream', 'X-Request-ID': id } });
  });
  return { host, calls, buttons, opened, controller, show(pattern = 'needle') {
    controller.show('%' + pattern, { providerOptions: { searchOwners: owners } }); return host.currentQuickInput;
  }, close() { lifetime.dispose(); s.unwrapSessionApiFetch(); s.gateway.workspaces = workspaces; s.gateway.request = request; } };
}

export async function identityChange(mode, phase) {
  const previous = s.workspaceContext.getSnapshot();
  s.workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, activeId: owners[0].workspaceId });
  let release; const barrier = new Promise(resolve => { release = resolve; });
  const f = await fixture(async (_request, index) => {
    if (phase === 'initial' && !index || phase === 'retry' && index === 1) await barrier;
    return phase !== 'initial' && !index ? { failures: [{ owner: owners[0].gatewayId, state: 'failed', retryable: true }] }
      : { name: index ? 'new-identity.txt' : 'late.txt' };
  });
  try {
    const picker = f.show();
    if (phase === 'initial') await until(() => f.calls.length === 1);
    else await until(() => !picker.busy && picker.items.some(item => item.buttons?.[0]?.tooltip === '重新查询此文件分支'));
    const stale = picker.items.find(item => item.buttons?.[0]?.tooltip === '重新查询此文件分支');
    const oldMatch = picker.items.find(item => item.match);
    if (phase === 'retry') { picker.press(stale); await until(() => f.calls.length === 2); }
    if (oldMatch) picker.activeItems = picker.selectedItems = [oldMatch];
    const before = f.calls.length;
    if (mode === 'credential') s.invalidateGatewayToken(0);
    else {
      s.workspaceContext.publish({ ...s.workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
      s.workspaceContext.publish({ ...s.workspaceContext.getSnapshot(), gatewayId: owners[0].gatewayId });
    }
    const invalidated = { labels: labels(picker), matches: matched(picker), selected: picker.selectedItems.length,
      active: picker.activeItems.length, buttons: picker.buttons.length, busy: picker.busy,
      aborted: f.calls.at(-1).signal.aborted };
    if (stale) picker.press(stale);
    if (oldMatch) picker.accept(oldMatch, true);
    picker.value = '%changed'; release(); await tick(); await tick();
    const unchanged = JSON.stringify(labels(picker)) === JSON.stringify(invalidated.labels), staleRequests = f.calls.length - before;
    picker.hide(); const reopened = f.show('newidentity');
    await until(() => !reopened.busy && matched(reopened).some(resource => resource.endsWith('/new-identity.txt')),
      () => ({ labels: labels(reopened), busy: reopened.busy, calls: f.calls.length, visible: reopened.visible }));
    return { invalidated, unchanged, staleRequests, opened: f.opened.length, reopened: matched(reopened),
      requests: f.calls.map(call => call.request) };
  } finally { release(); f.close(); s.workspaceContext.publish(previous); }
}

export async function activeWorkspaceSwitch() {
  const previous = s.workspaceContext.getSnapshot();
  s.workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, activeId: owners[0].workspaceId });
  let release; const barrier = new Promise(resolve => { release = resolve; });
  const f = await fixture(async () => { await barrier; return { name: 'frozen.txt' }; });
  try {
    const picker = f.show(); await until(() => f.calls.length === 1);
    s.workspaceContext.publish({ ...s.workspaceContext.getSnapshot(), activeId: 'another-workspace' });
    const aborted = f.calls[0].signal.aborted; release();
    await until(() => !picker.busy && matched(picker).length === 2);
    return { aborted, matches: matched(picker), requests: f.calls.map(call => call.request) };
  } finally { release(); f.close(); s.workspaceContext.publish(previous); }
}

export async function recovery(state, multiple = false) {
  let release; const barrier = new Promise(resolve => { release = resolve; });
  const f = await fixture(async (_request, index) => {
    if (index) { await barrier; return { name: 'recovered.txt' }; }
    return { failures: [{ owner: owners[0].gatewayId, state, retryable: true, http_status: 403 },
      ...(multiple ? [{ owner: owners[1].gatewayId, state: 'cancelled', retryable: false }] : [])] };
  });
  try {
    const picker = f.show(); await until(() => !picker.busy && diagnosticItems(picker).length > 0, () => ({ labels: labels(picker), busy: picker.busy, calls: f.calls.length }));
    const before = { labels: labels(picker), matches: matched(picker), icons: diagnosticItems(picker).map(item => item.iconClass),
      buttons: diagnosticItems(picker).map(item => item.buttons.length), diagnostics: await copyDiagnostics(picker) };
    const failed = picker.items.find(item => item.buttons?.[0]?.tooltip === '重新查询此文件分支');
    picker.press(failed); picker.press(failed); await until(() => f.calls.length === 2);
    const busy = picker.busy; release();
    await until(() => !picker.busy && matched(picker).some(resource => resource.endsWith('/recovered.txt')));
    return { before, busy, labels: labels(picker), diagnostics: await copyDiagnostics(picker), matches: matched(picker), requests: f.calls.map(call => call.request),
      selectedRoot: root(owners[0]), successfulRoot: root(owners[1]) };
  } finally { release(); f.close(); }
}

/** 原 Controller、QuickPick、List、Tree 和 CountBadge；配置／布局等宿主服务为装具。 */
export async function resultCount() {
  const f = await fixture(async () => ({ limited: true, failures: [{ owner: owners[0].gatewayId, state: 'failed', retryable: true }] }));
  const lifetime = new q.DisposableStore(), container = document.body.appendChild(document.createElement('div'));
  Object.assign(container.style, { width: '800px', height: '600px' });
  try {
    const source = f.show();
    await until(() => !source.busy && source.items.some(item => item.buttons?.length));
    const configuration = { getValue: () => undefined, onDidChangeConfiguration: q.Event.None };
    const services = new q.ServiceCollection(
      [q.IConfigurationService, configuration],
      [ILayoutService, { mainContainer: container, activeContainer: container, getContainer: () => container, onDidLayoutContainer: q.Event.None }],
      [IListService, lifetime.add(new ListService())], [IStorageService, lifetime.add(new InMemoryStorageService())],
      [IContextMenuService, { onDidShowContextMenu: q.Event.None }],
      [IKeybindingService, { mightProducePrintableCharacter: () => false, softDispatch: () => ({ kind: 0 }), lookupKeybinding: () => undefined }],
      [IThemeService, { onDidColorThemeChange: q.Event.None, getColorTheme: () => ({ type: 'dark', getColor: () => undefined }) }],
      [IAccessibilityService, { isScreenReaderOptimized: () => false, onDidChangeScreenReaderOptimized: q.Event.None,
        isMotionReduced: () => false, onDidChangeReducedMotion: q.Event.None, alwaysUnderlineAccessKeys: async () => false }],
    );
    const instantiation = lifetime.add(new q.InstantiationService(services, true));
    services.set(IContextKeyService, lifetime.add(instantiation.createInstance(ContextKeyService)));
    services.set(IContextViewService, lifetime.add(instantiation.createInstance(ContextViewService)));
    const controller = lifetime.add(instantiation.createInstance(QuickInputController, { container, idPrefix: 'countTest',
      ignoreFocusOut: () => true, returnFocus() {}, backKeybindingLabel() {}, setContextKey() {}, linkOpenerDelegate() {},
      hoverDelegate: { delay: 0, showHover() {} }, styles: Object.fromEntries(['button', 'countBadge', 'inputBox', 'toggle',
        'keybindingLabel', 'list', 'progressBar', 'widget', 'pickerGroup'].map(key => [key, {}])) }));
    controller.layout({ height: 600, width: 800 }, 0);
    const picker = lifetime.add(controller.createQuickPick({ useSeparators: true }));
    let pressed = 0; lifetime.add(picker.onDidTriggerItemButton(() => pressed++));
    picker.items = source.items; picker.show();
    const count = () => container.querySelector('.quick-input-visible-count').textContent;
    const snapshot = () => ({ count: count(), labels: [...container.querySelectorAll('.quick-input-list .label-name')].map(node => node.textContent),
      ariaLive: container.querySelector('.quick-input-visible-count').getAttribute('aria-live') });
    await tick();
    const before = snapshot(), match = picker.items.find(item => item.match), failed = picker.items.find(item => item.buttons?.length);
    const notice = picker.items.find(item => item.label?.includes('本次查询覆盖受限'));
    if (!notice) throw new Error('原 Provider 未返回限制提示');
    picker.activeItems = [match];
    const active = picker.activeItems[0] === match;
    const failedRow = [...container.querySelectorAll('.quick-input-list .monaco-list-row')]
      .find(row => row.textContent.includes('文件搜索失败'));
    const button = failedRow?.querySelector('.action-label');
    if (!button) throw new Error('原列表未呈现诊断重试按钮');
    button.click();
    picker.value = 'ok.txt'; await tick(); const filtered = snapshot();
    picker.value = ''; picker.items = [{ label: '默认候选' }, { label: '显式普通候选', excludeFromResultCount: false }, failed];
    await tick(); const ordinary = snapshot();
    picker.items = [failed, notice]; await tick(); const onlyNotices = snapshot();
    return { before, filtered, ordinary, onlyNotices, active, pressed,
      failuresExcluded: source.items.filter(item => !item.match && item.type !== 'separator').every(item => item.excludeFromResultCount === true) };
  } finally { lifetime.dispose(); container.remove(); f.close(); }
}

export async function cancelRetry(mode) {
  let release; const barrier = new Promise(resolve => { release = resolve; });
  const f = await fixture(async (_request, index) => {
    if (index === 1) { await barrier; return { name: 'late.txt' }; }
    return index ? { name: 'current.txt' } : { failures: [{ owner: owners[0].gatewayId, state: 'cancelled', retryable: true }] };
  });
  try {
    const picker = f.show(); await until(() => !picker.busy && picker.items.some(item => item.buttons?.[0]?.tooltip === '重新查询此文件分支'));
    picker.press(picker.items.find(item => item.buttons?.[0]?.tooltip === '重新查询此文件分支'));
    await until(() => f.calls.length === 2);
    if (mode === 'hide') picker.hide(); else picker.value = '%current';
    await until(() => f.calls[1].signal.aborted && (mode === 'hide' || !picker.busy && matched(picker).length === 2));
    const before = labels(picker); release(); await tick(); await tick();
    return { aborted: f.calls[1].signal.aborted, unchanged: JSON.stringify(labels(picker)) === JSON.stringify(before),
      matches: matched(picker), labels: labels(picker), requests: f.calls.map(call => call.request) };
  } finally { release(); f.close(); }
}

export async function requestFailure(kind) {
  const f = await fixture(async () => kind === 'http' ? { http: 409, detail: { code: 'source_changed', error_owner: 'workspace_files', reason: 'revision_changed' } }
    : kind === 'incomplete' ? { incomplete: true } : { failures: owners.map(owner => ({ owner: owner.gatewayId, state: 'failed', retryable: false })) });
  try {
    const picker = f.show(); await until(() => !picker.busy && labels(picker).some(label => label?.includes('失败')));
    return { labels: labels(picker), diagnostics: await copyDiagnostics(picker), details: picker.items.map(item => item.detail), requests: f.calls.length };
  } finally { f.close(); }
}

export async function synchronousLimit() {
  const branch = { owner: { gateway_id: owners[0].gatewayId, workspace_id: owners[0].workspaceId }, category: 'file', operation: 'content', resource_uri: root(owners[0]) };
  const event = { type: 'branch', request_id: 'sync-limit-request', sequence: 1, branch: { branch, state: 'failed', retryable: false, code: 'execution_failed', message: '原异步拒绝' } };
  const results = Array.from({ length: 31 }, (_, index) => ({ resource: uri(owners[0], `${index}.txt`), results: [s.textMatch(hit(root(owners[0]), `${index}.txt`))] }));
  const f = await fixture(async () => { throw new Error('同步展示合同不应查询HTTP'); }, { getAIName: async () => undefined,
    textSearchSplitSyncAsync: () => ({ syncResults: { results, messages: [] }, asyncResults: Promise.reject(new s.WorkspaceSearchBranchError(event, [], false)) }) });
  try {
    const picker = f.show(); await until(() => !picker.busy && diagnosticItems(picker).length > 0);
    return { labels: labels(picker), diagnostics: await copyDiagnostics(picker), matches: matched(picker), files: picker.items.filter(item => item.type === 'separator' && item.label).length,
      requests: f.calls.length };
  } finally { f.close(); }
}
