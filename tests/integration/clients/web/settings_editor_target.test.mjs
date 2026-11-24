import { t } from '../../../../src/clients/shared/core/i18n/index.js';
import { settingsCategories, settingsCategory, settingsGroupLabel } from '../../../../src/clients/workbench/src/settings/categories.js';
import { requireGatewayWorkspaceRequestTarget } from '../../../../src/clients/web/src/api/workspaceIdentity.js';
import { HttpRequestError } from '../../../../src/clients/web/src/api/http.js';
import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Action, Separator, SubmenuAction, toAction } from '../../../../src/clients/workbench/upstream/src/vs/base/common/actions.js';
import { CancellationToken, CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { Emitter, Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore, MutableDisposable, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { VSBuffer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/buffer.js';
import { isEqual, joinPath } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { Schemas } from '../../../../src/clients/workbench/upstream/src/vs/base/common/network.js';
import { parse, parseTree, findNodeAtLocation } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../../../src/clients/workbench/upstream/src/vs/base/common/jsonEdit.js';
import { Delayer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/async.js';
import { isCancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { StopWatch } from '../../../../src/clients/workbench/upstream/src/vs/base/common/stopwatch.js';
import { ConfigurationTarget } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configuration.js';
import { ServiceCollection } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
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
  environment = { t, ...environment };
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
const settingsHeaderRequests = [], settingsHeaderDirectory = {
  sources: [{ source_key: 'workspace_user', uri: 'boxteam-source://gateway/workspace/source.jsonc' }],
  schema_uri: 'boxteam-schema://gateway/workspace/schema.json',
};
let headerLifetimes = 0;
const { configSourceEditorUri } = evaluate(await source('src/settings/sourceEditor.ts', true),
  ['configSourceScheme', 'configSourceEditorUri'], { URI });
const modelParsed = await source('workbench/services/preferences/common/preferencesModels.ts');
const { nullRange } = evaluate(modelParsed, ['nullRange'], {});
const modelEnvironment = { EditorModel, SettingMatchType: {}, Emitter, Event, Disposable, ConfigurationTarget: { USER_LOCAL: 3, USER_REMOTE: 4, DEFAULT: 7 },
  IConfigurationService, Extensions: { Configuration: 'configuration' }, Registry };
const { Settings2EditorModel, DefaultSettings } = evaluate(modelParsed,
  ['AbstractSettingsModel', 'Settings2EditorModel', 'DefaultSettings'], modelEnvironment);
const { sourceDocumentPath } = evaluate(await source('src/settings/sourceOwner.ts', true), ['sourceDocumentPath'], {});
const { settingsEditorIdentity, settingsHistoryLocation, parseSettingsLocation, SourceSettings } =
  evaluate(await source('src/settings/editor.ts', true),
    ['object', 'at', 'settingsEditorIdentity', 'settingsHistoryLocation', 'parseSettingsLocation', 'SourceDefaults', 'SourceSettings'], {
      Disposable, Emitter, URI, settingsGroupLabel, ConfigurationScope, ConfigurationTarget: { USER_LOCAL: 3, USER_REMOTE: 4, DEFAULT: 7 }, IConfigurationService,
      DefaultSettings, Settings2EditorModel, nullRange, parse, parseTree, findNodeAtLocation, setProperty, applyEdits, configSourceEditorUri,
      sourceDocumentPath,
      configSourceFiles: files => files.getProvider(),
    });
class HeaderElement {
  style = {};
  children = [];
  listeners = new Map();
  attributes = new Map();
  text = '';
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  dispatch(name) { this.listeners.get(name)?.({ preventDefault() {} }); }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
}
const { SettingsContextHeader } = evaluate(await source('src/settings/header.ts', true), ['SettingsContextHeader'], {
  Disposable, DisposableStore, ITargetSettingsContext, ICommandService: serviceIdentifier(), INotificationService: serviceIdentifier(),
  IContextViewService: serviceIdentifier(), IInstantiationService: serviceIdentifier(), IEditorService: serviceIdentifier(),
  IEditorGroupsService: serviceIdentifier(), IQuickInputService: serviceIdentifier(),
  Link: class {
    constructor(container, descriptor, options) {
      this.element = new HeaderElement(); this.element.textContent = descriptor.label;
      this.element.addEventListener('click', options.opener); container.append(this.element);
    }
    dispose() {}
  },
  SelectBox: class {
    constructor() { this.options = []; }
    render() {} onDidSelect() { return { dispose() {} }; }
    setOptions(options) { this.options = options; } setEnabled() {} layout() {} dispose() {}
  }, defaultSelectBoxStyles: {}, sourceStatus: () => '来源状态',
  ConfigurationSourceOwner: class { async list(signal) { settingsHeaderRequests.push(signal); return settingsHeaderDirectory; } },
  workspaceContext: { getSnapshot: () => ({ gatewayId: 'gateway-entry', workspaces: [], loading: false, error: null }),
    subscribe: () => () => {}, async ensureReady() {} },
  createManagementTargetLifecycle: () => {
    headerLifetimes++;
    return { signal: new AbortController().signal, token: CancellationToken.None, check() {}, dispose() {} };
  },
  listManagementTargets: async () => [{ gateway_id: 'gateway-entry', name: '本地', connection_kind: 'local' }],
  managementGateway: () => ({ gateway_id: 'gateway-entry', name: '本地', connection_kind: 'local' }),
  managementWorkspace: () => ({}), selectManagementTarget() {},
  document: { createElement: () => new HeaderElement() }, clearNode: element => { element.textContent = ''; },
});
const { definitionObject, definitionValue } = evaluate(await source('../web/src/components/settings/ConfigurationEntities.tsx', true),
  ['definitionObject', 'definitionValue'], {});
let chooseCredentialImplementation = async () => '';
const { ConfigurationEntityView } = evaluate(await source('src/settings/entities.tsx', true), ['ConfigurationEntityView'],
  { Disposable, definitionObject, definitionValue, generateUuid: () => '00000000-0000-4000-8000-000000000001',
    chooseProviderCredential: (...args) => chooseCredentialImplementation(...args) }, ['create', 'addModel', 'setSecret']);
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
  EditorInput, Emitter, DisposableStore, ServiceCollection, URI, Schemas, isEqual, IPreferencesService,
  parseSettingsLocation, settingsEditorIdentity, ITargetSettingsContext, prepareSettingsPane: (...args) => prepareSettingsPane(...args),
  toDisposable, CancellationError, SettingsEditorIcon: { id: 'settings-editor-test' }, nls: { localize: (_key, text) => text },
});
const { SettingsEditor2InputSerializer } = evaluate(await source('workbench/contrib/preferences/browser/preferences.contribution.ts'),
  ['SettingsEditor2InputSerializer'], { SettingsEditor2Input, parseSettingsLocation, settingsHistoryLocation });

class EditorPaneBoundary {
  managementView = { active: false, hide() {}, show() {}, setVisible() {} };
  clearInput() { this.baseClearCount = (this.baseClearCount ?? 0) + 1; }
  dispose() { this.baseDisposeCount = (this.baseDisposeCount ?? 0) + 1; }
}
const settingsEditorMembers = ['notifySettingsDirty', 'discardSettingsUpdates', 'flushSettingsUpdates', 'cancelSettingsSearch',
  'disableAiSearchToggle', 'clearInput', 'dispose', 'onSearchInputChanged', 'triggerSearch', 'createFilterModel',
  'triggerFilterPreferences', 'onDidFinishSearch', 'doLocalSearch', 'doRemoteSearch', 'doAiSearch', 'getLLMRankedResults',
  'searchWithProvider', '_searchPreferencesModel', 'parseSettingFromJSON', 'onDidSettingsTargetChange',
  'getViewState', '_setOptions', 'showDefinitions', 'showCategory', 'openSettingsFile'];

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
  ConfigurationEntityView: class {}, settingsHistoryLocation,
}, settingsEditorMembers);

function scenario(name, callback) {
  registered.push(name);
  test(name, async () => { executed.push(name); await callback(); passed.push(name); });
}

function sourceSelectionFixture(layers = [{ source_key: 'workspace_user', layer: 'user', presence: 'present', precedence: 1 }]) {
  const auth = new Emitter(), context = new Emitter(), requests = [], picks = [], refreshes = [];
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
      return { sources: layers };
    }
    async read(source, _key, signal) { requests.push({ operation: 'read', owner: this.target, source, signal }); return document; }
  }
  const capture = evaluate(sources.get('local:src/settings/editor.ts'), ['captureSettingsLocation'], {
    CancellationTokenSource, CancellationError, IQuickInputService: {}, ConfigurationSourceOwner: SourceOwnerBoundary,
    requireGatewayWorkspaceRequestTarget,
    managementGateway: () => target,
    workspaceContext: { getSnapshot: () => snapshot, subscribe: listener => {
      const subscription = context.event(listener); return () => subscription.dispose();
    }, async refresh() {
      refreshes.push(snapshot.gatewayId);
      await Promise.resolve();
      if (snapshot.gatewayId === 'entry-A') snapshot = { ...snapshot, workspaces: [
        ...snapshot.workspaces,
        { gateway_id: 'entry-A', workspace_id: 'workspace-new', name: '新登记目标', status: 'ready' },
      ] };
    } },
    onGatewayTokenInvalidated: (_port, listener) => { const subscription = auth.event(listener); return () => subscription.dispose(); },
  }).captureSettingsLocation;
  return { target, quick, requests, picks, document, auth, context, refreshes,
    capture: (token, explicit) => capture({ invokeFunction: callback => callback({ get: () => quick }) }, token ?? CancellationToken.None, target, explicit),
    changeEntry() { snapshot = { ...snapshot, gatewayId: 'entry-C' }; context.fire(); },
    setWorkspaceStatus(status) { snapshot = { ...snapshot, workspaces: snapshot.workspaces.map(workspace => workspace.workspace_id === 'workspace-B' ? { ...workspace, status } : workspace) }; },
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
    expect(f.refreshes).toEqual(['entry-A']);
    expect(f.requests.every(item => item.signal.aborted)).toBe(true);
  } finally { f.dispose(); }
});

scenario('后端表单切换内置来源时，来源名称准确显示只读来源而不沿用默认设置名称', async () => {
  const f = sourceSelectionFixture([{ source_key: 'workspace_inline', layer: 'inline', presence: 'present', precedence: 0 }]);
  try {
    const captured = await f.capture(undefined, { workspace_id: 'workspace-B', source_key: 'workspace_inline', source_layer: 'user' });
    expect(captured.location.name).toBe(t('workbench.settings.builtInSource'));
    expect(captured.location).toMatchObject({ gateway_id: 'gateway-B', workspace_id: 'workspace-B', source_key: 'workspace_inline' });
  } finally { f.dispose(); }
});

scenario('Backend 未就绪时设置来源入口显错且不读取或打开虚假空配置', async () => {
  const f = sourceSelectionFixture();
  try {
    f.setWorkspaceStatus('starting');
    await expect(f.capture()).rejects.toThrow('尚未就绪（starting）');
    expect(f.refreshes).toEqual(['entry-A']);
    expect(f.requests).toEqual([]);
    expect(f.picks).toHaveLength(1);
  } finally { f.dispose(); }
});

scenario('配置菜单明确指定层时直接打开原 owner 来源，不弹目标或文件选择且不回退到其它层', async () => {
  const f = sourceSelectionFixture();
  try {
    const captured = await f.capture(undefined, { workspace_id: 'workspace-B', source_layer: 'user' });
    expect(captured.location).toEqual({ version: 2, gateway_id: 'gateway-B', gateway_connection_id: 'route-B',
      workspace_id: 'workspace-B', source_key: 'workspace_user', name: t('workbench.settings.backendSettings') });
    expect(f.picks).toEqual([]);
    expect(f.requests.map(item => item.operation)).toEqual(['list', 'read']);
    await expect(f.capture(undefined, { workspace_id: 'workspace-B', source_layer: 'workspace' })).rejects.toThrow(t('settings.editor.selectedSourceIdentityMissing'));
    expect(f.requests.map(item => item.operation)).toEqual(['list', 'read', 'list']);
    expect(f.picks).toEqual([]);
  } finally { f.dispose(); }
});

scenario('设置页隐藏释放声明长连接，显示恢复一次订阅且不改写草稿，失效后不重连', () => {
  const f = makeSource(location());
  const subscription = new MutableDisposable();
  let opened = 0, closed = 0;
  const observe = () => { opened += 1; return { dispose() { closed += 1; } }; };
  subscription.value = observe();
  f.source.bindSchemaObservation(subscription, observe);
  const original = f.model.textEditorModel.getValue();
  try {
    f.source.setVisible(true); expect(opened).toBe(1);
    f.source.setVisible(false); expect(closed).toBe(1);
    f.source.setVisible(false); expect(closed).toBe(1);
    f.source.setVisible(true); f.source.setVisible(true); expect(opened).toBe(2);
    expect(f.model.textEditorModel.getValue()).toBe(original);
    f.source.invalidateSchemas(); f.source.setVisible(true);
    expect(opened).toBe(2); expect(closed).toBe(2);
  } finally { subscription.dispose(); f.source.dispose(); }
});

scenario('目标来源选择先刷新已登记工作区快照并保留新候选', async () => {
  const f = sourceSelectionFixture();
  f.target.gateway_id = 'entry-A';
  f.target.gateway_connection_id = undefined;
  f.quick.pick = async (items, options) => options.title === '选择设置来源的所属目标'
    ? items.find(item => item.workspaceId === 'workspace-new')
    : items[0];
  try {
    const captured = await f.capture();
    expect(captured.location.workspace_id).toBe('workspace-new');
    expect(captured.location.gateway_id).toBe('entry-A');
    expect(f.refreshes).toEqual(['entry-A']);
    expect(f.requests.map(item => item.operation)).toEqual(['list', 'read']);
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
    getPositionAt: offset => { const lines = value.slice(0, offset).split('\n'); return { lineNumber: lines.length, column: lines.at(-1).length + 1 }; },
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
  const gateway = !locationValue.workspace_id;
  const jsonc = gateway
    ? `// 保留来源注释\n{\n  "config_version": 6,\n  "extends": "base",\n  "settings": { "gateway": {}, "network": {} },\n  "profiles": { "base": { "settings": { "network": {} } } }\n}\n`
    : `// 保留来源注释\n{\n  "config_version": 2,\n  "untouched": { "value": 9 }\n}\n`;
  const properties = {
    logging: { type: 'object', properties: { runtime: { type: 'object', properties: { level: { type: 'string', default: 'info' } } } } },
    'a.b': { type: 'object', properties: { c: { type: 'string' } } },
    a: { type: 'object', properties: { 'b.c': { type: 'string' } } },
  };
  const document = {
    gateway_id: locationValue.gateway_id, ...(gateway ? {} : { workspace_id: locationValue.workspace_id }), source_key: locationValue.source_key,
    jsonc, writable: true,
    json_schema: gateway ? { type: 'object', properties: { config_version: { type: 'integer' }, settings: { '$ref': '#/$defs/scopeSettings' } },
      $defs: { scopeSettings: { type: 'object', properties: { gateway: { type: 'object' } } },
        gatewaySettings: { type: 'object', properties }, gatewayPayload: { type: 'object', properties: {
          logging: { '$ref': '#/$defs/gatewaySettings/properties/logging' },
          'a.b': { '$ref': '#/$defs/gatewaySettings/properties/a.b' },
          a: { '$ref': '#/$defs/gatewaySettings/properties/a' },
          network: { type: 'object', properties: { proxy: { type: 'object', properties: { mode: { type: 'string' } } } } },
        } } } } : { type: 'object', properties },
    source_effective: { logging: { runtime: { level: 'effective' } } },
    ...(gateway ? { source_inherited: { logging: { runtime: { level: 'inherited' } } } } : {}),
    layers: [
      { source_key: 'gateway_inline', precedence: 1, payload: { logging: { runtime: { level: 'inherited' } } } },
      { source_key: locationValue.source_key, precedence: 10, payload: {} },
    ],
  };
  const reverted = new Emitter();
  const model = makeTextFileModel(jsonc, value => reverted.fire(value)), provider = { onDidChangeSource: Event.None, currentDocument(resource) {
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

async function settingsHeader(target) {
  const element = new HeaderElement();
  const quickCalls = [];
  const header = new SettingsContextHeader(element, () => {}, async () => {}, { target }, { error() {} },
    { async pick(items, options) { quickCalls.push({ items, options }); } }, {}, { createInstance: (Type, ...args) => new Type(...args) });
  await Promise.resolve(); await Promise.resolve();
  return { element, header, quickCalls, text: element.textContent };
}

scenario('功能导航先选分类，独立范围保存到原 owner；Gateway 与工作区不互相激活', async () => {
  const captures = [], opens = [], selections = [], errors = [];
  let finishReady, initialized = false;
  const ready = new Promise(resolve => { finishReady = () => { initialized = true; resolve(); }; });
  const local = { gateway_id: 'gateway-entry', connection_kind: 'local', name: '本地' };
  const remote = { gateway_id: 'gateway-remote', gateway_connection_id: 'ssh-remote', connection_kind: 'remote_gateway', name: '远端' };
  const alternate = { ...remote, gateway_connection_id: 'ssh-alternate', name: '远端备用连接' };
  const { SettingsCategoryNavigation } = evaluate(await source('src/settings/navigation.ts', true), ['SettingsCategoryNavigation'], {
    Disposable, MutableDisposable, CancellationTokenSource, isCancellationError, CancellationError, ITargetSettingsContext, settingsCategories, settingsCategory,
    IInstantiationService: serviceIdentifier(), IEditorService: serviceIdentifier(), IEditorGroupsService: serviceIdentifier(),
    IContextViewService: serviceIdentifier(), INotificationService: serviceIdentifier(), IEditorProgressService: serviceIdentifier(),
    SettingsEditor2Input: class { constructor(location) { this.location = location; } }, EditorsOrder: { MOST_RECENTLY_ACTIVE: 0 },
    SettingsTreeGroupElement: class { constructor(id, count, label) { Object.assign(this, { id, count, label, children: [] }); } dispose() {} },
    SelectBox: class { render() {} onDidSelect() { return { dispose() {} }; } setOptions(options, selected) { this.options = options; this.selected = selected; } select(index) { this.selected = index; } setEnabled() {} dispose() {} },
    defaultSelectBoxStyles: {}, document: { createElement: () => new HeaderElement() },
    createManagementTargetLifecycle: () => {
      if (!initialized) throw new Error('Gateway 尚未初始化');
      return { signal: new AbortController().signal, token: CancellationToken.None, check() {}, dispose() {} };
    },
    workspaceContext: { ensureReady: () => ready, getSnapshot: () => ({ gatewayId: local.gateway_id, workspaces: [
      { gateway_id: local.gateway_id, workspace_id: 'local-workspace', name: '本地工作区' },
      { gateway_id: remote.gateway_id, workspace_id: 'remote-workspace', name: '远端工作区' },
    ] }) },
    listManagementTargets: async () => [local, remote, alternate], managementTargetDisplayName: target => target.name,
    managementGateway: (_scope, id) => id === remote.gateway_id ? remote : local,
    managementWorkspace: (_scope, target) => ({ workspace_id: target.gateway_id === local.gateway_id ? 'local-workspace' : 'remote-workspace' }),
    selectManagementTarget: (...args) => selections.push(args),
    captureSettingsLocation: async (_parent, _token, target, scope) => {
      captures.push({ target, scope }); return { location: { ...target, ...scope }, document: {} };
    },
  });
  let exists = true, changed = 0;
  const createPane = target => {
    const context = { target };
    return new SettingsCategoryNavigation(new HeaderElement(), async () => {}, () => { changed++; },
      async (location, document, token) => { token.throwIfCancellationRequested?.(); opens.push({ location }); context.target = location ? { location, document: { layer: location.source_layer === 'workspace' ? 'workspace' : 'user' } } : undefined; },
      context, {}, {}, { error: error => errors.push(error) }, { show: () => ({ done() {} }) });
  };
  const pane = createPane(undefined);
  try {
    const pending = pane.openCategory('providers');
    await Promise.resolve(); expect(captures).toHaveLength(0);
    finishReady(); await pending;
    expect(captures[0]).toEqual({ target: local, scope: { workspace_id: 'local-workspace', source_key: undefined, source_layer: 'user' } });
    expect(pane.selected).toBe('providers');
    expect(opens).toHaveLength(1);
    // 模拟来源切换前后的真实分组：TOC 只保留导航结构，字段模型不跨来源存活。
    const Group = pane.selectedNode.constructor;
    const group = (id, children = []) => Object.assign(new Group(id, 1, id), { children });
    const clientRoot = group('client-root', [group('editor', [group('font')])]);
    pane.setCategory('client');
    const clientNavigation = pane.decorate(clientRoot, () => []);
    expect(clientNavigation.every(node => node.collapsed === undefined)).toBe(true);
    const clientBranch = clientNavigation[0].children[0];
    expect(clientBranch.element).not.toBe(clientRoot.children[0]);
    pane.setCategory('providers');
    const providerRoot = group('provider-root', [group('llm')]);
    const providerNavigation = pane.decorate(providerRoot, () => []);
    expect(providerNavigation[0].children[0].element).toBe(clientBranch.element);
    expect(providerNavigation[0].element.children).toEqual([]);
    expect(pane.categoryForNode(clientBranch.element).id).toBe('client');
    expect(pane.currentGroup(clientBranch.element.id)).toBeUndefined();
    const ownedIds = [...new Set(settingsCategories.flatMap(category => [...category.groups]))];
    const remainingIds = ['runtime', 'context', 'ui', 'custom-package/custom-plugin'];
    const sourceRoot = group('source-root', [...ownedIds, ...remainingIds].map(id => group(id)));
    for (const definition of settingsCategories.filter(category => category.id !== 'client')) {
      pane.setCategory(definition.id);
      pane.decorate(sourceRoot, () => []);
      const expectedIds = definition.id === 'runtime' ? remainingIds : [...definition.groups];
      expect(pane.filter.children.map(child => child.id)).toEqual(expectedIds);
      for (const child of pane.filter.children) expect(sourceRoot.children).toContain(child);
    }
    pane.setCategory('client');
    pane.decorate(clientRoot, () => []);
    expect(pane.currentGroup(clientBranch.element.id)).toBe(clientRoot.children[0]);
    pane.setCategory('providers');
    await pane.openCategory('agents');
    expect(opens).toHaveLength(1);
    expect(pane.selected).toBe('agents');
    await pane.open('providers', remote, 'workspace', 'remote-workspace');
    expect(captures.at(-1)).toEqual({ target: remote, scope: { workspace_id: 'remote-workspace', source_key: undefined, source_layer: 'workspace' } });
    expect(selections.at(-1)).toEqual(['settings', remote, 'remote-workspace']);
    await pane.openGatewayConfiguration(remote);
    expect(pane.selected).toBe('observability');
    expect(captures.at(-1)).toEqual({ target: remote, scope: { workspace_id: undefined, source_key: undefined, source_layer: 'user' } });
    await pane.openCategory('connections');
    expect(captures.at(-1)).toEqual({ target: remote, scope: { workspace_id: undefined, source_key: undefined, source_layer: 'user' } });
    await pane.openCategory('client');
    expect(opens.at(-1)).toEqual({ location: undefined });
    expect(pane.selected).toBe('client');
    expect(changed).toBeGreaterThan(0);
    const restored = createPane({ location: { ...alternate, workspace_id: 'remote-workspace' }, document: { layer: 'workspace' } });
    try {
      await restored.ready;
      expect(restored.currentTarget()).toEqual(alternate);
      expect(restored.targetSelector.selected).toBe(2);
    } finally { restored.dispose(); }
    expect(errors).toEqual([]);
  } finally { pane.dispose(); }

});

scenario('原设置过滤器按功能包含真实来源分组，不依赖伪造父链或字段数量', async () => {
  class Setting {}
  const { SettingsTreeElement, SettingsTreeGroupElement } = evaluate(await source('workbench/contrib/preferences/browser/settingsTreeModels.ts'),
    ['SettingsTreeElement', 'SettingsTreeGroupElement'], { Disposable, Emitter, SettingsTreeSettingElement: Setting });
  const { SettingsTreeFilter } = evaluate(await source('workbench/contrib/preferences/browser/settingsTree.ts'), ['SettingsTreeFilter'], {
    SettingsTreeGroupElement, SettingsTreeSettingElement: Setting, settingsCategory,
  }, ['groupIsRelatedToCategory', 'settingContainedInGroup']);
  const filter = new SettingsTreeFilter();
  const root = new SettingsTreeGroupElement('root', undefined, 'root', 0, false);
  const connections = new SettingsTreeGroupElement('workspaces', 1, '连接', 1, false);
  const logs = new SettingsTreeGroupElement('logging', 1, '日志', 1, false);
  const category = new SettingsTreeGroupElement('boxteam.settings.category.connections', undefined, '连接与远程目标', 0, false);
  connections.parent = logs.parent = root;
  const setting = new Setting(); setting.setting = { key: 'workspaces' };
  connections.children = [setting]; category.children = [connections];
  expect(filter.groupIsRelatedToCategory(root, category)).toBe(true);
  expect(filter.groupIsRelatedToCategory(connections, category)).toBe(true);
  expect(filter.groupIsRelatedToCategory(logs, category)).toBe(false);
  expect(filter.settingContainedInGroup({ key: 'workspaces' }, category)).toBe(true);
  expect(connections.parent).toBe(root);
  expect(category.count).toBeUndefined();
  category.dispose(); connections.dispose(); logs.dispose(); root.dispose();
});

scenario('普通设置不读取业务来源；来源层和保存详情折叠在所属内容中', async () => {
  const fixture = makeSource(location({ workspace_id: undefined }));
  try {
    const previousLifetimes = headerLifetimes;
    const gateway = await settingsHeader(fixture.source);
    expect(headerLifetimes).toBe(previousLifetimes);
    expect(gateway.text).not.toContain(t('workbench.settings.modelsProviders'));
    expect(gateway.text).not.toContain(t('configuration.entities.agentsTitle'));
    gateway.header.dispose();
    const window = await settingsHeader(undefined);
    expect(headerLifetimes).toBe(previousLifetimes);
    expect(window.text).toBe('');
    expect(window.element.style.display).toBe('none');
    window.header.dispose();
    const workspace = makeSource(location());
    try {
      const header = await settingsHeader(workspace.source);
      expect(header.text).not.toContain(t('configuration.entities.agentsTitle'));
      const details = header.element.children.at(-1).children[0];
      details.open = true;
      const locationButton = header.element.children.at(-1).children[0].children.find(element => element.textContent === t('settings.editor.sourceLocations'));
      expect(locationButton).toBeDefined();
      header.header.update();
      expect(header.element.children.at(-1).children[0]).toBe(details);
      expect(details.open).toBe(true);
      expect(details.children).toContain(locationButton);
      locationButton.dispatch('click');
      await new Promise(resolve => setTimeout(resolve, 0));
      expect(settingsHeaderRequests).toHaveLength(1);
      expect(headerLifetimes).toBe(previousLifetimes + 1);
      expect(header.quickCalls).toHaveLength(1);
      expect(header.quickCalls[0].options.title).toBe(t('settings.editor.sourceLocations'));
      expect(header.quickCalls[0].items.map(item => item.detail)).toEqual([
        'boxteam-source://gateway/workspace/source.jsonc', 'boxteam-schema://gateway/workspace/schema.json',
      ]);
      header.header.dispose();
    } finally { workspace.source.dispose(); }
  } finally { fixture.source.dispose(); }
});

scenario('创建同 ID Agent 须确认继承覆盖，取消不编辑，确认后的 inline 提示词解除下层文件覆盖', async () => {
  for (const confirmed of [false, true]) {
    const edits = [], confirmations = [];
    const values = ['coder', '本层名称', '本次输入的提示词'];
    const providers = { primary: { defaults: {}, models: { main: { model: 'server-model' } } } };
    const source = { readDraft: () => ({}), model: { textEditorModel: { getVersionId: () => 1 } },
      document: { source_key: 'workspace', source_effective: { llm: { providers } }, layers: [
        { source_key: 'user', precedence: 1, payload: { agents: { coder: { instructions: { system_prompt_file: 'boxteam-source://gateway/prompt.md' } } },
          llm: { providers } } },
        { source_key: 'workspace', precedence: 2, payload: {} },
      ] }, editPath: (...args) => edits.push(args) };
    const view = Object.assign(new ConfigurationEntityView(), { kind: 'agents', source, check() {}, render() {},
      lifetime: { token: CancellationToken.None }, input: async () => values.shift(), quick: { pick: async choices => {
        expect(choices.map(choice => choice.model)).toEqual([{ provider_id: 'primary', model_id: 'main' }]);
        return choices[0];
      } },
      dialogs: { confirm: async options => { confirmations.push(options); return { confirmed }; } } });
    await view.create();
    expect(confirmations).toHaveLength(1);
    expect(confirmations[0].message).toContain('coder');
    expect(edits).toHaveLength(confirmed ? 1 : 0);
    if (confirmed) expect(edits[0]).toEqual([['agents', 'coder'], { name: '本层名称',
      instructions: { system_prompt: '本次输入的提示词', system_prompt_file: null },
      model: { primary: { provider_id: 'primary', model_id: 'main' }, fallbacks: [] } }]);
    view.dispose();
  }
});

scenario('新 Provider 只建立连接草稿且零密钥请求，同名服务生成新 ID，协议选择期间编辑变化拒绝覆盖', async () => {
  for (const failure of ['duplicate', 'changed', 'none']) {
    let version = 1, credentialRequests = 0;
    const edits = [], inputs = ['team'];
    const source = { readDraft: () => ({}), model: { textEditorModel: { getVersionId: () => version } },
      document: { source_effective: { llm: { providers: failure === 'duplicate' ? { team: { models: {} } } : {} } } },
      editPath: (...args) => edits.push(args) };
    chooseCredentialImplementation = async () => { credentialRequests++; return 'secret:created'; };
    const view = Object.assign(new ConfigurationEntityView(), { kind: 'providers', source, check() {}, render() {},
      lifetime: { token: CancellationToken.None }, input: async () => inputs.shift(), quick: { pick: async items => {
        if (failure === 'changed') version++;
        return items[0];
      } } });
    try {
      if (failure !== 'changed') {
        await view.create();
        const id = edits[0][0][2];
        if (failure === 'duplicate') { expect(id).toMatch(/^provider-/); expect(id).not.toBe('team'); }
        else expect(id).toBe('team');
        expect(edits).toEqual([[['llm', 'providers', id], { name: 'team', defaults: { custom_llm_provider: 'openai', api_key: '',
          api_mode: { protocol: 'chat_completions', model_info: {}, supports_reasoning: {} } }, models: {} }]]);
      } else {
        await expect(view.create()).rejects.toThrow(t('configuration.entities.definitionChanged', 'team'));
        expect(edits).toEqual([]);
      }
      expect(credentialRequests).toBe(0);
    } finally { view.dispose(); }
  }
});

scenario('凭据选择只向原脏草稿写公开引用，零保存且拒绝选择期间的并发编辑', async () => {
  for (const concurrent of [false, true]) {
    let version = 1;
    const edits = [], commits = [];
    const source = { location: { gateway_id: 'gateway-A' }, model: { textEditorModel: { getVersionId: () => version } },
      editPath: (...args) => edits.push(args) };
    chooseCredentialImplementation = async () => { if (concurrent) version++; return 'secret:sec_' + '1'.repeat(32); };
    const view = Object.assign(new ConfigurationEntityView(), { source, check() {},
      lifetime: { token: CancellationToken.None }, connectionEdits: new Map(), commitConnection: id => commits.push(id) });
    try {
      if (concurrent) {
        await expect(view.setSecret('team')).rejects.toThrow(t('configuration.entities.definitionChanged', 'team'));
        expect(edits).toEqual([]); expect(commits).toEqual([]);
      } else {
        await view.setSecret('team');
        expect(edits).toEqual([[['llm', 'providers', 'team', 'defaults', 'api_key'], 'secret:sec_' + '1'.repeat(32)]]);
        expect(commits).toEqual(['team']);
      }
    } finally { view.dispose(); }
  }
});

scenario('Model 稳定键局部添加和复制，草稿变化时不写入过期预设', async () => {
  for (const copy of [false, true]) {
    const edits = [], inputs = ['new-model'];
    let version = 1;
    const preset = { model: 'server-model', overrides: { request_options: { parameters: { temperature: 0 } } } };
    const source = { readDraft: () => ({}), document: { source_effective: {} },
      model: { textEditorModel: { getVersionId: () => version } },
      preflightDraft: async () => ({ candidate: { llm: { providers: { team: { models: { preset } } } } } }),
      editPath: (...args) => edits.push(args) };
    const view = Object.assign(new ConfigurationEntityView(), { source, check() {}, render() {},
      lifetime: { signal: new AbortController().signal }, input: async () => inputs.shift() });
    try {
      await view.addModel('team', copy ? 'preset' : undefined);
      expect(edits).toEqual([[['llm', 'providers', 'team', 'models', 'new-model'], copy ? { ...preset, name: 'new-model' } : { model: 'new-model' }]]);
      inputs.splice(0, inputs.length, 'stale');
      source.preflightDraft = async () => { version++; return { candidate: { llm: { providers: { team: { models: { preset } } } } } }; };
      await expect(view.addModel('team', 'preset')).rejects.toThrow(t('configuration.entities.definitionChanged', 'team'));
      expect(edits).toHaveLength(1);
    } finally { view.dispose(); }
  }
});

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

const secretFunctions = ['reference', 'creationKey', 'pendingCreations', 'writeCreations', 'forgetCreation',
  'isWriteRejected', 'targetName', 'createSecret', 'recoverCreation', 'chooseProviderCredential', 'manageSecrets'];

scenario('命名密钥入口的目标提示与请求沿同一条明确连接，连接失效不借用同 Gateway 的其它连接', async () => {
  const primary = { gateway_id: 'gateway-remote', gateway_connection_id: 'ssh-primary', name: '主连接' };
  const alternate = { ...primary, gateway_connection_id: 'ssh-alternate', name: '备用连接' };
  const clients = [], prompts = [];
  const lifetime = { signal: new AbortController().signal, token: CancellationToken.None, check() {}, dispose() {} };
  const { chooseProviderCredential, manageSecrets } = evaluate(await source('src/settings/secrets.ts', true),
    secretFunctions, {
      StorageScope: { WORKSPACE: 1 },
      SecretsClient: class {
        constructor(target) { this.target = target; }
        async list() { clients.push(this.target); return { items: [] }; }
      },
      listManagementTargets: async () => [primary, alternate], managementTargetDisplayName: target => target.name,
      createManagementTargetLifecycle: () => lifetime,
    });
  const quick = { async pick(_items, options) { prompts.push(options); return undefined; } };
  const storage = { getObject(_key, _scope, fallback) { return fallback; } };
  for (const invoke of [target => chooseProviderCredential(target, quick, lifetime, storage), target => manageSecrets(target, quick, {}, storage)]) {
    await invoke(alternate);
    expect(JSON.stringify(prompts.at(-1))).toContain(alternate.name);
    expect(JSON.stringify(prompts.at(-1))).not.toContain(primary.name);
    expect(clients.at(-1)).toEqual(alternate);
    const reads = clients.length, picks = prompts.length;
    await expect(invoke({ ...alternate, gateway_connection_id: 'ssh-removed' })).rejects.toThrow(t('workbench.settings.targetOffline'));
    expect(clients).toHaveLength(reads);
    expect(prompts).toHaveLength(picks);
  }
});

scenario('密钥创建预定 ID，丢回执后只读原目标恢复，持久展示信息不含密码或摘要', async () => {
  const target = { gateway_id: 'gateway-A', gateway_connection_id: 'route-A', name: '原目标' };
  const sibling = { ...target, gateway_connection_id: 'route-B', name: '另一路由' };
  const state = new Map(), requests = [];
  const storage = { getObject(key, _scope, fallback) { return state.has(key) ? JSON.parse(state.get(key)) : fallback; },
    store(key, value) { state.set(key, JSON.stringify(value)); }, remove(key) { state.delete(key); },
    async flush() { requests.push({ method: 'FLUSH', state: [...state.values()] }); } };
  const lifetime = { signal: new AbortController().signal, token: CancellationToken.None, check() {}, dispose() {} };
  const id = 'sec_00000000000040008000000000000001';
  const item = { id, name: '共享 Key', revision: 3, updated_at: 'updated' };
  let listItems = [item];
  const module = evaluate(await source('src/settings/secrets.ts', true), secretFunctions, {
    generateUuid: () => '00000000-0000-4000-8000-000000000001',
    StorageScope: { WORKSPACE: 1 }, StorageTarget: { MACHINE: 0 }, HttpRequestError: class extends Error {},
    listManagementTargets: async () => [target, sibling], managementTargetDisplayName: target => target.name,
    createManagementTargetLifecycle: () => lifetime,
    SecretsClient: class {
      constructor(target) { this.target = target; }
      async list() { requests.push({ method: 'GET', target: this.target }); return { items: listItems }; }
      async create(payload) { requests.push({ method: 'POST', target: this.target, payload }); throw new Error('connection lost after publication'); }
    },
  });
  const answers = ['共享 Key', 'transient-secret-value'];
  const quick = { async input() { return answers.shift(); }, async pick(items) { return items.find(item => item.action === 'create'); } };
  await expect(module.chooseProviderCredential(target, quick, lifetime, storage)).rejects.toThrow(t('settings.secrets.creationUnknown', id));
  expect(requests.filter(item => item.method === 'POST')).toEqual([{ method: 'POST', target,
    payload: { id, name: '共享 Key', value: 'transient-secret-value' } }]);
  expect(requests.map(item => item.method)).toEqual(['GET', 'FLUSH', 'POST']);
  expect(requests[1].state.map(value => JSON.parse(value))).toEqual([[{ kind: 'create', id }]]);
  expect(JSON.stringify([...state])).not.toContain('transient-secret-value');
  expect([...state.values()].map(value => JSON.parse(value))).toEqual([[{ kind: 'create', id }]]);
  await module.chooseProviderCredential(sibling, { async pick(items) {
    expect(items.some(item => item.action?.startsWith('recover:'))).toBe(false);
    return undefined;
  } }, lifetime, storage);
  listItems = [];
  await expect(module.chooseProviderCredential(target, { async pick(items) {
    return items.find(item => item.action === 'recover:' + id);
  } }, lifetime, storage)).rejects.toThrow(t('settings.secrets.creationNotFound', id));
  expect(state.size).toBe(1);
  listItems = [item];
  let picks = 0;
  const recovered = await module.chooseProviderCredential(target, { async pick(items) {
    if (++picks === 1) return items.find(item => item.action === 'recover:' + id);
    expect(items[0].detail).toBe(t('settings.secrets.revision', 3, 'updated'));
    return items[0];
  } }, lifetime, storage);
  expect(recovered).toBe('secret:' + id);
  expect(state.size).toBe(0);
  expect(requests.filter(item => item.method === 'POST')).toHaveLength(1);
  expect(requests.filter(item => item.method === 'GET' && item.target.gateway_connection_id === target.gateway_connection_id)).toHaveLength(5);
});

scenario('密钥创建的明确拒绝不保留未知操作，恢复身份未落盘时不发请求', async () => {
  const id = 'sec_' + '0'.repeat(32), target = { gateway_id: 'gateway-A' };
  for (const storageFails of [false, true]) {
    const state = new Map(), calls = [];
    const failure = storageFails ? new Error('storage unavailable')
      : new HttpRequestError(409, 'Conflict', 'ID already exists', '/api/gateway/secrets', 'req_rejected');
    const storage = { getObject: key => state.get(key) ?? [], store: (key, items) => state.set(key, items),
      remove: key => state.delete(key), async flush() { calls.push('FLUSH'); if (storageFails) throw failure; } };
    const { createSecret } = evaluate(await source('src/settings/secrets.ts', true), secretFunctions, {
      generateUuid: () => '0'.repeat(32), HttpRequestError, StorageScope: { WORKSPACE: 1 }, StorageTarget: { MACHINE: 0 },
    });
    const inputs = ['Key', 'transient-secret'];
    await expect(createSecret({ async create() { calls.push('POST'); throw failure; } }, target, '原目标',
      { async input() { return inputs.shift(); } }, { check() {} }, storage)).rejects.toBe(failure);
    expect(calls).toEqual(storageFails ? ['FLUSH'] : ['FLUSH', 'POST']);
    expect([...state.values()]).toEqual(storageFails ? [[{ kind: 'create', id }]] : []);
  }
});

scenario('密钥更新或删除区分明确拒绝与丢回执，当前版本或缺失不能冒充原操作成功', async () => {
  for (const [action, rejected] of [['rename', false], ['rename', true], ['delete', false], ['delete', true]]) {
    const target = { gateway_id: 'gateway-A', gateway_connection_id: 'route-A', name: '原目标' };
    const item = { id: 'sec_' + '1'.repeat(32), name: 'Key', revision: 1, updated_at: 'before' };
    const lifetime = { signal: new AbortController().signal, token: CancellationToken.None, check() {}, dispose() {} };
    const calls = []; let reads = 0, picks = 0;
    const failure = rejected ? new HttpRequestError(409, 'Conflict', 'original revision rejected', '/api/gateway/secrets', 'req_rejected')
      : new Error('lost response');
    const { manageSecrets } = evaluate(await source('src/settings/secrets.ts', true), secretFunctions, {
      HttpRequestError, StorageScope: { WORKSPACE: 1 }, listManagementTargets: async () => [target], managementTargetDisplayName: target => target.name,
      createManagementTargetLifecycle: () => lifetime,
      SecretsClient: class {
        async list() { reads++; return { items: reads === 1 ? [item]
          : action === 'rename' ? [{ ...item, revision: 3, updated_at: 'concurrent' }] : [] }; }
        async update(...args) { calls.push(['PUT', ...args]); throw failure; }
        async delete(...args) { calls.push(['DELETE', ...args]); throw failure; }
      },
    });
    await expect(manageSecrets(target, {
      async pick(items) { return ++picks === 1 ? items.find(choice => choice.item) : items.find(choice => choice.action === action); },
      async input() { return 'Renamed'; },
    }, { async confirm() { return { confirmed: true }; } }, { getObject(_key, _scope, fallback) { return fallback; } })).rejects.toThrow(
      rejected ? failure.message : t('settings.secrets.writeUnknown', item.id, action === 'rename'
        ? t('settings.secrets.revision', 3, 'concurrent') : t('settings.secrets.currentMissing')));
    expect(calls).toHaveLength(1);
    expect(reads).toBe(2);
  }
});

scenario('source Input 身份只由 Gateway、工作区和 source key 决定，历史序列化剥离连接路由', () => {
  const preferences = { createSettings2EditorModel() { throw new Error('来源 Input 不应创建窗口设置模型'); } };
  const original = location(), input = new SettingsEditor2Input(original, undefined, preferences);
  const sameOwner = new SettingsEditor2Input(location({ name: '重命名', gateway_connection_id: 'connection-B' }), undefined, preferences);
  try {
    expect(input.location).toEqual(original);
    expect(input.getName()).toBe(t('workbench.settings.editorName'));
    expect(input.getDescription()).toBeUndefined();
    expect(input.getTitle()).toBe(t('workbench.settings.editorName'));
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
    const selection = targetSettings.settingSelection('a%2Eb.c', true);
    const lines = draft.split('\n');
    expect(lines[selection.startLineNumber - 1].slice(selection.startColumn - 1, selection.endColumn - 1)).toBe('"dot-property"');
    expect(targetSettings.settingSelection('unknown.field', true)).toBeUndefined();
    model.dispose();
  } finally { targetSettings.dispose(); }
});

scenario('Gateway v6 信封编辑业务与网络字段，保留 extends、profiles、注释和后端平铺投影', async () => {
  const target = location({ workspace_id: undefined, source_key: 'gateway_user' }), f = makeSource(target);
  try {
    f.document.layers[0].payload.logging.runtime.level = '物理层值不可代替 graph 继承';
    const keys = f.source.groups.flatMap(group => group.sections.flatMap(section => section.settings.map(setting => setting.key)));
    expect(keys).toContain('logging.runtime.level');
    expect(keys).toContain('network.proxy.mode');
    expect(keys).not.toContain('settings');
    expect(f.source.inspect('logging.runtime.level')).toMatchObject({ defaultValue: 'inherited', value: 'effective' });
    f.source.edit('logging.runtime.level', 'debug');
    f.source.edit('network.proxy.mode', 'direct');
    const draft = f.model.textEditorModel.getValue(), parsed = parse(draft);
    expect(draft).toContain('// 保留来源注释');
    expect(parsed).toMatchObject({ config_version: 6, extends: 'base', profiles: { base: { settings: { network: {} } } },
      settings: { gateway: { logging: { runtime: { level: 'debug' } } }, network: { proxy: { mode: 'direct' } } } });
    expect(f.source.inspect('logging.runtime.level')).toMatchObject({ userRemoteValue: 'debug', value: 'debug' });
    expect(f.source.settingSelection('logging.runtime.level', true)).toBeDefined();
    expect(parsed.logging).toBeUndefined();
    expect(parsed.network).toBeUndefined();
    f.document.source_effective = { plugins: { runtime: { entries: [
      { package_id: 'sample.package', plugin_id: 'sample', package_version: '1.0.0', enabled: true },
    ] } } };
    f.source.replaceSchemas({ plugins: [{ package_id: 'sample.package', plugin_id: 'sample', package_version: '1.0.0',
      name: 'Sample', configuration_schema: { type: 'object', properties: { mode: { type: 'string' } } } }] });
    f.source.edit('plugins.configuration.values.sample%2Epackage.sample.mode', 'safe');
    f.source.editPath(['plugins', 'configuration', 'values', 'unused.package', 'unused', 'mode'], 'waiting');
    const pluginDraft = parse(f.model.textEditorModel.getValue());
    expect(pluginDraft.settings.gateway.plugins.configuration.values['sample.package'].sample.mode).toBe('safe');
    expect(f.source.unboundPluginParameters()).toEqual([{ packageId: 'unused.package', pluginId: 'unused', installation: 'unavailable' }]);
  } finally { f.source.dispose(); }
});

scenario('设置行内 JSON 入口保留精确字段定位和发起组，不受等待期间焦点或来源切换影响', async () => {
  const pane = new SettingsEditor2(), opened = [], pending = Promise.withResolvers();
  const resource = URI.parse('boxteam-config-source://gateway-A/workspace-A/source.jsonc');
  const selection = { startLineNumber: 5, startColumn: 4, endLineNumber: 5, endColumn: 12 };
  const source = { resource, settingSelection: (key, edit) => { expect(key).toBe('a%2Eb.c'); expect(edit).toBe(true); return selection; } };
  Object.assign(pane, { group: { id: 17 }, targetSettingsContext: { target: source },
    flushSettingsUpdates: () => pending.promise, editorService: { openEditor: (...args) => { opened.push(args); } } });
  const opening = pane.openSettingsFile({ revealSetting: { key: 'a%2Eb.c', edit: true } });
  pane.group = { id: 99 }; pane.targetSettingsContext.target = undefined;
  pending.resolve(); await opening;
  expect(opened).toEqual([[{ resource, options: { pinned: true, selection }, languageId: 'jsonc', forceFile: true }, 17]]);
});

scenario('来源标题仅提示本目标非空未绑定参数，保留 JSONC 入口并区分安装态', async () => {
  const packageId = 'schema.probe', pluginId = 'preference', version = '1.0.0';
  const declaration = { package_id: packageId, plugin_id: pluginId, package_version: version, name: '参数插件',
    configuration_schema: { type: 'object', properties: { mode: { type: 'string' } } } };
  const a = makeSource(location());
  const b = makeSource(location({ gateway_id: 'gateway-B', workspace_id: 'workspace-B', gateway_connection_id: 'connection-B' }));
  const setValues = (fixture, values) => fixture.model.textEditorModel.pushEditOperations(null, [{ text: JSON.stringify({ config_version: 2,
    plugins: { configuration: { values } } }) }]);
  const setBinding = (fixture, entries) => { fixture.document.source_effective = { plugins: { runtime: { entries } } }; };
  try {
    setValues(a, { [packageId]: { [pluginId]: { mode: 'kept' } } });
    a.source.replaceSchemas({ plugins: [declaration] });
    const installedWarning = (await settingsHeader(a.source)).text;
    expect(installedWarning).toContain('参数未生效');
    expect(installedWarning).toContain('已安装但未绑定');
    expect(installedWarning).toContain('来源 JSONC 编辑或删除');
    expect(a.source.groups.filter(group => group.extensionInfo)).toEqual([]);

    setValues(b, { [packageId]: { [pluginId]: { mode: 'bound' } } });
    setBinding(b, [{ package_id: packageId, plugin_id: pluginId, package_version: version, enabled: false }]);
    b.source.replaceSchemas({ plugins: [declaration] });
    expect((await settingsHeader(b.source)).text).not.toContain('参数未生效');
    expect(b.source.groups.filter(group => group.extensionInfo)).toHaveLength(1);
    expect((await settingsHeader(a.source)).text).toContain('已安装但未绑定');

    setValues(a, { [packageId]: { [pluginId]: {} } });
    expect((await settingsHeader(a.source)).text).not.toContain('参数未生效');
    setValues(a, { [packageId]: { [pluginId]: { mode: 'kept' } } });
    a.source.replaceSchemas({ plugins: [] });
    expect((await settingsHeader(a.source)).text).toContain('schema.probe/preference 当前目标未见配置声明（可能已卸载），参数未生效');
    a.source.replaceSchemas(undefined);
    expect((await settingsHeader(a.source)).text).toContain('schema.probe/preference 未绑定，安装声明不可用，参数未生效');
    a.model.textEditorModel.pushEditOperations(null, [{ text: '{' }]);
    expect((await settingsHeader(a.source)).text).toContain('当前来源 JSONC 无效，无法判定未绑定参数');
  } finally { a.source.dispose(); b.source.dispose(); }
});

scenario('source Input 通过原文本模型报告 dirty、显式 save 与 revert', async () => {
  const target = location(), f = makeSource(target), targetSettings = f.source, configurationCalls = [];
  const { child } = childServices(targetSettings, configurationCalls);
  const input = new SettingsEditor2Input(target, f.document, { createSettings2EditorModel() { return { dispose() {} }; } });
  const lifetime = new DisposableStore(), dirtyEvents = [];
  input.onDidChangeDirty(() => dirtyEvents.push(input.isDirty()));
  prepareSettingsPaneImplementation = async () => child;
  try {
    const context = { target: undefined };
    const paneServices = { invokeFunction: fn => fn({ get: () => context }) };
    const parent = { createChild: services => { Object.assign(context, services.get(ITargetSettingsContext)); return paneServices; } };
    expect(await input.prepareEditorPane(parent, lifetime, CancellationToken.None)).toBe(paneServices);
    const model = await input.resolve(paneServices);
    expect(model.settingsGroups.flatMap(group => group.sections.flatMap(section => section.settings.map(setting => setting.key))))
      .toEqual(['logging.runtime.level', 'a%2Eb.c', 'a.b%2Ec']);
    const textModel = f.model.textEditorModel;
    targetSettings.edit('logging.runtime.level', 'debug');
    expect(textModel.getValue()).toContain('"level": "debug"');
    expect(f.model.isDirty()).toBe(true);
    expect(input.isDirty()).toBe(true);
    expect(f.model.saveCalls).toBe(0);
    expect(configurationCalls).toEqual([]);
    await input.selectSource(paneServices, undefined, undefined, CancellationToken.None);
    expect(context.target).toBeUndefined();
    expect(input.getName()).toBe(t('workbench.settings.editorName'));
    expect(input.getDescription()).toBeUndefined();
    expect(input.getTitle()).toBe(t('workbench.settings.editorName'));
    expect(input.location).toEqual(target);
    expect(input.isDirty()).toBe(true);
    await input.selectSource(paneServices, target, undefined, CancellationToken.None);
    expect(context.target).toBe(targetSettings);
    expect(await input.resolve(paneServices)).toBe(model);
    expect(textModel.getValue()).toContain('"level": "debug"');

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
    const dirtyEventCount = dirtyEvents.length;
    lifetime.dispose();
    expect(dirtyEvents).toHaveLength(dirtyEventCount);
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

scenario('管理入口默认本地且与导航活动工作区独立，失效会捕获离开再返回', async () => {
  const parsed = await source('src/managementTarget.ts', true);
  const auth = new Emitter(), context = new Emitter(), selections = new Map();
  let gatewayId = 'gateway-local';
  const snapshot = { get gatewayId() { return gatewayId; }, activeId: 'workspace-other' };
  const flow = evaluate(parsed, ['managementGateway', 'selectManagementTarget', 'createManagementTargetLifecycle'], {
    selections, workspaceContext: { getSnapshot: () => snapshot, subscribe: listener => {
      const subscription = context.event(listener); return () => subscription.dispose();
    } }, onGatewayTokenInvalidated: (_port, listener) => {
      const subscription = auth.event(listener); return () => subscription.dispose();
    }, CancellationTokenSource, t: key => key === 'workbench.managementTarget.local' ? '本地' : key,
  });
  const remote = { gateway_id: 'gateway-remote', name: '远端', connection_kind: 'remote_gateway', gateway_connection_id: 'route-remote' };
  try {
    expect(flow.managementGateway('settings')).toEqual({ gateway_id: 'gateway-local', name: '本地', connection_kind: 'local' });
    flow.selectManagementTarget('settings', remote);
    expect(flow.managementGateway('settings')).toEqual(remote);
    expect(snapshot.activeId).toBe('workspace-other');

    const lifecycle = flow.createManagementTargetLifecycle('目标已失效');
    gatewayId = 'gateway-temporary'; context.fire();
    gatewayId = 'gateway-local'; context.fire();
    expect(() => lifecycle.check()).toThrow('目标已失效');
    lifecycle.dispose();
  } finally { auth.dispose(); context.dispose(); }
});

scenario('各管理入口按Gateway与入口分别记住远端目标及工作区，不影响活动工作区', async () => {
  const parsed = await source('src/managementTarget.ts', true);
  const workspaces = [
    { gateway_id: 'entry-A', workspace_id: 'local-1', name: '本地一', status: 'ready' },
    { gateway_id: 'entry-A', workspace_id: 'local-2', name: '本地二', status: 'ready' },
    { gateway_id: 'gateway-B', workspace_id: 'remote-1', name: '远端一', status: 'ready' },
    { gateway_id: 'gateway-B', workspace_id: 'remote-2', name: '远端二', status: 'ready' },
  ];
  const snapshot = { gatewayId: 'entry-A', workspaces, activeId: 'navigation-workspace' };
  const flow = evaluate(parsed, ['selections', 'workspaceSelections', 'managementGateway', 'selectManagementTarget', 'managementWorkspace'], {
    workspaceContext: { getSnapshot: () => snapshot },
    panelManagementChanged: { fire() {} },
    t: key => key === 'workbench.managementTarget.local' ? '本地' : key,
  });
  const local = { gateway_id: 'entry-A', name: '本地', connection_kind: 'local' };
  const remote = { gateway_id: 'gateway-B', name: '远端 B', connection_kind: 'remote_gateway', gateway_connection_id: 'route-B' };
  expect(flow.managementWorkspace('settings', local).workspace_id).toBe('local-1');
  expect(flow.managementWorkspace('settings', local, 'local-2').workspace_id).toBe('local-2');
  expect(flow.managementWorkspace('settings', remote).workspace_id).toBe('remote-1');
  expect(flow.managementWorkspace('settings', remote, 'remote-2').workspace_id).toBe('remote-2');
  expect(flow.managementWorkspace('settings', local).workspace_id).toBe('local-2');
  expect(flow.managementWorkspace('settings', remote).workspace_id).toBe('remote-2');
  expect(flow.managementWorkspace('panel', local).workspace_id).toBe('local-1');
  expect(flow.managementGateway('panel')).toEqual(local);
  expect(flow.managementWorkspace('panel', local).workspace_id).toBe('local-1');
  expect(flow.managementWorkspace('panel', local, 'local-2').workspace_id).toBe('local-2');
  flow.selectManagementTarget('panel', remote);
  expect(flow.managementGateway('panel')).toEqual(remote);
  expect(flow.managementWorkspace('panel', remote).workspace_id).toBe('remote-1');
  expect(flow.managementWorkspace('panel', remote, 'remote-2').workspace_id).toBe('remote-2');
  expect(flow.managementWorkspace('panel', local).workspace_id).toBe('local-2');
  expect(flow.managementWorkspace('panel', remote).workspace_id).toBe('remote-2');
  flow.selectManagementTarget('settings', remote);
  expect(flow.managementGateway('settings')).toEqual(remote);
  expect(flow.managementGateway('extensions')).toEqual(local);
  expect(snapshot.activeId).toBe('navigation-workspace');
});

scenario('凭据失效保留各管理目标并取消在途选择，页面 Gateway 改变才清除选择', async () => {
  const parsed = await source('src/managementTarget.ts', true);
  const code = parsed.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed)).join('\n').replace(/^export /gm, '');
  const auth = new Emitter(), context = new Emitter(), actions = [], changes = [];
  let entry = 'gateway-a', delayedPick;
  const remote = { gateway_id: 'gateway-b', name: 'B', connection_kind: 'remote_gateway', gateway_connection_id: 'route-b' };
  const environment = {
    t, actionTitles: { 'workbench.managementTarget.selectExtensions': 'Select Extension Target', 'workbench.panel.selectTarget': 'Select Panel Target', 'workbench.managementTarget.selectSettings': 'Select Settings Target' },
    CancellationTokenSource, MutableDisposable, Emitter,
    Action2: class { constructor(description) { this.id = description.id; } },
    MenuId: {}, MenuRegistry: { appendMenuItem: () => toDisposable(() => {}) }, ContextKeyExpr: { equals: () => ({}) },
    registerAction2: Action => actions.push(new Action()), VIEWLET_ID: 'extensions', IQuickInputService: {},
    onGatewayTokenInvalidated: (_port, listener) => { const subscription = auth.event(listener); return () => subscription.dispose(); },
    listGatewayManagementTargets: async () => ({ local_gateway_id: entry, items: [remote] }),
    workspaceContext: { getSnapshot: () => ({ gatewayId: entry }), subscribe: listener => {
      const subscription = context.event(listener); return () => subscription.dispose();
    } },
  };
  const flow = new Function(...Object.keys(environment), ts.transpile(code, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None })
    + '\nreturn { managementGateway, onDidChangeExtensionManagementTarget, onDidChangePanelManagementTarget };')(...Object.values(environment));
  const subscriptions = [flow.onDidChangeExtensionManagementTarget(() => changes.push('extensions')),
    flow.onDidChangePanelManagementTarget(() => changes.push('panel'))];
  const quick = { pick: async (items, _options, token) => delayedPick ? delayedPick(items, token) : items[0] };
  const accessor = { get: () => quick };
  try {
    for (const action of actions) await action.run(accessor);
    for (const scope of ['extensions', 'settings', 'panel']) expect(flow.managementGateway(scope)).toEqual(remote);
    changes.length = 0;
    auth.fire();
    expect(changes).toEqual([]);
    for (const scope of ['extensions', 'settings', 'panel']) expect(flow.managementGateway(scope)).toEqual(remote);

    const entered = Promise.withResolvers(), picked = Promise.withResolvers();
    delayedPick = (items, token) => { entered.resolve({ item: items[0], token }); return picked.promise; };
    const pending = actions[0].run(accessor).then(() => undefined, error => error);
    const input = await entered.promise;
    auth.fire();
    expect(input.token.isCancellationRequested).toBe(true);
    picked.resolve(input.item);
    const error = await pending;
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toContain('页面 Gateway 或访问凭据已改变');
    expect(flow.managementGateway('extensions')).toEqual(remote);

    entry = 'gateway-c'; context.fire();
    expect(changes).toEqual(['extensions', 'panel']);
    for (const scope of ['extensions', 'settings', 'panel']) {
      expect(flow.managementGateway(scope)).toEqual({ gateway_id: entry, name: '本地', connection_kind: 'local' });
    }
  } finally { for (const subscription of subscriptions) subscription.dispose(); auth.dispose(); context.dispose(); }
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
    const context = { target: undefined }, paneChild = { invokeFunction: fn => fn({ get: () => context }) };
    const pending = input.prepareEditorPane({ createChild: () => paneChild }, lifetime, cancellation.token);
    await entered.promise;
    cancellation.cancel(); release.resolve();
    await expect(pending).rejects.toBeInstanceOf(CancellationError);
    expect(input.isDirty()).toBe(false);
    expect(context.target).toBeUndefined();
    await expect(input.resolve(paneChild)).rejects.toThrow('设置模型尚未绑定当前编辑器组');
    lifetime.dispose();
    expect(reference.disposed).toBe(true);
  } finally { release.resolve(); lifetime.dispose(); cancellation.dispose(); input.dispose(); prepareSettingsPaneImplementation = async () => { throw new Error('该测试尚未提供来源 Pane 准备结果'); }; }
});

scenario('业务设置沿原生按组视图缓存恢复实体选择，明确查询返回全部设置', () => {
  const pane = new SettingsEditor2(), cached = new Map(), source = { location: { workspace_id: 'workspace-A' } };
  let disposed = false;
  Object.assign(pane, {
    input: { resource: 'settings-A' }, group: { id: 1 }, targetSettingsContext: { target: source },
    inSettingsEditorContextKey: { set() {} }, cancelSettingsSearch() {},
    entityKind: 'providers', entityView: { selection: { definitionKind: 'providers', definitionId: 'provider-B' }, dispose() { disposed = true; } },
    entityContainer: { style: {} }, bodyContainer: { style: {} }, searchWidget: { getValue: () => '', setValue() {} },
    settingsTree: { getHTMLElement: () => pane.bodyContainer }, refreshTOCTree() {},
    settingsNavigation: { selected: 'providers', setCategory(category) { this.selected = category; } },
    settingsTargetsWidget: { settingsTarget: 4 }, viewState: { settingsTarget: 4 },
    editorMemento: { saveEditorState(group, input, state) { cached.set(`${group.id}:${input.resource}`, state); },
      loadEditorState(group, input) { return cached.get(`${group.id}:${input.resource}`); } },
    modelDisposables: { add: value => value },
    instantiationService: { createInstance: () => ({ selection: {}, show(kind, id) { this.selection = { definitionKind: kind, definitionId: id }; },
      setVisible(visible) { this.visible = visible; } }) },
  });
  pane.clearInput();
  expect(disposed).toBe(true);
  pane._setOptions({});
  expect(pane.getViewState()).toMatchObject({ definitionKind: 'providers', definitionId: 'provider-B' });
  expect(pane.entityContainer.style.display).toBe('block');
  pane._setOptions({ query: '', viewState: { definitionKind: 'agents' } });
  expect(pane.entityKind).toBeUndefined();
  expect(pane.entityView.visible).toBe(false);
  expect(pane.bodyContainer.style.display).toBe('');
  pane.group = { id: 2 };
  pane._setOptions({});
  expect(pane.entityKind).toBeUndefined();
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

scenario('没有工作区的 Gateway 仍注册和读取原生输出，旧工作区选择错误不被吞掉', async () => {
  const target = { gateway_id: 'gateway-remote', gateway_connection_id: 'ssh-remote', connection_kind: 'remote_gateway', name: '远端' };
  const reads = [], errors = [], registrations = [], writes = [];
  const log = { log_id: 'gateway:gateway', service: 'gateway', label: 'Gateway 日志', status: 'available', tail: 'gateway-only-log' };
  let snapshot = { gatewayId: 'entry', workspaces: [{ gateway_id: 'other-gateway', workspace_id: 'other-workspace' }] };
  let selectedWorkspaceReads = 0;
  const { WorkspaceOutput } = evaluate(await source('src/workspaceOutput.ts', true), ['WorkspaceOutput'], {
    Disposable, OUTPUT_VIEW_ID: 'output', VSBuffer, joinPath, OutputChannelUpdateMode: { Replace: 1 },
    workspaceContext: { getSnapshot: () => snapshot }, managementGateway: () => target,
    managementWorkspace: () => { selectedWorkspaceReads++; throw new Error('已选工作区不存在'); },
    getGatewayDiagnostics: async (_port, options) => { reads.push(options.target); return { logs: [log], selected_log_id: log.log_id }; },
    watchPluginRuntimeStatus: async () => {},
  }, ['updateScope', 'clearScope', 'readSelected', 'logText']);
  const view = new WorkspaceOutput();
  let selected;
  Object.assign(view, { sources: new Map(), workspaceScopedContext: { set() {}, reset() {} }, refresh: { cancel() {}, schedule() {} },
    views: { isViewVisible: () => true }, notifications: { error: error => errors.push(error) },
    registry: { registerChannel: channel => registrations.push(channel), removeChannel() {} },
    environment: { windowLogsPath: URI.parse('vscode-log:/window') },
    files: { writeFile: async (_resource, value) => writes.push(value.toString()) },
    output: { getActiveChannel: () => selected ? { id: selected } : undefined, showChannel: async id => { selected = id; }, getChannel: () => ({ update() {} }) },
  });
  try {
    view.updateScope();
    await view.loading;
    await view.readSelected();
    expect(selectedWorkspaceReads).toBe(0);
    expect(reads).toHaveLength(2);
    for (const read of reads) expect(read).toEqual({ gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id, workspace_id: undefined });
    expect(registrations.map(channel => channel.label)).toEqual(['Gateway 日志']);
    expect(writes.at(-1)).toBe('gateway-only-log\n');
    expect(errors).toEqual([]);
    snapshot = { ...snapshot, workspaces: [{ gateway_id: target.gateway_id, workspace_id: 'another-workspace' }] };
    view.updateScope(true);
    expect(selectedWorkspaceReads).toBe(1);
    expect(errors.map(error => error.message)).toEqual(['已选工作区不存在']);
    expect(reads).toHaveLength(2);
  } finally { view.clearScope(); view.dispose(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-fingerprints.json'), `${JSON.stringify({ pinnedUpstreamCommit, fingerprints, registered, executed, passed }, null, 2)}\n`);
  await run.writeResult(!executed.length ? 'skipped' : executed.length === passed.length ? 'passed' : 'failed', {
    boundary: 'Integration 源码合同：执行固定上游 sourceTransform 后 AST 调用真实 SettingsEditor2Input、serializer、SourceSettings、SourceDefaults、Settings2EditorModel 及 SettingsEditor2 的 dirty/save/revert、pending writer 和 AI 搜索取消方法。文件文本模型、Pane DI/owner 及 AI 搜索 provider 是具名边界；不经过真实 Gateway、配置 HTTP 或浏览器。',
    registered, executed, passed, filtered: registered.filter(name => !executed.includes(name)),
  });
  await run.close();
});

test('设置目录晚到与同分类范围刷新不能覆盖用户已选管理页，显式命令仍可切换', async () => {
  const parsed = await source('src/settings/management.tsx', true);
  const { SettingsManagementView } = evaluate(parsed, ['SettingsManagementView'], {
    Disposable, workspaceContext: { getSnapshot: () => ({ gatewayId: 'local' }) },
    pageCategory: { connections: 'connections', credentials: 'connections', runtime: 'runtime', diagnostics: 'observability', theme: 'client' },
  }, ['show']);
  const view = new SettingsManagementView();
  const bound = { location: { gateway_id: 'local' } };
  const pages = [];
  Object.assign(view, { category: 'client', request: {}, updateActions() {}, render() { pages.push(this.page); } });
  const pane = new SettingsEditor2();
  Object.assign(pane, { managementView: view, targetSettingsContext: { target: bound }, viewState: {},
    settingsNavigation: { setCategory() {}, selectedNode: undefined }, refreshTOCTree() {},
    settingsTree: { getHTMLElement: () => ({ style: {} }) }, entityContainer: { style: {} }, searchWidget: { getValue: () => '' } });
  pane.showCategory('connections', undefined, { managementPage: 'connections' });
  view.page = 'credentials'; // 用户已通过工具栏切页，随后目录初始化回调重绘同分类。
  view.dialog = 'external-device';
  pane.showCategory('connections');
  expect(pages.at(-1)).toBe('credentials');
  expect(view.dialog).toBe('external-device');
  view.show('connections', bound, { managementPage: 'connections' });
  expect(view.dialog).toBeUndefined();
  expect(pages.at(-1)).toBe('connections');
  view.show('observability', bound, { managementPage: 'diagnostics', diagnosticLocation: { reference: { log_id: 'original' } } });
  view.show('observability', bound);
  expect(view.request.diagnosticLocation.reference.log_id).toBe('original');
  view.show('observability', { location: { gateway_id: 'remote' } });
  expect(pages.at(-1)).toBe('diagnostics');
  expect(view.request.diagnosticLocation).toBeUndefined();
  view.show('observability', bound, {}, true);
  expect(pages.at(-1)).toBeUndefined();
});
