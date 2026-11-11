import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { sourceTransform, pinnedUpstreamCommit } from './sourceTransform.mjs';
import { prepareQuery, pieceToQuery, scoreFuzzy2 } from '../upstream/src/vs/base/common/fuzzyScorer.js';
import { SymbolKinds, SymbolTag, SymbolKind } from '../upstream/src/vs/editor/common/languages.js';
import { ThemeIcon } from '../upstream/src/vs/base/common/themables.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { toDisposable, DisposableStore, Disposable, MutableDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { timeout } from '../upstream/src/vs/base/common/async.js';
import { Range } from '../upstream/src/vs/editor/common/core/range.js';
import { groupBy } from '../upstream/src/vs/base/common/arrays.js';
import { compare } from '../upstream/src/vs/base/common/strings.js';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';

const root = resolve('src/clients/workbench');
const path = join(root, 'upstream/src/vs/workbench/contrib/search/common/search.ts');
const transformed = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
const parsed = ts.createSourceFile(path, transformed, ts.ScriptTarget.Latest, true);
const declarations = parsed.statements.filter(node => (ts.isModuleDeclaration(node) || ts.isClassDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name && ['WorkspaceSymbolProviderRegistry', 'WorkspaceSymbolItem', 'getWorkspaceSymbols', 'coalesceWorkspaceSymbols'].includes(node.name.getText(parsed)));
// 执行原聚合实现；只去掉模块导出，不复制聚合、去重、Provider 与取消逻辑。
const aggregate = new Bun.Transpiler({ loader: 'ts' }).transformSync(declarations.map(node => node.getText(parsed).replace(/^export /, '')).join('\n'));
interface Symbol { name: string; kind: number; location: { uri: URI; range?: Range }; }
interface Provider { provideWorkspaceSymbols(query: string, token: CancellationToken): Symbol[] | Promise<Symbol[]>; }
interface Pick { label: string; symbol?: Symbol; description?: string; trigger?: () => Promise<number>; }
const original = new Function('Range', 'groupBy', 'compare', 'CancellationToken', 'onUnexpectedExternalError', aggregate + '\nreturn { WorkspaceSymbolProviderRegistry, getWorkspaceSymbols, coalesceWorkspaceSymbols };')(
  Range, groupBy, compare, CancellationToken, (error: unknown) => { throw error; },
) as { WorkspaceSymbolProviderRegistry: { all(): Provider[]; register(provider: Provider): { dispose(): void } };
  getWorkspaceSymbols(query: string, token: CancellationToken, providers: Provider[], onError: (provider: Provider, error: unknown) => void): Promise<{ symbol: Symbol; provider: Provider }[]>;
  coalesceWorkspaceSymbols: (items: object[]) => object[] };
const providerPath = join(root, 'upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.ts');
const providerText = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(providerPath, await readFile(providerPath, 'utf8'));
const providerSource = ts.createSourceFile(providerPath, providerText, ts.ScriptTarget.Latest, true);
const providerClass = providerSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'SymbolsQuickAccessProvider')! as ts.ClassDeclaration;
const pickerPath = join(root, 'upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.ts');
const pickerSource = ts.createSourceFile(pickerPath, await readFile(pickerPath, 'utf8'), ts.ScriptTarget.Latest, true);
const pickerScript = ts.transpile(pickerSource.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(pickerSource).replace(/^export /, '')).join('\n'));
const Picker = new Function('Disposable', 'DisposableStore', 'MutableDisposable', 'CancellationTokenSource', 'timeout', 'isFunction', 'isKeyModified', pickerScript + '\nreturn PickerQuickAccessProvider;')(
  Disposable, DisposableStore, MutableDisposable, CancellationTokenSource, timeout, (value: unknown) => typeof value === 'function', () => false,
);
const members = providerClass.members.filter(node => node.name && ['TREAT_AS_GLOBAL_SYMBOL_TYPES', 'configuration', '_getPicks', 'getSymbolPicks', 'doGetSymbolPicks', 'toSymbolPicks', 'compareSymbols', 'openSymbol'].includes(node.name.getText(providerSource)));
const baseScript = ts.transpile(`class SymbolsQuickAccessProvider extends PickerQuickAccessProvider { constructor() { super('#'); } ${members.map(node => node.getText(providerSource)).join('\n')} }`);
// 原聚合、模糊评分、容器匹配、去重与接受逻辑；只注入语言 Provider 和窗口服务。
const Base = new Function('getWorkspaceSymbols', 'prepareQuery', 'pieceToQuery', 'scoreFuzzy2', 'SymbolKinds', 'SymbolTag', 'SymbolKind', 'ThemeIcon', 'Codicon', 'localize', 'Range', 'TriggerAction', 'Schemas', 'SIDE_GROUP', 'ACTIVE_GROUP', 'PickerQuickAccessProvider', baseScript + '\nreturn SymbolsQuickAccessProvider;')(
  original.getWorkspaceSymbols, prepareQuery, pieceToQuery, scoreFuzzy2, SymbolKinds, SymbolTag, SymbolKind, ThemeIcon, Codicon,
  (_key: string, label: string) => label, Range, { CLOSE_PICKER: 1 }, { http: 'http', https: 'https' }, -2, -1, Picker,
);
const scopePath = join(root, 'src/search/symbolsQuickAccess.ts');
const scopeSource = ts.createSourceFile(scopePath, await readFile(scopePath, 'utf8'), ts.ScriptTarget.Latest, true);
const owner = scopeSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WorkspaceSymbolsQuickAccess')! as ts.ClassDeclaration;
const javascript = ts.transpile(owner.getText(scopeSource).replace('export class', 'class'));
const Scope = new Function('SymbolsQuickAccessProvider', 'WorkspaceSymbolProviderRegistry', 'gatewayWorkspaceKey', 'workspaceFileSource', 'toDisposable', 'prepareQuery', 'pieceToQuery', 'coalesceWorkspaceSymbols', 'getWorkspaceSymbols', 'ThemeIcon', 'Codicon', 'TriggerAction', 'CancellationTokenSource', javascript + '\nreturn WorkspaceSymbolsQuickAccess;')(
  Base, original.WorkspaceSymbolProviderRegistry, gatewayWorkspaceKey,
  (uri: URI) => { const [, workspaceId] = uri.path.split('/'); return { gatewayId: uri.authority, workspaceId }; },
  toDisposable, prepareQuery, pieceToQuery, original.coalesceWorkspaceSymbols, original.getWorkspaceSymbols, ThemeIcon, Codicon, { NO_ACTION: 0 }, CancellationTokenSource,
) as new () => { workspaceContextService: object; delayer: object; configurationService: object; labelService: object;
  picker: { items: Pick[]; activeItems: Pick[]; busy: boolean }; settled: Promise<void>; getScopedSymbolPicks(query: string, options: object, token: CancellationToken, progress: (picks: Pick[], busy: boolean) => void, pickOptions?: object): Promise<void>; provide(picker: object, token: CancellationToken, options: object): { dispose(): void }; _getPicks(query: string, disposable: DisposableStore, token: CancellationToken, options: object): Promise<Pick[]> };
const a = { gatewayId: 'g', workspaceId: 'a' }, b = { gatewayId: 'g', workspaceId: 'b' };
function fixture() {
  const scope = new Scope();
  scope.workspaceContextService = { getWorkspace: () => ({ folders: ['a', 'b'].map(id => ({ uri: URI.parse(`boxteam-file://g/${id}/root`) })) }) };
  scope.picker = { items: [], activeItems: [], busy: false };
  scope.delayer = { trigger: (callback: () => Promise<void>) => { scope.settled = Promise.resolve().then(callback); return scope.settled; } };
  scope.configurationService = { getValue: () => ({ workbench: { editor: {} } }) };
  scope.labelService = { getUriLabel: (uri: URI) => uri.path };
  return scope;
}

test('完整窗口范围才调用原Registry；部分、空或其它工作区范围零Provider调用', async () => {
  let calls = 0;
  const provider = { provideWorkspaceSymbols: async () => { calls++; return [{ name: 'builtin', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } }]; } };
  const registration = original.WorkspaceSymbolProviderRegistry.register(provider);
  try {
    const scope = fixture();
    for (const owners of [[a], [], [a, { ...b, gatewayId: 'other' }]]) {
      expect((await scope._getPicks('', new DisposableStore(), CancellationToken.None, { searchOwners: owners }))[0]!.label).toContain('完整窗口工作区');
    }
    expect(calls).toBe(0);
    scope._getPicks('', new DisposableStore(), CancellationToken.None, { searchOwners: [b, a, a] });
    await scope.settled;
    expect(scope.picker.items.map(pick => pick.label)).toEqual(['builtin']);
    expect(calls).toBe(1);
  } finally { registration.dispose(); }
});

test('原聚合保留成功候选及Provider关联，失败可见，取消不发布迟到错误', async () => {
  const symbol = { name: 'good', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  const good = { provideWorkspaceSymbols: async () => [symbol] };
  const failure = { provideWorkspaceSymbols: async (): Promise<Symbol[]> => { throw new Error('language server failed'); } };
  const registrations = [good, failure].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  try {
    const items = await original.getWorkspaceSymbols('', CancellationToken.None, [good], () => {});
    expect(items[0]!.symbol).toBe(symbol); expect(items[0]!.provider).toBe(good);
    const scope = fixture();
    scope._getPicks('', new DisposableStore(), CancellationToken.None, { searchOwners: [a, b] });
    await scope.settled;
    const picks = scope.picker.items;
    expect(picks.map(pick => pick.label)).toEqual(['good', '工作区符号不可用：Error: language server failed']);
    expect(picks[1]!.description).toContain(gatewayWorkspaceKey(a));
    const cancelled = new CancellationTokenSource(); cancelled.cancel();
    expect(await fixture()._getPicks('', new DisposableStore(), cancelled.token, { searchOwners: [a, b] })).toEqual([]);
    cancelled.dispose();
  } finally { registrations.forEach(registration => registration.dispose()); }
  expect((await fixture()._getPicks('', new DisposableStore(), CancellationToken.None, { searchOwners: [a, b] }))[0]!.label).toContain('没有可用的内置');
});

test('快分支先发布原评分候选，单分支重试保留兄弟与活动项，最终沿原去重', async () => {
  const symbol = { name: 'shared', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  let release!: (symbols: Symbol[]) => void;
  let goodCalls = 0, slowCalls = 0, failedCalls = 0;
  const good = { provideWorkspaceSymbols: () => { goodCalls++; return [symbol]; } };
  const slow = { provideWorkspaceSymbols: () => { slowCalls++; return new Promise<Symbol[]>(resolve => { release = resolve; }); } };
  const failure = { provideWorkspaceSymbols: async () => {
    if (++failedCalls === 1) throw new Error('original failure');
    return [{ ...symbol, name: 'sharedRetry', location: { uri: URI.parse('boxteam-file://g/b/root/retry.ts') } }];
  } };
  const registrations = [good, slow, failure].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  const scope = fixture(), lifetime = new DisposableStore();
  try {
    scope._getPicks('sh', lifetime, CancellationToken.None, { searchOwners: [a, b] });
    const finished = scope.settled;
    await Promise.resolve(); await Promise.resolve();
    expect(scope.picker.items.map(pick => pick.label)).toEqual(['shared', '工作区符号不可用：Error: original failure']);
    expect(scope.picker.busy).toBe(true);
    scope.picker.activeItems = [scope.picker.items[0]!];
    await scope.picker.items[1]!.trigger!();
    expect([goodCalls, slowCalls, failedCalls]).toEqual([1, 1, 2]);
    expect(scope.picker.items.map(pick => pick.label)).toEqual(['shared', 'sharedRetry']);
    expect(scope.picker.activeItems[0]!.symbol).toBe(symbol);
    expect(scope.picker.busy).toBe(true);
    release([{ ...symbol }]);
    await finished;
    const final = scope.picker.items;
    expect(final.map(pick => pick.label)).toEqual(['shared', 'sharedRetry']);
    expect(scope.picker.busy).toBe(false);
    expect(final[0]!.symbol).toBe(symbol);
  } finally { lifetime.dispose(); registrations.forEach(registration => registration.dispose()); }
});

test('新输入取消旧Provider及重试，迟到结果和错误均不改新Picker', async () => {
  let resolve!: (symbols: Symbol[]) => void, reject!: (error: Error) => void;
  const seenTokens: CancellationToken[] = [];
  const slow = { provideWorkspaceSymbols: (_query: string, token: CancellationToken) => {
    seenTokens.push(token); return new Promise<Symbol[]>(accept => { resolve = accept; });
  } };
  const failure = { provideWorkspaceSymbols: (_query: string, token: CancellationToken) => {
    seenTokens.push(token); return new Promise<Symbol[]>((_accept, fail) => { reject = fail; });
  } };
  const registrations = [slow, failure].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  const scope = fixture(), cancellation = new CancellationTokenSource(), lifetime = new DisposableStore();
  try {
    scope._getPicks('old', lifetime, cancellation.token, { searchOwners: [a, b] });
    const old = scope.settled;
    await Promise.resolve();
    cancellation.cancel(); lifetime.dispose();
    expect(seenTokens).toEqual([cancellation.token, cancellation.token]);
    expect(seenTokens.every(token => token.isCancellationRequested)).toBe(true);
    scope.picker.items = [{ label: 'new query' }];
    resolve([{ name: 'old', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } }]);
    reject(new Error('late failure'));
    await old;
    expect(scope.picker.items).toEqual([{ label: 'new query' }]);
    expect(scope.picker.busy).toBe(false);
  } finally { cancellation.dispose(); registrations.forEach(registration => registration.dispose()); }
});

test('重试保留原symbol/container查询语义，已完成失败重试可继续但不读成功分支', async () => {
  let calls = 0;
  const queries: string[] = [];
  const provider = { provideWorkspaceSymbols: (query: string) => {
    queries.push(query);
    if (++calls < 3) throw new Error(`attempt ${calls}`);
    return [
      { name: 'target', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/desired.ts') } },
      { name: 'target', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/other.ts') } },
    ];
  } };
  const registration = original.WorkspaceSymbolProviderRegistry.register(provider);
  const scope = fixture(), lifetime = new DisposableStore();
  try {
    scope._getPicks('tar desired', lifetime, CancellationToken.None, { searchOwners: [a, b] });
    await scope.settled;
    await scope.picker.items[0]!.trigger!();
    expect(scope.picker.items[0]!.label).toContain('attempt 2');
    await scope.picker.items[0]!.trigger!();
    expect(queries).toEqual(['tar', 'tar', 'tar']);
    expect(scope.picker.items.map(pick => pick.symbol?.location.uri.path)).toEqual(['/a/root/desired.ts']);
  } finally { lifetime.dispose(); registration.dispose(); }
});

test('真实原Picker输入、按钮和关闭共用逐次token，首轮结算不清掉在途重试busy', async () => {
  const change = new Emitter<void>(), accept = new Emitter<object>(), itemButton = new Emitter<object>(), separatorButton = new Emitter<object>();
  let releaseSlow!: (symbols: Symbol[]) => void, releaseRetry!: (symbols: Symbol[]) => void;
  const tokens: CancellationToken[] = [], queries: string[] = [];
  let calls = 0;
  const symbol = { name: 'shared', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  const slow = { provideWorkspaceSymbols: (query: string, token: CancellationToken) => {
    queries.push(query); tokens.push(token);
    return query === 'new' ? [] : new Promise<Symbol[]>(resolve => { releaseSlow = resolve; });
  } };
  const retry = { provideWorkspaceSymbols: (query: string, token: CancellationToken) => {
    queries.push(query); tokens.push(token);
    if (++calls === 1) throw new Error('first failure');
    return query === 'new' ? [] : new Promise<Symbol[]>(resolve => { releaseRetry = resolve; });
  } };
  const registrations = [slow, retry].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  const scope = fixture(), outer = new CancellationTokenSource();
  const picker = { value: '#sh', items: [] as Pick[], activeItems: [] as Pick[], selectedItems: [] as Pick[], busy: false, keyMods: {},
    onDidChangeValue: change.event, onDidAccept: accept.event, onDidTriggerItemButton: itemButton.event, onDidTriggerSeparatorButton: separatorButton.event };
  const lifetime = scope.provide(picker, outer.token, { searchOwners: [a, b] });
  try {
    await Promise.resolve(); await Promise.resolve();
    const failure = picker.items[0]!;
    itemButton.fire({ item: failure, button: (failure as Pick & { buttons: object[] }).buttons[0] });
    releaseSlow([symbol]);
    await scope.settled;
    expect(picker.items.map(pick => pick.label)).toEqual(['shared']);
    expect(picker.busy).toBe(true);
    picker.value = '#new'; change.fire();
    expect(tokens[0]!.isCancellationRequested).toBe(true);
    expect(tokens[1]!.isCancellationRequested).toBe(true);
    expect(tokens[2]!.isCancellationRequested).toBe(true);
    releaseRetry([{ ...symbol, name: 'lateRetry' }]);
    await scope.settled;
    expect(picker.items.map(pick => pick.label)).toEqual(['没有匹配的工作区符号']);
    expect(picker.busy).toBe(false);
    expect(queries).toEqual(['sh', 'sh', 'sh', 'new', 'new']);
    const currentTokens = tokens.slice(-2);
    lifetime.dispose();
    expect(currentTokens.every(token => token.isCancellationRequested)).toBe(true);
  } finally {
    lifetime.dispose(); outer.dispose(); registrations.forEach(registration => registration.dispose());
    [change, accept, itemButton, separatorButton].forEach(event => event.dispose());
  }
});

const accessPath = join(root, 'src/search/quickAccess.ts');
const accessSource = ts.createSourceFile(accessPath, await readFile(accessPath, 'utf8'), ts.ScriptTarget.Latest, true);
const accessClass = accessSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WorkspaceQuickAccess')! as ts.ClassDeclaration;
const accessScript = ts.transpile(`class WorkspaceQuickAccess extends PickerQuickAccessProvider {
  constructor() { super(''); }
  ${accessClass.members.filter(node => !ts.isConstructorDeclaration(node)).map(node => node.getText(accessSource)).join('\n')}
}`);
// 真实默认Picker及符号owner共用一次查询；联邦搜索是受控服务边界，不复制其网络协议。
function mixedFixture(options: object = {}, includeSymbols = true) {
  const change = new Emitter<void>(), accept = new Emitter<object>(), itemButton = new Emitter<object>(), hide = new Emitter<object>();
  let branchProgress!: (branches: object[]) => void, branchBusy!: (value: boolean) => void;
  let branchToken!: CancellationToken, branchDisposed = false;
  const opened: object[] = [];
  const Mixed = new Function('PickerQuickAccessProvider', 'DisposableStore', 'MutableDisposable', 'CancellationTokenSource', 'toDisposable',
    'workspaceContext', 'PickerEditorState', 'extractRangeFromFilter', 'rankResourcePicks', 'WorkspaceSymbolsQuickAccess',
    'queryFederatedSearchBranches', 'QuickInputHideReason', 'getCodeEditor', 'TriggerAction', 'Codicon', 'ThemeIcon', 'GroupDirection',
    accessScript + '\nreturn WorkspaceQuickAccess;')(
    Picker, DisposableStore, MutableDisposable, CancellationTokenSource, toDisposable,
    { getSnapshot: () => ({ gatewayId: 'g' }), subscribe: () => () => {} }, class {},
    (value: string) => /:\d+$/.test(value) ? { filter: value.replace(/:\d+$/, ''), range: {} } : undefined,
    (items: object[]) => items, Scope,
    (_owners: object[], _categories: string[], _filter: string, disposables: DisposableStore, token: CancellationToken,
      _cache: object, _map: object, progress: typeof branchProgress, busy: typeof branchBusy) => {
      branchProgress = progress; branchBusy = busy; branchToken = token;
      disposables.add(toDisposable(() => { branchDisposed = true; }));
      busy(true);
      return [];
    }, { Gesture: 1 }, () => undefined, { NO_ACTION: 0 }, Codicon, ThemeIcon, { RIGHT: 1, DOWN: 2 },
  );
  const scope = new Mixed();
  const group = { id: 7 };
  scope.groups = { activeGroup: group, getGroup: (id: number) => id === 7 ? group : undefined,
    findGroup: () => ({ id: 8 }) };
  scope.history = { getHistory: () => [] };
  scope.configuration = { getValue: (key: string) => key === 'search.quickOpen.includeSymbols' ? includeSymbols : undefined };
  scope.editors = { openEditor: async (input: object, editorGroup: number) => { opened.push({ input, group: editorGroup }); } };
  let symbols: ReturnType<typeof fixture> | undefined;
  scope.instantiation = { createInstance: (ctor: object) => {
    if (ctor === Scope) {
      symbols = fixture();
      (symbols as unknown as { editorService: object }).editorService = scope.editors;
      return symbols;
    }
    return { dispose() {} };
  } };
  scope.quickChat = { enabled: false };
  scope.watchIdentity = () => {}; // 身份通知为具名宿主边界；本装具验证符号查询与接受生命周期。
  scope.quickInput = {};
  scope.notifications = { error: (error: unknown) => { throw error; } };
  const picker = { value: '', items: [] as (Pick & { id: string; type?: string; accept?: (mods: object, event: object) => Promise<void> })[],
    activeItems: [] as Pick[], selectedItems: [] as Pick[], busy: false, buttons: [], keyMods: {},
    onDidChangeValue: change.event, onDidAccept: accept.event, onDidTriggerItemButton: itemButton.event,
    onDidTriggerSeparatorButton: (() => toDisposable(() => {})), onDidTriggerButton: (() => toDisposable(() => {})),
    onDidHide: hide.event, onDidChangeActive: (() => toDisposable(() => {})) };
  const outer = new CancellationTokenSource();
  const lifetime = scope.provide(picker, outer.token, { searchOwners: [a, b], ...options });
  return { picker, opened, input(value: string) { picker.value = value; change.fire(); },
    branches(label: string, busy: boolean) {
      branchBusy(busy);
      branchProgress([{ category: '文件', picks: [{ id: 'file', label, accept() {} }], status: { id: 'file-state', label: '文件完成' } }]);
    }, get branchToken() { return branchToken; }, get branchDisposed() { return branchDisposed; },
    get symbols() { return symbols; },
    accept(pick: Pick) { picker.selectedItems = [pick]; },
    dispose() { lifetime.dispose(); outer.dispose(); [change, accept, itemButton, hide].forEach(event => event.dispose()); } };
}

test('默认列表沿同一符号链增量合并，保留原文件与活动身份，重试只读失败Provider', async () => {
  let release!: (symbols: Symbol[]) => void, retries = 0, successes = 0;
  const tokens: CancellationToken[] = [];
  const fast = { provideWorkspaceSymbols: (_query: string, token: CancellationToken) => {
    successes++; tokens.push(token);
    return [{ name: 'needle', kind: SymbolKind.Class, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts'), range: new Range(3, 2, 3, 5) } },
      { name: 'needleLocal', kind: SymbolKind.Method, containerName: 'Container', location: { uri: URI.parse('boxteam-file://g/a/root/local.ts') } }];
  } };
  const slow = { provideWorkspaceSymbols: (_query: string, token: CancellationToken) => {
    tokens.push(token);
    if (++retries === 1) throw new Error('slow failed');
    return new Promise<Symbol[]>(resolve => { release = resolve; });
  } };
  const registrations = [fast, slow].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  const f = mixedFixture();
  try {
    f.input('needle');
    await f.symbols!.settled;
    expect(f.picker.items.filter(pick => pick.symbol).map(pick => pick.label)).toEqual(['needle']);
    f.branches('needle file', false);
    expect(f.picker.busy).toBe(false);
    const symbol = f.picker.items.find(pick => pick.symbol)!;
    f.picker.activeItems = [symbol];
    const failure = f.picker.items.find(pick => pick.label.includes('slow failed'))!;
    const retry = failure.trigger!();
    expect(f.picker.busy).toBe(true);
    expect(f.picker.items.some(pick => pick.id === 'file')).toBe(true);
    expect(f.picker.activeItems[0]?.symbol).toBe(symbol.symbol);
    release([]); await retry;
    expect(f.picker.busy).toBe(false);
    expect(successes).toBe(1); expect(retries).toBe(2);
    const current = f.picker.items.find(pick => pick.symbol)!;
    await current.accept!({ alt: true, ctrlCmd: false }, { inBackground: true });
    expect(f.opened).toEqual([{ input: { resource: symbol.symbol!.location.uri,
      options: { preserveFocus: true, pinned: true, selection: new Range(3, 2, 3, 2) } }, group: 8 }]);
    f.input('needle:4');
    expect(f.picker.items.some(pick => pick.symbol)).toBe(false);
    expect(f.picker.items.some(pick => pick.id === 'file')).toBe(true);
    expect(successes).toBe(1); expect(retries).toBe(2);
    f.dispose();
    expect(tokens.every(token => token.isCancellationRequested)).toBe(true);
    expect(f.branchDisposed).toBe(true);
  } finally { f.dispose(); registrations.forEach(registration => registration.dispose()); }
});

test('默认列表两组busy独立结算，新输入与关闭取消原查询并丢弃迟到符号', async () => {
  const queries: { token: CancellationToken; resolve: (symbols: Symbol[]) => void }[] = [];
  const registration = original.WorkspaceSymbolProviderRegistry.register({ provideWorkspaceSymbols: (_query, token) =>
    new Promise<Symbol[]>(resolve => { queries.push({ token, resolve }); }) });
  const f = mixedFixture();
  try {
    f.input('old'); await Promise.resolve(); await Promise.resolve();
    const oldBranch = f.branchToken;
    f.branches('old file', false);
    expect(f.picker.busy).toBe(true);
    f.input('new'); await Promise.resolve(); await Promise.resolve();
    expect(oldBranch.isCancellationRequested).toBe(true);
    expect(queries[0]!.token.isCancellationRequested).toBe(true);
    queries[0]!.resolve([{ name: 'late', kind: SymbolKind.Class, location: { uri: URI.parse('boxteam-file://g/a/root/late.ts') } }]);
    queries[1]!.resolve([]); await f.symbols!.settled;
    expect(f.picker.busy).toBe(true);
    f.branches('new file', false);
    expect(f.picker.busy).toBe(false);
    expect(f.picker.items.some(pick => pick.label === 'late')).toBe(false);
    f.dispose();
    expect(queries[1]!.token.isCancellationRequested).toBe(true);
    expect(f.branchToken.isCancellationRequested).toBe(true);
  } finally { f.dispose(); registration.dispose(); }
});

test('文件限定、接收回调、行列定位、关闭设置均零符号查询，部分范围只显示原因', async () => {
  let calls = 0;
  const registration = original.WorkspaceSymbolProviderRegistry.register({ provideWorkspaceSymbols: () => { calls++; return []; } });
  try {
    for (const options of [{ filesOnly: true }, { handleAccept() {} }, { range: true }, { off: true }, { searchOwners: [a] }]) {
      const f = mixedFixture(options, !('off' in options));
      try {
        f.input('range' in options ? 'needle:4' : 'needle');
        await f.symbols?.settled;
        expect(calls).toBe(0);
        expect(f.picker.items.some(pick => pick.label.includes('完整窗口工作区'))).toBe('searchOwners' in options);
      } finally { f.dispose(); }
    }
  } finally { registration.dispose(); }
});

test('默认符号确认关闭后原resolve仍完成，新查询和关闭取消不误杀已接受打开', async () => {
  let release!: (symbol: Symbol) => void;
  const symbol = { name: 'needle', kind: SymbolKind.Class, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  let resolveToken!: CancellationToken;
  const provider = { provideWorkspaceSymbols: () => [symbol], resolveWorkspaceSymbol: (_symbol: Symbol, token: CancellationToken) => {
    resolveToken = token;
    return new Promise<Symbol>(resolve => { release = resolve; });
  } };
  const registration = original.WorkspaceSymbolProviderRegistry.register(provider);
  const f = mixedFixture();
  try {
    f.input('needle'); await f.symbols!.settled;
    const pick = f.picker.items.find(pick => pick.symbol)!;
    f.accept(pick); f.dispose();
    const opening = pick.accept!({ alt: false, ctrlCmd: false }, { inBackground: false });
    expect(resolveToken.isCancellationRequested).toBe(false);
    release(symbol); await opening;
    expect(f.opened).toHaveLength(1);
    expect(f.branchToken.isCancellationRequested).toBe(true);
  } finally { f.dispose(); registration.dispose(); }
});

test('明确范围符号确认沿原调用token完成resolve，关闭仍取消未结算查询', async () => {
  let queryToken!: CancellationToken, actionToken!: CancellationToken;
  const symbol = { name: 'needle', kind: SymbolKind.Class, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  const registration = original.WorkspaceSymbolProviderRegistry.register({
    provideWorkspaceSymbols: (_query, token) => { queryToken = token; return [symbol]; },
    resolveWorkspaceSymbol: async (_symbol: Symbol, token: CancellationToken) => { actionToken = token; return symbol; },
  });
  const scope = fixture(), outer = new CancellationTokenSource(), opened: object[] = [];
  (scope as unknown as { editorService: object }).editorService = { openEditor: async (input: object) => { opened.push(input); } };
  const picker = { value: '#needle', items: [] as Pick[], activeItems: [] as Pick[], selectedItems: [] as Pick[], keyMods: {},
    onDidChangeValue: () => toDisposable(() => {}), onDidAccept: () => toDisposable(() => {}),
    onDidTriggerItemButton: () => toDisposable(() => {}), onDidTriggerSeparatorButton: () => toDisposable(() => {}) };
  const lifetime = scope.provide(picker, outer.token, { searchOwners: [a, b] });
  try {
    await scope.settled;
    const pick = picker.items[0] as Pick & { accept(mods: object, event: object): Promise<void> };
    picker.selectedItems = [pick]; lifetime.dispose();
    await pick.accept({ alt: false, ctrlCmd: false }, { inBackground: false });
    expect(queryToken.isCancellationRequested).toBe(true);
    expect(actionToken.isCancellationRequested).toBe(false);
    expect(opened).toHaveLength(1);
  } finally { lifetime.dispose(); outer.dispose(true); registration.dispose(); }
});
