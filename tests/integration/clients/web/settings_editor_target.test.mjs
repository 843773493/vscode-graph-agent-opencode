import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { CancellationToken, CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { Emitter, Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { isEqual } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { Schemas } from '../../../../src/clients/workbench/upstream/src/vs/base/common/network.js';
import { parse } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../../../src/clients/workbench/upstream/src/vs/base/common/jsonEdit.js';
import { Delayer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/async.js';
import { isCancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { StopWatch } from '../../../../src/clients/workbench/upstream/src/vs/base/common/stopwatch.js';
import { ConfigurationTarget } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configuration.js';
import { ConfigurationScope } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import { SettingMatchType } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { MODIFIED_SETTING_TAG, POLICY_SETTING_TAG, AGENTS_WINDOW_SETTING_TAG, ENABLE_LANGUAGE_FILTER,
  EMBEDDINGS_SEARCH_PROVIDER_NAME, LLM_RANKED_SEARCH_PROVIDER_NAME, STRING_MATCH_SEARCH_PROVIDER_NAME,
  TF_IDF_SEARCH_PROVIDER_NAME, FILTER_MODEL_SEARCH_PROVIDER_NAME } from '../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/preferences/common/preferences.js';
import { IAiSettingsSearchService, AiSettingsSearchResultKind } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/aiSettingsSearch/common/aiSettingsSearch.js';
import { AiSettingsSearchService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/aiSettingsSearch/common/aiSettingsSearchService.js';
import { EditorModel } from '../../../../src/clients/workbench/upstream/src/vs/workbench/common/editor/editorModel.js';
import { pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_editor_target.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = join(root, 'upstream/src/vs');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const sources = new Map(), fingerprints = [], registered = [], executed = [], passed = [];
const artifactsSource = await readFile(import.meta.path, 'utf8');
await writeFile(join(run.artifactsDir, 'test-source.mjs'), artifactsSource);
fingerprints.push({ path: 'tests/integration/clients/web/settings_editor_target.test.mjs', original: createHash('sha256').update(artifactsSource).digest('hex') });

async function source(relative, local = false) {
  const key = `${local ? 'local:' : ''}${relative}`;
  if (!sources.has(key)) {
    const path = join(local ? root : upstream, relative), original = await readFile(path, 'utf8');
    const converted = local ? original : await transform(path, original);
    const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
    if (parsed.parseDiagnostics.length) throw new Error(`转换源码不能解析：${relative}`);
    fingerprints.push({ path: key, original: createHash('sha256').update(original).digest('hex'), converted: createHash('sha256').update(converted).digest('hex') });
    await writeFile(join(run.artifactsDir, key.replaceAll('/', '__').replace(':', '__')), converted);
    sources.set(key, parsed);
  }
  return sources.get(key);
}

function evaluate(parsed, names, environment, classMembers) {
  const declarations = names.map(name => {
    let node = parsed.statements.find(statement => statement.name?.text === name || ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name));
    if (!node) throw new Error(`实际源码缺少声明：${parsed.fileName}#${name}`);
    if (classMembers) {
      const members = node.members.filter(member => classMembers.includes(member.name?.getText(parsed)));
      for (const member of classMembers) if (!members.some(candidate => candidate.name?.getText(parsed) === member)) {
        throw new Error(`实际类缺少成员：${name}.${member}`);
      }
      node = ts.factory.updateClassDeclaration(node, node.modifiers, node.name, node.typeParameters, node.heritageClauses, members);
    }
    return ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  });
  const javascript = ts.transpile(declarations.join('\n'), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None,
    useDefineForClassFields: false, experimentalDecorators: true });
  return new Function(...Object.keys(environment), `${javascript}\nreturn { ${names.join(', ')} };`)(...Object.values(environment));
}

const serviceIdentifier = () => (..._args) => {};
const IConfigurationService = serviceIdentifier(), IPreferencesService = serviceIdentifier();
const windowSchemaReads = [];
const configurationRegistry = { onDidSchemaChange: Event.None,
  getConfigurationProperties() { windowSchemaReads.push('window schema read'); return { 'window.only': { type: 'boolean' } }; } };
const Registry = { as: () => configurationRegistry };
const ITargetSettingsContext = evaluate(await source('src/settings/scope.ts', true), ['ITargetSettingsContext'], {
  createDecorator: serviceIdentifier, registerSingleton() {}, InstantiationType: { Delayed: 0 },
}).ITargetSettingsContext;
const { configSourceEditorUri } = evaluate(await source('src/settings/sourceEditor.ts', true),
  ['configSourceScheme', 'configSourceEditorUri'], { URI });
const modelParsed = await source('workbench/services/preferences/common/preferencesModels.ts');
const { nullRange } = evaluate(modelParsed, ['nullRange'], {});
const modelEnvironment = { EditorModel, SettingMatchType: {}, Emitter, Event, Disposable, ConfigurationTarget: { USER_LOCAL: 3, USER_REMOTE: 4, DEFAULT: 7 },
  IConfigurationService, Extensions: { Configuration: 'configuration' }, Registry };
const { Settings2EditorModel, DefaultSettings } = evaluate(modelParsed,
  ['AbstractSettingsModel', 'Settings2EditorModel', 'DefaultSettings'], modelEnvironment);
const { settingsEditorIdentity, settingsHistoryLocation, parseSettingsLocation, SourceSettings } =
  evaluate(await source('src/settings/editor.ts', true),
    ['object', 'at', 'settingsEditorIdentity', 'settingsHistoryLocation', 'parseSettingsLocation', 'SourceDefaults', 'SourceSettings'], {
      Disposable, Emitter, URI, ConfigurationScope, ConfigurationTarget: { USER_LOCAL: 3, USER_REMOTE: 4, DEFAULT: 7 }, IConfigurationService,
      DefaultSettings, Settings2EditorModel, nullRange, parse, setProperty, applyEdits, configSourceEditorUri,
      configSourceFiles: files => files.getProvider(),
    });
const parseSearchQuery = evaluate(await source('workbench/contrib/preferences/browser/settingsTreeModels.ts'),
  ['tagRegex', 'extensionRegex', 'featureRegex', 'idRegex', 'languageRegex', 'parseQuery'], {
    MODIFIED_SETTING_TAG, POLICY_SETTING_TAG, AGENTS_WINDOW_SETTING_TAG, ENABLE_LANGUAGE_FILTER,
    isFalsyOrWhitespace: value => typeof value !== 'string' || value.trim().length === 0,
  }).parseQuery;

const commonParsed = await source('workbench/common/editor.ts');
const common = evaluate(commonParsed, ['EditorInputCapabilities', 'Verbosity', 'AbstractEditorInput', 'isEditorInput'], { Disposable });
const { EditorInput } = evaluate(await source('workbench/common/editor/editorInput.ts'), ['EditorInput'], {
  ...common, Emitter, DisposableStore, URI, isEqual,
});
let prepareSettingsPaneImplementation = async (_parent, _location, _document, _lifetime, token) => {
  if (token.isCancellationRequested) throw new CancellationError();
  throw new Error('该测试尚未提供来源 Pane 准备结果');
};
const prepareSettingsPane = (...args) => prepareSettingsPaneImplementation(...args);
const { SettingsEditor2Input } = evaluate(await source('workbench/services/preferences/common/preferencesEditorInput.ts'), ['SettingsEditor2Input'], {
  EditorInput, Emitter, DisposableStore, URI, Schemas, isEqual, IPreferencesService,
  parseSettingsLocation, settingsEditorIdentity, ITargetSettingsContext, prepareSettingsPane: (...args) => prepareSettingsPane(...args),
  toDisposable, CancellationError, SettingsEditorIcon: { id: 'settings-editor-test' }, nls: { localize: (_key, text) => text },
});
const { SettingsEditor2InputSerializer } = evaluate(await source('workbench/contrib/preferences/browser/preferences.contribution.ts'),
  ['SettingsEditor2InputSerializer'], { SettingsEditor2Input, parseSettingsLocation, settingsHistoryLocation });

class EditorPaneBoundary {
  clearInput() { this.baseClearCount = (this.baseClearCount ?? 0) + 1; }
  dispose() { this.baseDisposeCount = (this.baseDisposeCount ?? 0) + 1; }
}
const settingsEditorMembers = ['notifySettingsDirty', 'discardSettingsUpdates', 'flushSettingsUpdates', 'cancelSettingsSearch',
  'disableAiSearchToggle', 'clearInput', 'dispose', 'onSearchInputChanged', 'triggerSearch', 'createFilterModel',
  'triggerFilterPreferences', 'onDidFinishSearch', 'doLocalSearch', 'doRemoteSearch', 'doAiSearch', 'getLLMRankedResults',
  'searchWithProvider', '_searchPreferencesModel', 'parseSettingFromJSON', 'onDidSettingsTargetChange'];

class SearchResultModelBoundary {
  rawResults = [];
  root = { children: [] };
  showAiResults = false;

  setResult(index, result) {
    this.rawResults[index] = result;
    this.root.children = (result?.filterMatches ?? []).map(match => ({ setting: match.setting }));
  }

  getRawResults() { return this.rawResults; }
}

const { SearchResultIdx } = evaluate(await source('workbench/contrib/preferences/browser/settingsTreeModels.ts'), ['SearchResultIdx'], {});
const { AiSearchProvider } = evaluate(await source('workbench/contrib/preferences/browser/preferencesSearch.ts'),
  ['cleanFilter', 'SettingsRecordProvider', 'EmbeddingsSearchProvider', 'AiSearchProvider'], {
    IAiSettingsSearchService, SettingMatchType, EMBEDDINGS_SEARCH_PROVIDER_NAME, LLM_RANKED_SEARCH_PROVIDER_NAME,
  });
const { SettingsEditor2 } = evaluate(await source('workbench/contrib/preferences/browser/settingsEditor2.ts'), ['SettingsEditor2'], {
  EditorPane: EditorPaneBoundary,
  MODIFIED_SETTING_TAG, SHOW_AI_RESULTS_DISABLED_LABEL: 'No AI results available',
  CancellationTokenSource, SearchResultIdx, SearchResultModel: SearchResultModelBoundary, SettingMatchType, StopWatch,
  isCancellationError, parseQuery: parseSearchQuery,
  EMBEDDINGS_SEARCH_PROVIDER_NAME, LLM_RANKED_SEARCH_PROVIDER_NAME, STRING_MATCH_SEARCH_PROVIDER_NAME,
  TF_IDF_SEARCH_PROVIDER_NAME, FILTER_MODEL_SEARCH_PROVIDER_NAME,
}, settingsEditorMembers);

function scenario(name, callback) {
  registered.push(name);
  test(name, async () => { executed.push(name); await callback(); passed.push(name); });
}

function sourceSelectionFixture() {
  const auth = new Emitter(), context = new Emitter(), requests = [], picks = [];
  let snapshot = { gatewayId: 'entry-A', workspaces: [
    { gateway_id: 'gateway-B', workspace_id: 'workspace-B', name: 'B', status: 'ready' },
    { gateway_id: 'gateway-C', workspace_id: 'workspace-C', name: 'C', status: 'ready' },
  ] };
  const target = { gateway_id: 'gateway-B', gateway_connection_id: 'route-B', name: '远端 B' };
  const quick = { pick: async (items, options, token) => {
    picks.push({ items, options, token });
    return options.title === '选择设置来源的所属目标' ? items.find(item => item.workspaceId === 'workspace-B') : items[0];
  } };
  const document = { jsonc: '{}', source_key: 'workspace_user' };
  class SourceOwnerBoundary {
    constructor(target) { this.target = { ...target }; }
    async list(signal) {
      requests.push({ operation: 'list', owner: this.target, signal });
      return { sources: [{ source_key: 'workspace_user', layer: 'user', presence: 'present', precedence: 1 }] };
    }
    async read(source, _key, signal) { requests.push({ operation: 'read', owner: this.target, source, signal }); return document; }
  }
  const capture = evaluate(sources.get('local:src/settings/editor.ts'), ['captureSettingsLocation'], {
    CancellationTokenSource, CancellationError, IQuickInputService: {}, ConfigurationSourceOwner: SourceOwnerBoundary,
    managementGateway: () => target,
    workspaceContext: { getSnapshot: () => snapshot, subscribe: listener => {
      const subscription = context.event(listener); return () => subscription.dispose();
    } },
    onGatewayTokenInvalidated: (_port, listener) => { const subscription = auth.event(listener); return () => subscription.dispose(); },
  }).captureSettingsLocation;
  return { target, quick, requests, picks, document, auth, context,
    capture: token => capture({ invokeFunction: callback => callback({ get: () => quick }) }, token ?? CancellationToken.None),
    changeEntry() { snapshot = { ...snapshot, gatewayId: 'entry-C' }; context.fire(); },
    dispose() { auth.dispose(); context.dispose(); },
  };
}

scenario('两种设置入口共用来源选择，目标冻结且不重复读取工作区目录', async () => {
  // 原 owner 的来源 HTTP 边界可控；选择与取消运行实际生产函数。
  const f = sourceSelectionFixture();
  const pick = f.quick.pick;
  f.quick.pick = async (...args) => {
    const selected = await pick(...args);
    f.target.gateway_id = 'gateway-C'; f.target.gateway_connection_id = 'route-C';
    return selected;
  };
  try {
    const captured = await f.capture();
    expect(captured.location).toEqual({ version: 2, gateway_id: 'gateway-B', gateway_connection_id: 'route-B',
      workspace_id: 'workspace-B', source_key: 'workspace_user', name: 'B' });
    expect(captured.document).toBe(f.document);
    expect(f.requests.map(item => item.operation)).toEqual(['list', 'read']);
    expect(f.requests.map(item => item.owner)).toEqual(Array(2).fill({ gateway_id: 'gateway-B', gateway_connection_id: 'route-B', workspace_id: 'workspace-B' }));
    expect(f.picks[0].items.map(item => item.label)).toEqual(['Gateway 配置', 'B']);
    expect(f.requests.every(item => item.signal.aborted)).toBe(true);
  } finally { f.dispose(); }
});

for (const invalidation of ['credential', 'entry', 'cancel']) scenario(`设置来源选择在 ${invalidation} 失效时取消 Picker 并拒绝迟到回执`, async () => {
  const f = sourceSelectionFixture(), cancellation = new CancellationTokenSource(), entered = Promise.withResolvers();
  f.quick.pick = async (_items, _options, token) => {
    entered.resolve(token);
    return new Promise(resolve => token.onCancellationRequested(() => resolve({ workspaceId: 'workspace-B', label: 'B' })));
  };
  try {
    const pending = f.capture(cancellation.token), pickerToken = await entered.promise;
    const rejected = pending.then(() => { throw new Error('失效的设置选择不应成功'); }, error => error);
    if (invalidation === 'credential') f.auth.fire();
    else if (invalidation === 'entry') f.changeEntry();
    else cancellation.cancel();
    expect(await rejected).toBeInstanceOf(Error);
    expect(pickerToken.isCancellationRequested).toBe(true);
    expect(f.requests).toEqual([]);
  } finally { cancellation.dispose(); f.dispose(); }
});

async function searchFixture() {
  const pane = new SettingsEditor2(), requests = [], countEvents = [], traces = [], refreshes = [];
  const ai = new AiSettingsSearchService();
  const registration = ai.registerSettingsSearchProvider({ searchSettings(query, _options, token) { requests.push({ query, token }); } });
  pane.searchDelayer = new Delayer(0);
  pane.searchInputDelayer = new Delayer(0);
  pane.viewState = { settingsTarget: ConfigurationTarget.USER_REMOTE };
  pane.defaultSettingsEditorModel = { settingsGroups: [{ id: 'defaults', sections: [{ settings: [] }] }, { id: 'sample', sections: [{ settings: [] }] }] };
  pane.preferencesSearchService = {
    getLocalSearchProvider: () => ({ async searchModel() { return { filterMatches: [], exactMatch: false }; } }),
    getRemoteSearchProvider: () => undefined,
    getAiSearchProvider(query) { const provider = new AiSearchProvider(ai); provider.setFilter(query); return provider; },
  };
  pane.canShowAdvancedSettings = () => true;
  pane.settingsTargetsWidget = { settingsTarget: ConfigurationTarget.USER_REMOTE, updateLanguageFilterIndicators() {} };
  pane.onConfigUpdate = (...args) => { refreshes.push(args); return Promise.resolve(); };
  pane.editorProgressService = { show: () => ({ done() {} }) };
  pane.tocTreeModel = {};
  pane.tocTree = { setFocus() {}, setSelection() {}, expandAll() {} };
  pane.settingsTree = { scrollTop: 0 };
  pane.workspaceTrustManagementService = { isWorkspaceTrusted: () => true };
  pane.instantiationService = { createInstance: () => new SearchResultModelBoundary() };
  pane.searchInputActionBar = { hasAction: () => true };
  pane.showAiResultsAction = { enabled: false, checked: false, label: '' };
  pane.aiResultsAvailable = { value: false, set(value) { this.value = value; } };
  pane.inSettingsEditorContextKey = { set() {} };
  pane.renderResultCountMessages = aiAvailable => countEvents.push({ aiAvailable, query: pane.viewState.query });
  pane.refreshTOCTree = () => {};
  pane.refreshTree = () => {};
  pane.renderTree = () => {};
  pane.toggleTocBySearchBehaviorType = () => {};
  pane.layoutSplitView = () => {};
  pane.searchWidget = { value: '', getValue() { return this.value; }, addToHistory() {}, setValue(value) { this.value = value; } };
  pane.updateSearchPlaceholder = () => {};
  pane.saveSearchHistory = () => {};
  pane.logService = { trace: (...args) => traces.push(args) };
  pane.telemetryService = { publicLog2() {} };
  pane.searchResultModel = new SearchResultModelBoundary();
  const alpha = { key: 'sample.alpha', range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 } };
  const beta = { key: 'sample.beta', range: { startLineNumber: 2, startColumn: 1, endLineNumber: 2, endColumn: 2 } };
  pane.defaultSettingsEditorModel.settingsGroups[1].sections[0].settings = [alpha, beta];
  pane.renderedSearchQuery = '';
  pane.currentSettingsModel = {};
  const release = async () => {
    const { query } = requests[0];
    ai.handleSearchResult({ query, kind: AiSettingsSearchResultKind.EMBEDDED, settings: ['sample.alpha'] });
    ai.handleSearchResult({ query, kind: AiSettingsSearchResultKind.LLM_RANKED, settings: ['sample.alpha'] });
    await new Promise(resolve => setTimeout(resolve, 0));
  };
  return { pane, requests, countEvents, traces, refreshes,
    query(value) { pane.searchWidget.setValue(value); return pane.onSearchInputChanged(true); },
    release,
    close() { pane.dispose(); pane.searchDelayer.dispose(); pane.searchInputDelayer.dispose(); registration.dispose(); ai.dispose(); },
  };
}

function location(overrides = {}) {
  return { version: 2, gateway_id: 'gateway-A', workspace_id: 'workspace-A', source_key: 'workspace_user', name: 'Workspace User',
    gateway_connection_id: 'connection-A', ...overrides };
}

function makeTextFileModel(initial, notifyReverted) {
  const content = new Emitter(), savedEvent = new Emitter(), dirtyEvent = new Emitter();
  let value = initial, saved = initial, dirty = false;
  const textEditorModel = {
    getValue: () => value,
    getFullModelRange: () => ({ startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: value.length + 1 }),
    pushEditOperations(_selections, operations) { setValue(operations[0].text); },
  };
  const setValue = next => {
    value = next;
    const nextDirty = value !== saved;
    if (dirty !== nextDirty) { dirty = nextDirty; dirtyEvent.fire(); }
    content.fire();
  };
  return {
    textEditorModel,
    onDidChangeContent: content.event, onDidSave: savedEvent.event, onDidChangeDirty: dirtyEvent.event,
    isDirty: () => dirty,
    saveCalls: 0, revertCalls: 0,
    async save() {
      if (dirty) { this.saveCalls++; saved = value; dirty = false; dirtyEvent.fire(); savedEvent.fire(); }
      return true;
    },
    async revert() { this.revertCalls++; setValue(saved); notifyReverted(this); },
  };
}

function makeSource(locationValue) {
  const jsonc = `// 保留来源注释\n{\n  "config_version": 2,\n  "untouched": { "value": 9 }\n}\n`;
  const document = {
    gateway_id: locationValue.gateway_id, workspace_id: locationValue.workspace_id, source_key: locationValue.source_key,
    jsonc, writable: true,
    json_schema: { type: 'object', properties: {
      logging: { type: 'object', properties: { runtime: { type: 'object', properties: { level: { type: 'string', default: 'info' } } } } },
      'a.b': { type: 'object', properties: { c: { type: 'string' } } },
      a: { type: 'object', properties: { 'b.c': { type: 'string' } } },
    } },
    source_effective: { logging: { runtime: { level: 'effective' } } },
    layers: [
      { source_key: 'gateway_inline', precedence: 1, payload: { logging: { runtime: { level: 'inherited' } } } },
      { source_key: locationValue.source_key, precedence: 10, payload: {} },
    ],
  };
  const reverted = new Emitter();
  const model = makeTextFileModel(jsonc, value => reverted.fire(value)), provider = { currentDocument(resource) {
    if (resource.toString() !== configSourceEditorUri(locationValue, locationValue.source_key).toString()) throw new Error('来源资源身份不匹配');
    return document;
  } };
  const files = { getProvider: () => provider };
  const textFileModels = { onDidRevert: reverted.event };
  const schemaRequests = [], errors = [];
  const owner = { async configurationSchemas(signal) { schemaRequests.push(signal); return { plugins: [] }; } };
  provider.configurationSchemas = () => owner.configurationSchemas();
  const notifications = { error: error => errors.push(error) };
  const source = new SourceSettings(locationValue, model, files, { plugins: [] }, textFileModels);
  return { source, model, document, provider, files, textFileModels, owner, notifications, schemaRequests, errors };
}

function childServices(source, configurationCalls) {
  const configurationService = {
    onDidChangeConfiguration: Event.None,
    updateValue(...args) { configurationCalls.push(args); return Promise.resolve(); },
  };
  const child = {
    invokeFunction(callback) { return callback({ get(id) {
      if (id === ITargetSettingsContext) return { target: source };
      if (id === IConfigurationService) return configurationService;
      throw new Error('未登记的设置服务');
    } }); },
    createInstance(Model, ...args) { return new Model(...args, configurationService); },
  };
  return { child, configurationService };
}

scenario('source Input 身份只由 Gateway、工作区和 source key 决定，历史序列化剥离连接路由', () => {
  const preferences = { createSettings2EditorModel() { throw new Error('来源 Input 不应创建窗口设置模型'); } };
  const original = location(), input = new SettingsEditor2Input(original, undefined, preferences);
  const sameOwner = new SettingsEditor2Input(location({ name: '重命名', gateway_connection_id: 'connection-B' }), undefined, preferences);
  try {
    expect(input.location).toEqual(original);
    expect(input.matches(sameOwner)).toBe(true);
    for (const change of [{ gateway_id: 'gateway-B' }, { workspace_id: 'workspace-B' }, { source_key: 'other_source' }]) {
      expect(input.matches(new SettingsEditor2Input(location(change), undefined, preferences))).toBe(false);
    }
    const serializer = new SettingsEditor2InputSerializer(), serialized = serializer.serialize(input);
    const stable = { version: 2, gateway_id: 'gateway-A', workspace_id: 'workspace-A', source_key: 'workspace_user', name: 'Workspace User' };
    expect(JSON.parse(serialized)).toEqual(stable);
    expect(serialized).not.toContain('connection-A');
    const restored = serializer.deserialize({ createInstance: (_type, value, document) => new SettingsEditor2Input(value, document, preferences) }, serialized);
    expect(restored.location).toEqual(stable);
    expect(restored.matches(input)).toBe(true);
    restored.dispose();
  } finally { input.dispose(); sameOwner.dispose(); }
});

scenario('来源 schema 只构造当前 Settings2EditorModel，JSONC 表单编辑保持路径、注释和继承投影', async () => {
  const target = location(), f = makeSource(target), configurationCalls = [];
  const targetSettings = new SourceSettings(target, f.model, f.files, { plugins: [] }, f.textFileModels);
  const child = childServices(targetSettings, configurationCalls).child;
  try {
    const model = targetSettings.createModel(child), groups = model.settingsGroups;
    const keys = groups.flatMap(group => group.sections.flatMap(section => section.settings.map(setting => setting.key)));
    expect(keys).toEqual(['logging.runtime.level', 'a%2Eb.c', 'a.b%2Ec']);
    expect(keys).not.toContain('window.only');
    expect(windowSchemaReads).toEqual([]);

    const inherited = targetSettings.inspect('logging.runtime.level');
    expect(inherited).toMatchObject({ defaultValue: 'inherited', value: 'effective' });
    targetSettings.edit('logging.runtime.level', 'debug');
    targetSettings.edit('a%2Eb.c', 'dot-property');
    targetSettings.edit('a.b%2Ec', 'nested-property');
    const draft = f.model.textEditorModel.getValue(), parsed = parse(draft);
    expect(draft).toContain('// 保留来源注释');
    expect(parsed).toMatchObject({ config_version: 2, untouched: { value: 9 }, logging: { runtime: { level: 'debug' } },
      'a.b': { c: 'dot-property' }, a: { 'b.c': 'nested-property' } });
    expect(targetSettings.inspect('logging.runtime.level')).toMatchObject({ userRemoteValue: 'debug', value: 'debug', defaultValue: 'inherited' });
    expect(configurationCalls).toEqual([]);
    model.dispose();
  } finally { targetSettings.dispose(); }
});

scenario('source Input 通过原文本模型报告 dirty、显式 save 与 revert', async () => {
  const target = location(), f = makeSource(target), targetSettings = f.source, configurationCalls = [];
  const { child } = childServices(targetSettings, configurationCalls);
  const input = new SettingsEditor2Input(target, f.document, { createSettings2EditorModel() { throw new Error('不应使用窗口模型'); } });
  const lifetime = new DisposableStore(), dirtyEvents = [];
  input.onDidChangeDirty(() => dirtyEvents.push(input.isDirty()));
  prepareSettingsPaneImplementation = async () => child;
  try {
    expect(await input.prepareEditorPane({}, lifetime, CancellationToken.None)).toBe(child);
    const model = await input.resolve(child);
    expect(model.settingsGroups.flatMap(group => group.sections.flatMap(section => section.settings.map(setting => setting.key))))
      .toEqual(['logging.runtime.level', 'a%2Eb.c', 'a.b%2Ec']);
    const textModel = f.model.textEditorModel;
    targetSettings.edit('logging.runtime.level', 'debug');
    expect(textModel.getValue()).toContain('"level": "debug"');
    expect(f.model.isDirty()).toBe(true);
    expect(input.isDirty()).toBe(true);
    expect(f.model.saveCalls).toBe(0);
    expect(configurationCalls).toEqual([]);

    expect(await input.save()).toBe(input);
    expect(f.model.saveCalls).toBe(1);
    expect(f.model.isDirty()).toBe(false);
    expect(input.isDirty()).toBe(false);

    targetSettings.edit('logging.runtime.level', 'temporary');
    expect(input.isDirty()).toBe(true);
    await input.revert();
    expect(f.model.revertCalls).toBe(1);
    expect(parse(textModel.getValue()).logging.runtime.level).toBe('debug');
    expect(input.isDirty()).toBe(false);
    expect(dirtyEvents).toContain(true);
    expect(dirtyEvents.at(-1)).toBe(false);
  } finally { lifetime.dispose(); input.dispose(); targetSettings.dispose(); prepareSettingsPaneImplementation = async () => { throw new Error('该测试尚未提供来源 Pane 准备结果'); }; }
});

scenario('来源正式保存和还原后替换确切绑定版本的 schema，解除 B 不影响 A', async () => {
  const a = makeSource(location()), b = makeSource(location({ workspace_id: 'workspace-B' }));
  const binding = version => ({ package_id: 'schema.probe', plugin_id: 'preference', package_version: version, enabled: false });
  const schemas = { plugins: [
    { package_id: 'schema.probe', plugin_id: 'preference', package_version: '1.0.0', name: '版本一',
      configuration_schema: { type: 'object', properties: { mode: { type: 'string', default: 'first' }, onlyOne: { type: 'boolean' } } } },
    { package_id: 'schema.probe', plugin_id: 'preference', package_version: '2.0.0', name: '版本二',
      configuration_schema: { type: 'object', properties: { mode: { type: 'boolean', default: true }, onlyTwo: { type: 'number' } } } },
  ] };
  const receipt = (fixture, version) => {
    fixture.document.source_effective = { plugins: { runtime: { entries: version ? [binding(version)] : [] } } };
    fixture.model.textEditorModel.pushEditOperations(null, [{ text: JSON.stringify(fixture.document.source_effective) }]);
  };
  receipt(a, '1.0.0'); receipt(b, '2.0.0');
  const sourceA = new SourceSettings(location(), a.model, a.files, schemas, a.textFileModels);
  const sourceB = new SourceSettings(location({ workspace_id: 'workspace-B' }), b.model, b.files, schemas, b.textFileModels);
  const fields = source => source.groups.filter(group => group.extensionInfo).flatMap(group => group.sections.flatMap(section => section.settings));
  const mode = source => fields(source).find(setting => setting.key.endsWith('.mode'));
  try {
    expect(mode(sourceA)).toMatchObject({ type: 'string', value: 'first' });
    expect(mode(sourceB)).toMatchObject({ type: 'boolean', value: true });
    receipt(b, '1.0.0'); await b.model.save();
    expect(mode(sourceB)).toMatchObject({ type: 'string', value: 'first' });
    expect(fields(sourceB).map(setting => setting.key)).not.toContain('plugins.configuration.values.schema%2Eprobe.preference.onlyTwo');
    receipt(b, undefined); await b.model.save();
    expect(fields(sourceB)).toEqual([]);
    expect(() => sourceB.edit('plugins.configuration.values.schema%2Eprobe.preference.mode', 'stale')).toThrow('不属于当前来源');
    expect(mode(sourceA)).toMatchObject({ type: 'string', value: 'first' });
    receipt(b, '2.0.0'); await b.model.revert();
    expect(mode(sourceB)).toMatchObject({ type: 'boolean', value: true });
  } finally { sourceA.dispose(); sourceB.dispose(); a.source.dispose(); b.source.dispose(); }
});

scenario('安装事件替换声明后按当前绑定展示，保存不补读，卸载与失效移除字段', async () => {
  const f = makeSource(location());
  const plugin = version => ({ package_id: 'schema.probe', plugin_id: 'preference', package_version: version, name: version,
    configuration_schema: { type: 'object', properties: { mode: { type: version === '1.0.0' ? 'string' : 'boolean' } } } });
  const setBinding = version => {
    f.document.source_effective = { plugins: { runtime: { entries: version ? [{ ...plugin(version), enabled: false }] : [] } } };
    f.model.textEditorModel.pushEditOperations(null, [{ text: JSON.stringify(f.document.source_effective) }]);
  };
  const fields = () => f.source.groups.filter(group => group.extensionInfo).flatMap(group => group.sections.flatMap(section => section.settings));
  try {
    setBinding('1.0.0'); await f.model.save(); expect(fields()).toEqual([]);
    f.source.replaceSchemas({ plugins: [plugin('1.0.0'), plugin('2.0.0')] });
    expect(fields()[0].type).toBe('string');
    setBinding('2.0.0'); await f.model.save(); expect(fields()[0].type).toBe('boolean');
    expect(f.schemaRequests).toEqual([]);
    setBinding(undefined); await f.model.save();
    f.source.replaceSchemas({ plugins: [plugin('1.0.0')] }); expect(fields()).toEqual([]);
    setBinding('1.0.0'); await f.model.revert(); expect(fields()[0].type).toBe('string');
    f.source.replaceSchemas(undefined); expect(fields()).toEqual([]);
  } finally { f.source.dispose(); }
});

scenario('配置声明沿冻结目标读取并拒绝错误 Gateway 或 workspace 回执', async () => {
  const calls = [];
  let receipt = { gateway_id: 'gateway-B', workspace_id: 'workspace-B', plugins: [] };
  const Owner = evaluate(await source('src/settings/sourceOwner.ts', true), ['ConfigurationSourceOwner'], {
    gateway: { request: async (path, options) => { calls.push({ path, ...options }); return receipt; } },
    watchPluginConfigurationSchemas: async (target, signal) => { calls.push({ target, signal }); },
  }).ConfigurationSourceOwner;
  const target = { gateway_id: 'gateway-B', workspace_id: 'workspace-B', gateway_connection_id: 'route-B' };
  const owner = new Owner({ ...target, version: 2, name: '展示名称' }), abort = new AbortController();
  expect(await owner.configurationSchemas(abort.signal)).toBe(receipt);
  expect(calls[0].path).toBe('/api/gateway/service-plugins/configuration-schemas');
  expect(JSON.parse(calls[0].body)).toEqual(target);
  expect(calls[0].headers['X-BoxTeam-Target-Gateway-Id']).toBe('gateway-B');
  expect(calls[0].signal).toBe(abort.signal);
  await owner.watchConfigurationSchemas(abort.signal, () => {});
  expect(calls[1]).toEqual({ target, signal: abort.signal });
  for (const wrong of [{ gateway_id: 'gateway-A' }, { workspace_id: 'workspace-A' }]) {
    receipt = { ...target, ...wrong, plugins: [] };
    await expect(owner.configurationSchemas()).rejects.toThrow('不属于原目标');
  }
});


scenario('同来源分屏共享声明订阅，最后视图关闭回收，异路由与错误不串用', async () => {
  const Files = evaluate(await source('src/settings/sourceEditor.ts', true), ['ConfigurationSourceFiles'],
    { Disposable, getErrorMessage: error => error.message }, ['observeConfigurationSchemas', 'release']).ConfigurationSourceFiles;
  const files = Object.create(Files.prototype), requests = [], received = [[], [], []], errors = [];
  const owner = { target: { gateway_connection_id: 'route-A' }, watchConfigurationSchemas(signal, next) {
    const reply = Promise.withResolvers(); requests.push({ signal, next, ...reply }); return reply.promise;
  } };
  const edit = { owner }; files.edit = () => edit;
  const observer = index => ({ next: value => received[index].push(value), error: error => errors.push(error.message) });
  const a = files.observeConfigurationSchemas(undefined, owner, observer(0));
  const b = files.observeConfigurationSchemas(undefined, owner, observer(1));
  expect(requests).toHaveLength(1);
  const first = { plugins: [] }; requests[0].next(first);
  expect(received[0]).toEqual([first]); expect(received[1]).toEqual([first]);
  const late = files.observeConfigurationSchemas(undefined, owner, observer(2));
  expect(received[2]).toEqual([first]); expect(requests).toHaveLength(1);
  const other = files.observeConfigurationSchemas(undefined,
    { ...owner, target: { gateway_connection_id: 'route-B' } }, observer(2));
  expect(requests).toHaveLength(2);
  a.dispose(); b.dispose(); expect(requests[0].signal.aborted).toBe(false);
  late.dispose(); expect(requests[0].signal.aborted).toBe(true);
  requests[0].next({ plugins: ['retired'] }); expect(received[0]).toEqual([first]);
  requests[1].reject(new Error('配置读取授权已撤销'));
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(errors).toEqual(['配置读取授权已撤销']);
  other.dispose(); expect(requests[1].signal.aborted).toBe(true); expect(edit.schemaStreams.size).toBe(0);
  const resource = { toString: () => 'same-source' };
  files.edits = new Map([[resource.toString(), edit]]);
  files.observeConfigurationSchemas(resource, owner, observer(0));
  files.release(resource);
  expect(requests[2].signal.aborted).toBe(true); expect(files.edits.size).toBe(0);
});

scenario('Pane 取消来源准备后不绑定迟到模型，释放已取得的文本引用', async () => {
  const input = new SettingsEditor2Input(location(), undefined, { createSettings2EditorModel() { return undefined; } });
  const cancellation = new CancellationTokenSource(), lifetime = new DisposableStore(), entered = Promise.withResolvers(), release = Promise.withResolvers();
  const reference = { disposed: false, dispose() { this.disposed = true; } };
  const unusedSource = { createModel() { throw new Error('取消后不得构造设置模型'); }, onDidChange: Event.None, model: { isDirty: () => false } };
  const child = { invokeFunction: callback => callback({ get: () => ({ target: unusedSource }) }) };
  prepareSettingsPaneImplementation = async (_parent, _target, _document, disposables) => {
    disposables.add(reference); entered.resolve(); await release.promise; return child;
  };
  try {
    const pending = input.prepareEditorPane({}, lifetime, cancellation.token);
    await entered.promise;
    cancellation.cancel(); release.resolve();
    await expect(pending).rejects.toBeInstanceOf(CancellationError);
    expect(input.isDirty()).toBe(false);
    await expect(input.resolve(child)).rejects.toThrow('设置模型尚未绑定当前编辑器组');
    lifetime.dispose();
    expect(reference.disposed).toBe(true);
  } finally { release.resolve(); lifetime.dispose(); cancellation.dispose(); input.dispose(); prepareSettingsPaneImplementation = async () => { throw new Error('该测试尚未提供来源 Pane 准备结果'); }; }
});

scenario('窗口设置 Pane 清空或关闭时取消旧搜索并清理延迟任务', async () => {
  const makePane = () => {
    const pane = new SettingsEditor2(), search = new CancellationTokenSource(), events = [];
    Object.assign(pane, {
      searchInProgress: search, searchDelayer: { cancel: () => events.push('search') }, searchInputDelayer: { cancel: () => events.push('input') },
      showAiResultsAction: { checked: true, enabled: true, label: '' }, aiResultsAvailable: { value: true, set(value) { this.value = value; } },
      inSettingsEditorContextKey: { set(value) { events.push(value); } },
    });
    return { pane, search, events };
  };
  const cleared = makePane();
  cleared.pane.clearInput();
  expect(cleared.search.token.isCancellationRequested).toBe(true);
  expect(cleared.events).toEqual(['search', 'input', false]);
  expect(cleared.pane.showAiResultsAction).toMatchObject({ checked: false, enabled: false, label: 'No AI results available' });
  expect(cleared.pane.aiResultsAvailable.value).toBe(false);
  expect(cleared.pane.baseClearCount).toBe(1);

  const closed = makePane();
  closed.pane.dispose();
  expect(closed.search.token.isCancellationRequested).toBe(true);
  expect(closed.events).toEqual(['search', 'input']);
  expect(closed.pane.baseDisposeCount).toBe(1);
});

scenario('窗口原设置保存保持写入次序，新一轮 dirty 更新等前一轮结清', async () => {
  const pane = new SettingsEditor2(), entered = Promise.withResolvers(), release = Promise.withResolvers(), writes = [];
  const input = new SettingsEditor2Input(undefined, undefined, { createSettings2EditorModel: () => ({ dispose() {} }) });
  const dirtyEvents = [];
  input.onDidChangeDirty(() => dirtyEvents.push(input.isDirty()));
  Object.assign(pane, {
    pendingSettingUpdates: new Map(), pendingSettingWrite: undefined,
    settingFastUpdateDelayer: { cancel() {} }, settingSlowUpdateDelayer: { cancel() {} },
    settingsWriterInput: input,
    async updateChangedSetting(key, value) {
      writes.push([key, value]);
      if (value === 16) { entered.resolve(); await release.promise; }
    },
  });
  input.registerSettingsWriter(1, { pending: () => pane.pendingSettingUpdates.size > 0 || !!pane.pendingSettingWrite,
    save: () => pane.flushSettingsUpdates(), revert: () => pane.discardSettingsUpdates() });
  const first = { key: 'editor.fontSize', value: 16, inFlight: false };
  pane.pendingSettingUpdates.set('font', first);
  try {
    const firstSave = input.save();
    await entered.promise;
    pane.pendingSettingUpdates.set('font', { key: 'editor.fontSize', value: 19, inFlight: false });
    pane.pendingSettingUpdates.set('tabs', { key: 'editor.tabSize', value: 4, inFlight: false });
    const secondSave = input.save();
    expect(writes).toEqual([['editor.fontSize', 16]]);
    expect(input.isDirty()).toBe(true);
    release.resolve();
    await Promise.all([firstSave, secondSave]);
    expect(writes).toEqual([['editor.fontSize', 16], ['editor.fontSize', 19], ['editor.tabSize', 4]]);
    expect(pane.pendingSettingUpdates.size).toBe(0);
    expect(input.isDirty()).toBe(false);
    expect(dirtyEvents).toContain(true);
    expect(dirtyEvents.at(-1)).toBe(false);
  } finally { release.resolve(); input.dispose(); }
});

scenario('窗口原设置写入失败保留队列，下一次显式 save 可重试', async () => {
  const pane = new SettingsEditor2(), failure = new Error('配置写入失败'), writes = [];
  const input = new SettingsEditor2Input(undefined, undefined, { createSettings2EditorModel: () => ({ dispose() {} }) });
  const pending = { key: 'editor.fontSize', value: 17, inFlight: false };
  Object.assign(pane, {
    pendingSettingUpdates: new Map([['font', pending]]), pendingSettingWrite: undefined,
    settingFastUpdateDelayer: { cancel() {} }, settingSlowUpdateDelayer: { cancel() {} }, settingsWriterInput: input,
    async updateChangedSetting(key, value) { writes.push([key, value]); if (writes.length === 1) throw failure; },
  });
  input.registerSettingsWriter(1, { pending: () => pane.pendingSettingUpdates.size > 0 || !!pane.pendingSettingWrite,
    save: () => pane.flushSettingsUpdates(), revert: () => pane.discardSettingsUpdates() });
  try {
    await expect(input.save()).rejects.toBe(failure);
    expect(input.isDirty()).toBe(true);
    expect(pane.pendingSettingUpdates.get('font')).toBe(pending);
    expect(pending.inFlight).toBe(false);
    expect(await input.save()).toBe(input);
    expect(writes).toEqual([['editor.fontSize', 17], ['editor.fontSize', 17]]);
    expect(pane.pendingSettingUpdates.size).toBe(0);
    expect(input.isDirty()).toBe(false);
  } finally { input.dispose(); }
});

scenario('纯 @modified 查询撤销旧文字 AI 搜索，迟到结果不能替换当前过滤来源', async () => {
  const f = await searchFixture();
  try {
    await f.query('make the editor calm');
    expect(f.requests).toHaveLength(1);
    const token = f.requests[0].token, previous = f.pane.searchResultModel;
    expect(token.isCancellationRequested).toBe(false);
    await f.query('@modified');
    const current = f.pane.searchResultModel;
    expect(current).not.toBe(previous);
    expect(current.root.children.map(child => child.setting.key)).toEqual(['sample.alpha', 'sample.beta']);
    expect(token.isCancellationRequested).toBe(true);
    const counts = [...f.countEvents];
    await f.release();
    expect(f.pane.searchResultModel).toBe(current);
    expect(current.getRawResults()[SearchResultIdx.Embeddings]).toBeUndefined();
    expect(current.getRawResults()[SearchResultIdx.AiSelected]).toBeUndefined();
    expect(f.pane.showAiResultsAction.enabled).toBe(false);
    expect(f.pane.aiResultsAvailable.value).toBe(false);
    expect(f.countEvents).toEqual(counts);
    expect(f.traces).toEqual([]);
  } finally { f.close(); }
});

for (const lifecycle of ['clearInput', 'dispose']) scenario(`原 ${lifecycle} 撤销在途 AI 搜索，迟到结果不重建或发布设置模型`, async () => {
  const f = await searchFixture();
  try {
    await f.query('make the editor calm');
    expect(f.requests).toHaveLength(1);
    const token = f.requests[0].token, counts = [...f.countEvents];
    f.pane[lifecycle]();
    const current = f.pane.searchResultModel;
    expect(token.isCancellationRequested).toBe(true);
    await f.release();
    expect(f.pane.searchResultModel).toBe(current);
    expect(f.pane.searchResultModel?.getRawResults()[SearchResultIdx.Embeddings]).toBeUndefined();
    expect(f.pane.searchResultModel?.getRawResults()[SearchResultIdx.AiSelected]).toBeUndefined();
    expect(f.pane.showAiResultsAction.enabled).toBe(false);
    expect(f.pane.aiResultsAvailable.value).toBe(false);
    expect(f.countEvents).toEqual(counts);
    expect(f.traces).toEqual([]);
  } finally { f.close(); }
});

scenario('来源层切换撤销在途 AI 搜索，并按新来源重跑当前查询', async () => {
  const f = await searchFixture();
  try {
    await f.query('make the editor calm');
    expect(f.requests).toHaveLength(1);
    const token = f.requests[0].token, current = f.pane.searchResultModel, counts = [...f.countEvents];
    f.pane.settingsTargetsWidget.settingsTarget = ConfigurationTarget.WORKSPACE;
    f.pane.onDidSettingsTargetChange(ConfigurationTarget.WORKSPACE);
    expect(token.isCancellationRequested).toBe(true);
    expect(f.pane.viewState.settingsTarget).toBe(ConfigurationTarget.WORKSPACE);
    expect(f.refreshes.at(-1)).toEqual([undefined, true, true]);
    await f.release();
    expect(f.pane.searchResultModel).toBe(current);
    expect(current.getRawResults()[SearchResultIdx.Embeddings]).toBeUndefined();
    expect(current.getRawResults()[SearchResultIdx.AiSelected]).toBeUndefined();
    expect(f.pane.showAiResultsAction.enabled).toBe(false);
    expect(f.countEvents).toEqual(counts);
    expect(f.traces).toEqual([]);
  } finally { f.close(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-fingerprints.json'), `${JSON.stringify({ pinnedUpstreamCommit, fingerprints, registered, executed, passed }, null, 2)}\n`);
  await run.writeResult(!executed.length ? 'skipped' : executed.length === passed.length ? 'passed' : 'failed', {
    boundary: 'Integration 源码合同：执行固定上游 sourceTransform 后 AST 调用真实 SettingsEditor2Input、serializer、SourceSettings、SourceDefaults、Settings2EditorModel 及 SettingsEditor2 的 dirty/save/revert、pending writer 和 AI 搜索取消方法。文件文本模型、Pane DI/owner 及 AI 搜索 provider 是具名边界；不经过真实 Gateway、配置 HTTP 或浏览器。',
    registered, executed, passed, filtered: registered.filter(name => !executed.includes(name)),
  });
  await run.close();
});
