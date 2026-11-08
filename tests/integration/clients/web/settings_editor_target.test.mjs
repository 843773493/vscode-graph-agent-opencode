import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Delayer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/async.js';
import { CancellationToken } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CancellationError, getErrorMessage, isCancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { Emitter, Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore, MutableDisposable, dispose, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Schemas } from '../../../../src/clients/workbench/upstream/src/vs/base/common/network.js';
import * as platform from '../../../../src/clients/workbench/upstream/src/vs/base/common/platform.js';
import { isEqual } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { isFalsyOrWhitespace } from '../../../../src/clients/workbench/upstream/src/vs/base/common/strings.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { ConfigurationTarget, IConfigurationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configuration.js';
import { ConfigurationScope, Extensions } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import { IFileService } from '../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { ILogService } from '../../../../src/clients/workbench/upstream/src/vs/platform/log/common/log.js';
import { IContextKeyService } from '../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/common/contextkey.js';
import { ConfirmResult } from '../../../../src/clients/workbench/upstream/src/vs/platform/dialogs/common/dialogs.js';
import { createDecorator, IInstantiationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiation.js';
import { InstantiationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiationService.js';
import { ServiceCollection } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { Registry } from '../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { IWorkspaceContextService } from '../../../../src/clients/workbench/upstream/src/vs/platform/workspace/common/workspace.js';
import { EditorModel } from '../../../../src/clients/workbench/upstream/src/vs/workbench/common/editor/editorModel.js';
import { IPreferencesService, SettingMatchType, SettingValueType, validateSettingsEditorOptions } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { ITextEditorService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/textfile/common/textEditorService.js';
import { ITextFileService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import { IFilesConfigurationService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/filesConfiguration/common/filesConfigurationService.js';
import { ITargetSettingsContext } from '../../../../src/clients/workbench/src/settings/scope.js';
import { IEditorService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import * as preferences from '../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/preferences/common/preferences.js';
import { pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_editor_target.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = join(root, 'upstream/src/vs');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const sources = new Map(), fingerprints = [], registered = [], executed = [], passed = [];
const testSource = await readFile(import.meta.path, 'utf8');
await writeFile(join(run.artifactsDir, 'test-source.mjs'), testSource);
fingerprints.push({ path: 'tests/integration/clients/web/settings_editor_target.test.mjs',
  original: createHash('sha256').update(testSource).digest('hex') });
const selectedEditorMembers = ['pendingSettingUpdates', 'pendingSettingWrite', 'settingsWriterInput', 'settingsTargetSelection', 'switchSettingsGateway', 'shouldSettingUpdateFast',
  'setInput', '_setOptions', 'clearInput', 'restoreCachedState', 'onConfigUpdate', 'onDidSettingsTargetChange', 'onDidChangeSetting', 'notifySettingsDirty',
  'discardSettingsUpdates', 'flushSettingsUpdates', 'updateChangedSetting'];

async function source(relative, local = false) {
  const key = `${local ? 'local:' : ''}${relative}`;
  if (!sources.has(key)) {
    const path = join(local ? root : upstream, relative), original = await readFile(path, 'utf8');
    const converted = local ? original : await transform(path, original);
    const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
    if (parsed.parseDiagnostics.length) throw new Error(`转换源码不能解析：${relative}`);
    fingerprints.push({ path: key, original: createHash('sha256').update(original).digest('hex'),
      converted: createHash('sha256').update(converted).digest('hex') });
    await writeFile(join(run.artifactsDir, key.replaceAll('/', '__').replace(':', '__')), converted);
    sources.set(key, parsed);
  }
  return sources.get(key);
}

function evaluate(parsed, names, environment, classMembers) {
  const declarations = names.map(name => {
    let node = parsed.statements.find(node => node.name?.text === name || ts.isVariableStatement(node)
      && node.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name));
    if (!node) throw new Error(`实际源码缺少声明：${parsed.fileName}#${name}`);
    if (classMembers) {
      const members = node.members.filter(member => classMembers.includes(member.name?.getText(parsed)));
      for (const member of classMembers) {
        if (!members.some(node => node.name?.getText(parsed) === member)) throw new Error(`实际类缺少成员：${name}.${member}`);
      }
      node = ts.factory.updateClassDeclaration(node, node.modifiers, node.name, node.typeParameters, node.heritageClauses, members);
    }
    return ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  });
  // 保留装饰器，使原 Input 和原 Settings2EditorModel 经真实 InstantiationService 构造。
  const javascript = ts.transpile(declarations.join('\n'), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None,
    useDefineForClassFields: false, experimentalDecorators: true });
  return new Function(...Object.keys(environment), `${javascript}\nreturn { ${names.join(', ')} };`)(...Object.values(environment));
}

const commonParsed = await source('workbench/common/editor.ts');
const common = evaluate(commonParsed, ['EditorInputCapabilities', 'Verbosity', 'AbstractEditorInput', 'isEditorInput'], { Disposable });
const { EditorInput } = evaluate(await source('workbench/common/editor/editorInput.ts'), ['EditorInput'], {
  ...common, Emitter, DisposableStore, URI, isEqual,
});
const modelsParsed = await source('workbench/services/preferences/common/preferencesModels.ts');
const { AbstractSettingsModel } = evaluate(modelsParsed, ['AbstractSettingsModel'], { EditorModel, SettingMatchType });
const { Settings2EditorModel, SettingsEditorModel } = evaluate(modelsParsed, ['Settings2EditorModel', 'SettingsEditorModel'], {
  AbstractSettingsModel, Emitter, IConfigurationService, ConfigurationTarget, Registry, Extensions,
});
const { PreferencesService } = evaluate(await source('workbench/services/preferences/browser/preferencesService.ts'), ['PreferencesService'], {
  Disposable, Settings2EditorModel, ConfigurationTarget,
}, ['createSettings2EditorModel']);

const ScopeBoundary = createDecorator('settingsEditorTargetIntegrationBoundary');
const GroupIdentity = createDecorator('settingsEditorTargetIntegrationGroup');
const helperParsed = await source('src/settings/editor.ts', true);
const { settingsEditorIdentity, parseSettingsLocation, settingsHistoryLocation } = evaluate(helperParsed,
  ['settingsEditorIdentity', 'parseSettingsLocation', 'settingsHistoryLocation'], {});
const { prepareSettingsPane } = evaluate(helperParsed, ['prepareSettingsPane'], {
  CancellationError,
  // 只替代配置 HTTP 准备及目标服务装配；原 prepare/取消流程和 Input 模型绑定仍实际执行。
  prepareSettingsContext: async (location, signal) => {
    signal.throwIfAborted();
    return { integrationTransport: 'prepared', backend: location.backend_id };
  },
  settingsEditorServices: (parent, location, context, lifetime) => parent.invokeFunction(accessor =>
    accessor.get(ScopeBoundary).prepare(parent, location, context, lifetime)),
});
const { SettingsEditor2Input } = evaluate(await source('workbench/services/preferences/common/preferencesEditorInput.ts'), ['SettingsEditor2Input'], {
  EditorInput, Emitter, DisposableStore, URI, Schemas, isEqual, IPreferencesService,
  parseSettingsLocation, settingsEditorIdentity, prepareSettingsPane, toDisposable, CancellationError,
  SettingsEditorIcon: { id: 'settings-integration-icon' }, nls: { localize: (_key, text) => text },
});
const { FileEditorInput } = evaluate(await source('workbench/contrib/files/browser/editors/fileEditorInput.ts'), ['FileEditorInput'], {
  AbstractTextResourceEditorInput: EditorInput, parseSettingsLocation, settingsEditorIdentity, prepareSettingsPane, CancellationError,
}, ['settingsTarget', 'verifiedSettingsContext', 'settingsLocation', 'editorPaneScope', 'bindSettingsTarget', 'prepareEditorPane']);
const { SideBySideEditorInput } = evaluate(await source('workbench/common/editor/sideBySideEditorInput.ts'), ['SideBySideEditorInput'], {
  EditorInput, Event, IEditorService,
});
const { SettingsEditor2InputSerializer } = evaluate(await source('workbench/contrib/preferences/browser/preferences.contribution.ts'),
  ['SettingsEditor2InputSerializer'], { SettingsEditor2Input, parseSettingsLocation, settingsHistoryLocation });
const { FileEditorInputSerializer } = evaluate(await source('workbench/contrib/files/browser/editors/fileEditorHandler.ts'),
  ['FileEditorInputSerializer'], { URI, isEqual, ITextEditorService, settingsHistoryLocation });

class RendererBoundary extends Disposable {
  constructor(editor, model) { super(); this.model = model; this.renderCount = 0; editor.integrationRenderers.push(this); }
  render() { this.renderCount++; }
}
class UserRendererBoundary extends RendererBoundary { kind = 'user'; }
class WorkspaceRendererBoundary extends RendererBoundary { kind = 'workspace'; }
const { SettingsEditorContribution } = evaluate(await source('workbench/contrib/preferences/browser/preferences.contribution.ts'),
  ['SettingsEditorContribution'], { Disposable, DisposableStore, IInstantiationService, IPreferencesService,
    IWorkspaceContextService, IContextKeyService, SettingsEditorModel, ConfigurationTarget,
    CONTEXT_SETTINGS_JSON_EDITOR: preferences.CONTEXT_SETTINGS_JSON_EDITOR,
    UserSettingsRenderer: UserRendererBoundary, WorkspaceSettingsRenderer: WorkspaceRendererBoundary,
  });

class CompositeBoundary extends Disposable {}
const { EditorPane } = evaluate(await source('workbench/browser/parts/editor/editorPane.ts'), ['EditorPane'], {
  Composite: CompositeBoundary, Emitter, Event,
  DEFAULT_EDITOR_MIN_DIMENSIONS: { width: 1, height: 1 }, DEFAULT_EDITOR_MAX_DIMENSIONS: { width: Infinity, height: Infinity },
});
const queryParsed = await source('workbench/contrib/preferences/browser/settingsTreeModels.ts');
const { parseQuery } = evaluate(queryParsed, ['tagRegex', 'extensionRegex', 'featureRegex', 'idRegex', 'languageRegex', 'parseQuery'], {
  ...preferences, isFalsyOrWhitespace,
});
const switching = {};
const { SettingsContextHeader } = evaluate(await source('src/settings/header.ts', true), ['SettingsContextHeader'], {
  Disposable, URI, isEqual, getErrorMessage, ConfigurationTarget,
  ITargetSettingsContext, IPreferencesService, IWorkspaceContextService, IFileService, IFilesConfigurationService, ITextFileService, ILogService,
});
const { SettingsEditor2 } = evaluate(await source('workbench/contrib/preferences/browser/settingsEditor2.ts'), ['SettingsEditor2'], {
  EditorPane, SettingsEditor2Input, Settings2EditorModel, SettingValueType, ConfigurationTarget,
  URI, parseQuery, isCancellationError, MODIFIED_SETTING_TAG: preferences.MODIFIED_SETTING_TAG,
  validateSettingsEditorOptions, platform, DisposableStore, CancellationToken, SideBySideEditorInput, FileEditorInput,
  settingsEditorIdentity, selectSettingsManagementTargetCommand: 'integration.selectSettingsTarget',
  ConfirmResult,
  captureSettingsLocation: (...args) => switching.capture(...args), prepareSettingsContext: (...args) => switching.prepare(...args),
  rebindSettingsFiles: (...args) => switching.rebind(...args),
}, selectedEditorMembers);
await Promise.all(['sourceTransform.mjs', 'settingsEditorTransform.mjs', 'settingsEditorScopeTransform.mjs', 'settingsTreeScopeTransform.mjs']
  .map(name => source(`scripts/${name}`, true)));

function location(overrides = {}) {
  return { version: 1, gateway_id: 'gateway-A', backend_id: 'backend-A', profile_id: 'profile-A', view_id: 'view-A',
    workspace_ids: ['workspace-A', 'workspace-B'], name: 'Target A', gateway_connection_id: 'route-A', ...overrides };
}

function headerFixture({ allowed = true, multiRoot = true, window = false, resolveResource } = {}) {
  const lifetime = new DisposableStore(), calls = [], logs = [], models = new Map(), rendered = [];
  const events = Object.fromEntries(['readonly', 'provider', 'registration', 'resolve', 'modelReadonly', 'remove', 'operation', 'save', 'files']
    .map(name => [name, lifetime.add(new Emitter())]));
  const profile = URI.parse('boxteam://gateway-A/config/profile'), workspaceFile = URI.parse('boxteam://gateway-A/config/view');
  const local = URI.parse('vscode-userdata:/User/settings.json'), folder = URI.parse('boxteam-file://gateway-A/workspace-A/root');
  const resources = { [ConfigurationTarget.USER_REMOTE]: profile, [ConfigurationTarget.USER_LOCAL]: local,
    [ConfigurationTarget.APPLICATION]: local, [ConfigurationTarget.WORKSPACE]: multiRoot ? workspaceFile : folder.with({ path: folder.path + '/.vscode/settings.json' }),
    [ConfigurationTarget.WORKSPACE_FOLDER]: folder.with({ path: folder.path + '/.vscode/settings.json' }) };
  const target = window ? undefined : { location: location({ workspace_ids: ['workspace-Z', 'workspace-A'] }),
    configuration: { kind: 'settings', connection: { configuration_write_allowed: allowed } } };
  const state = { readonly: false, workspace: { name: 'Frozen view', configuration: multiRoot ? workspaceFile : undefined } };
  const element = { style: {}, textContent: '', title: '', offsetHeight: 20 };
  const services = new ServiceCollection(
    [ITargetSettingsContext, { target }],
    [IPreferencesService, { getEditableSettingsURI(scope, resource) { calls.push({ scope, resource }); return resolveResource
      ? resolveResource(scope, resource) : Promise.resolve(resources[scope]); } }],
    [IWorkspaceContextService, { getWorkspace: () => state.workspace, getWorkspaceFolder: () => ({ name: 'Frozen folder' }) }],
    [IFileService, { onDidChangeFileSystemProviderCapabilities: events.provider.event,
      onDidChangeFileSystemProviderRegistrations: events.registration.event, onDidRunOperation: events.operation.event, onDidFilesChange: events.files.event }],
    [IFilesConfigurationService, { isReadonly: (_resource, stat) => state.readonly || !!stat?.readonly, onDidChangeReadonly: events.readonly.event }],
    [ITextFileService, { files: { get: resource => models.get(resource.toString()), onDidResolve: events.resolve.event,
      onDidChangeReadonly: events.modelReadonly.event, onDidRemove: events.remove.event, onDidSave: events.save.event } }],
    [ILogService, { error: error => logs.push(error) }]);
  const di = lifetime.add(new InstantiationService(services));
  const header = lifetime.add(di.createInstance(SettingsContextHeader, element, () => rendered.push(element.textContent)));
  return { header, element, target, state, events, profile, local, folder, calls, logs, models, rendered, close: () => lifetime.dispose() };
}

function fixture() {
  const owned = [], preparations = [];
  const own = value => { owned.push(value); return value; };
  function configuration(owner, lifetime) {
    const changed = lifetime.add(new Emitter()), calls = [], inspected = [];
    const boundary = { owner, calls, inspected, changed, write: async () => {}, defaults: new Map(), service: {
      onDidChangeConfiguration: changed.event,
      inspect(key, overrides) { inspected.push({ key, overrides }); return { defaultValue: boundary.defaults.get(key) }; },
      updateValue(key, value, overrides, target, options) {
        const call = { owner, key, value, overrides, target, options }; calls.push(call);
        return boundary.write(call);
      },
    } };
    return boundary;
  }
  function preferencesService(di, owner, lifetime) {
    const service = lifetime.add(new PreferencesService()), defaultTargets = [];
    service.instantiationService = di;
    // 默认设置目录是具名外部数据边界，返回的编辑器模型由原 PreferencesService 方法与真实 DI 创建。
    service.getDefaultSettings = target => {
      defaultTargets.push(target);
      return { getSettingsGroups: () => [{ id: owner, sections: [{ settings: [{ key: `${owner}.setting` }] }] }] };
    };
    return { service, defaultTargets };
  }
  const parentLifetime = own(new DisposableStore()), defaultConfiguration = configuration('window', parentLifetime);
  const services = new ServiceCollection([IConfigurationService, defaultConfiguration.service]);
  const parent = own(new InstantiationService(services));
  const windowPreferences = preferencesService(parent, 'window', parentLifetime);
  services.set(IPreferencesService, windowPreferences.service);
  services.set(ScopeBoundary, { prepare(parent, target, context, lifetime) {
    const group = parent.invokeFunction(accessor => accessor.get(GroupIdentity));
    const config = configuration(`${target.backend_id}/${group}`, lifetime), childServices = new ServiceCollection([IConfigurationService, config.service]);
    const child = parent.createChild(childServices, lifetime), prefs = preferencesService(child, config.owner, lifetime);
    childServices.set(IPreferencesService, prefs.service);
    preparations.push({ group, location: target, context, di: child, configuration: config, preferences: prefs });
    return child;
  } });
  return { parent, preparations, defaultConfiguration, windowPreferences,
    input(target, context) { return own(parent.createInstance(SettingsEditor2Input, arguments.length ? target : location(), context)); },
    async group(input, id) {
      const lifetime = own(new DisposableStore()), groupParent = parent.createChild(new ServiceCollection([GroupIdentity, id]), lifetime);
      const di = await input.prepareEditorPane(groupParent, lifetime, CancellationToken.None);
      const prepared = preparations.find(value => value.di === di);
      if (!prepared) throw new Error('目标设置输入没有取得该组的配置服务');
      return { id, di, lifetime, configuration: prepared.configuration };
    },
    async editor(input, id) {
      const group = await this.group(input, id), pane = new SettingsEditor2('settings-integration', { id }, {}, {}, {});
      own(pane);
      const logs = [], rendered = [], reports = [], refreshes = [];
      pane.instantiationService = group.di; pane.configurationService = group.configuration.service;
      pane.modelDisposables = pane._register(new DisposableStore());
      pane.inputChangeListener = pane._register(new MutableDisposable());
      pane.settingFastUpdateDelayer = pane._register(new Delayer(60_000));
      pane.settingSlowUpdateDelayer = pane._register(new Delayer(60_000));
      pane.updatedConfigSchemaDelayer = pane._register(new Delayer(60_000));
      let query = '';
      pane.searchWidget = { getValue: () => query, setValue: value => { query = value; } };
      pane.settingsTargetsWidget = { settingsTarget: ConfigurationTarget.USER_REMOTE,
        updateTarget(target) { this.settingsTarget = target; } };
      pane.viewState = { settingsTarget: ConfigurationTarget.USER_REMOTE };
      pane.inSettingsEditorContextKey = { set() {} };
      pane.logService = { error: error => logs.push(error) };
      pane.fileDialogService = { showSaveConfirm: async () => ConfirmResult.SAVE };
      pane.onConfigUpdate = (...args) => { refreshes.push(args); return Promise.resolve(); };
      pane.updateTreeScrollSync = () => {};
      pane.refreshInstalledExtensionsList = async () => {};
      pane.renderTree = (...args) => rendered.push(args);
      pane.refreshTOCTree = () => {};
      pane.reportModifiedSetting = value => { reports.push(value); };
      await pane.setInput(input, {}, {}, CancellationToken.None);
      return { pane, group, logs, rendered, reports, refreshes,
        change(key, value, type = SettingValueType.String, manualReset = false, scope = ConfigurationScope.RESOURCE) {
          pane.onDidChangeSetting(key, value, type, manualReset, scope);
        } };
    },
    close() { dispose(owned.reverse()); },
  };
}

function jsonRendererFixture() {
  const lifetime = new DisposableStore(), requests = [], renderers = [];
  const text = { uri: URI.parse('boxteam://gateway-A/profile/settings'), onDidChangeContent: Event.None, getValue: () => '{}' };
  let currentModel, contextKey;
  const editor = { getModel: () => currentModel, onDidChangeModel: Event.None, integrationRenderers: renderers };
  const contextKeys = { createKey(_key, defaultValue) {
    contextKey = { value: defaultValue, set(value) { this.value = value; }, reset() { this.value = defaultValue; }, get() { return this.value; } };
    return contextKey;
  } };
  const di = lifetime.add(new InstantiationService(new ServiceCollection(
    [IContextKeyService, contextKeys], [IWorkspaceContextService, { onDidChangeWorkbenchState: Event.None }],
    [IPreferencesService, { createPreferencesEditorModel(uri) {
      const request = { uri, ...Promise.withResolvers() }; requests.push(request); return request.promise;
    } }],
  )));
  const contribution = lifetime.add(di.createInstance(SettingsEditorContribution, editor));
  return { contribution, requests, renderers, text, context: () => contextKey.get(),
    showModel() { currentModel = text; },
    model(target) {
      const reference = { disposed: 0, object: { textEditorModel: text }, dispose() { this.disposed++; } };
      const model = lifetime.add(new SettingsEditorModel(reference, target));
      return { model, reference };
    },
    close() { lifetime.dispose(); },
  };
}

function scenario(name, callback) {
  registered.push(name);
  test(name, async () => { executed.push(name); await callback(); passed.push(name); });
}

scenario('原配置更新和历史恢复入口展示冻结上下文及准备时授权，不依赖目标变化事件', async () => {
  const f = headerFixture({ allowed: false });
  try {
    const pane = { settingsContextHeader: f.header, settingsTargetsWidget: { settingsTarget: ConfigurationTarget.USER_REMOTE }, viewState: {} };
    await SettingsEditor2.prototype.onConfigUpdate.call(pane);
    expect(f.element.textContent).toContain('范围：目标用户 · 写入授权：未授权（打开时）');
    expect(f.element.textContent).toContain('打开上下文：2 个工作区');
    expect(f.element.title).toContain('Gateway gateway-A\nBackend backend-A\nprofile profile-A\nview view-A\n工作区（打开时的顺序）：\n1. workspace-Z\n2. workspace-A');
    expect(f.element.title).toContain(f.profile.toString());
    const input = {}, cleared = [];
    Object.assign(pane, { input, group: {}, searchWidget: { getValue: () => '', setValue() {} },
      editorMemento: { loadEditorState: () => ({ target: ConfigurationTarget.USER_LOCAL }), clearEditorState: owner => cleared.push(owner) } });
    SettingsEditor2.prototype.restoreCachedState.call(pane);
    await Promise.resolve();
    expect(cleared).toEqual([input]);
    expect(f.element.textContent).toContain('范围：当前工作台用户 · 写入授权：当前工作台管理');
    expect(f.element.title).toContain(f.local.toString());
    expect(f.element.title).toContain('1. workspace-Z\n2. workspace-A');
  } finally { f.close(); }
});

scenario('多根 view 使用配置授权，folder及单根workspace明确沿原文件服务且不借用授权快照', async () => {
  const f = headerFixture(), single = headerFixture({ multiRoot: false, allowed: false });
  try {
    const pane = { settingsContextHeader: f.header, settingsTargetsWidget: {}, viewState: {} };
    pane.onConfigUpdate = () => {
      pane.settingsTargetsWidget.settingsTarget = pane.viewState.settingsTarget;
      return SettingsEditor2.prototype.onConfigUpdate.call(pane);
    };
    SettingsEditor2.prototype.onDidSettingsTargetChange.call(pane, ConfigurationTarget.WORKSPACE);
    await Promise.resolve();
    expect(f.element.textContent).toContain('范围：工作区 · Frozen view · 写入授权：已授权（打开时）');
    SettingsEditor2.prototype.onDidSettingsTargetChange.call(pane, f.folder);
    await Promise.resolve();
    expect(f.element.textContent).toContain('范围：文件夹 · Frozen folder · 写入授权：保存时检查');
    expect(f.element.textContent).not.toContain('已授权');
    single.header.update(ConfigurationTarget.WORKSPACE);
    await Promise.resolve();
    expect(single.element.textContent).toContain('写入授权：保存时检查');
    expect(single.element.textContent).not.toContain('未授权');
  } finally { f.close(); single.close(); }
});

scenario('配置变化与语言切换只重绘原状态，不重新prepare、stat或解析同一来源', async () => {
  const f = headerFixture();
  try {
    const pane = { settingsContextHeader: f.header, settingsTargetsWidget: { settingsTarget: ConfigurationTarget.USER_REMOTE }, viewState: {} };
    await SettingsEditor2.prototype.onConfigUpdate.call(pane);
    for (let index = 0; index < 10; index++) await SettingsEditor2.prototype.onConfigUpdate.call(pane, new Set(['editor.tabSize']));
    pane.viewState.languageFilter = 'typescript';
    await SettingsEditor2.prototype.onConfigUpdate.call(pane);
    expect(f.calls).toHaveLength(1);
    expect(f.element.textContent).toContain('范围：目标用户 · 语言 typescript');
    expect(f.element.textContent).toContain('文件：保存时检查');
    expect(f.element.textContent).not.toContain('可写');
  } finally { f.close(); }
});

scenario('原服务已取得的只读、锁定及外部失效更新同一展示，不解析或保存文件', async () => {
  const f = headerFixture({ window: true });
  try {
    f.header.update(ConfigurationTarget.USER_LOCAL);
    await Promise.resolve();
    expect(f.element.textContent).toContain('打开上下文：当前工作台');
    f.state.readonly = { value: '原文件服务的只读原因' }; f.events.readonly.fire();
    expect(f.element.textContent).toContain('文件：只读'); expect(f.element.title).toContain('原文件服务的只读原因');
    f.state.readonly = false;
    f.models.set(f.local.toString(), { resource: f.local, isReadonly: () => true });
    f.events.resolve.fire({ model: f.models.get(f.local.toString()) });
    expect(f.element.textContent).toContain('文件：只读');
    f.models.clear(); f.events.remove.fire(f.local);
    f.events.operation.fire({ target: { resource: f.local, readonly: false, locked: true } });
    expect(f.element.textContent).toContain('文件：锁定');
    f.events.save.fire({ model: { resource: f.local }, stat: { resource: f.local, readonly: true, locked: false } });
    expect(f.element.textContent).toContain('文件：只读');
    f.events.files.fire({ contains: resource => isEqual(resource, f.local) });
    expect(f.element.textContent).toContain('文件：保存时检查');
    expect(f.calls).toHaveLength(1);
  } finally { f.close(); }
});

scenario('来源异步解析乱序或关闭后不覆盖新来源，当前失败保留原错误与上下文', async () => {
  const gates = [], f = headerFixture({ resolveResource: () => { const gate = Promise.withResolvers(); gates.push(gate); return gate.promise; } });
  try {
    f.header.update(ConfigurationTarget.USER_REMOTE);
    f.header.update(ConfigurationTarget.USER_LOCAL);
    gates[1].resolve(f.local); await Promise.resolve();
    const selected = f.element.title;
    gates[0].resolve(f.profile); await Promise.resolve();
    expect(f.element.title).toBe(selected);
    f.header.update(ConfigurationTarget.WORKSPACE);
    const error = new Error('原目标来源不可访问'); gates[2].reject(error); await Promise.resolve();
    expect(f.element.textContent).toContain('不可用：原目标来源不可访问');
    expect(f.element.textContent).toContain('打开上下文：2 个工作区'); expect(f.logs).toEqual([error]);
    f.header.update(ConfigurationTarget.USER_REMOTE);
    f.close(); const rendered = f.rendered.length;
    gates[3].resolve(f.profile); await Promise.resolve();
    expect(f.rendered).toHaveLength(rendered);
  } finally { f.close(); }
});

scenario('原 Settings 输入按稳定 owner/profile/有序 view 匹配，名称与连接路由不参与身份', () => {
  const f = fixture();
  try {
    const original = location(), input = f.input(original);
    original.workspace_ids.push('late'); original.backend_id = 'late';
    expect(input.location).toEqual(location());
    expect(input.matches(f.input(location({ name: 'Renamed', gateway_connection_id: 'other-route' })))).toBe(true);
    for (const difference of [{ gateway_id: 'gateway-B' }, { backend_id: 'backend-B' }, { profile_id: 'profile-B' },
      { view_id: 'view-B' }, { workspace_ids: ['workspace-B', 'workspace-A'] }, { workspace_ids: ['workspace-A'] }]) {
      expect(input.matches(f.input(location(difference)))).toBe(false);
    }
    expect(input.editorPaneScope).toBe(input);
    expect(input.getName()).toBe('设置 · Target A'); expect(input.getDescription()).toBe('gateway-A · backend-A');
  } finally { f.close(); }
});

scenario('原 serializer 仅恢复明确 location，拒绝损坏历史并保留默认窗口输入', async () => {
  const f = fixture();
  try {
    const serializer = new SettingsEditor2InputSerializer(), input = f.input(location(), { transient: 'must-not-persist' });
    const serialized = serializer.serialize(input), restored = serializer.deserialize(f.parent, serialized);
    const { gateway_connection_id: _route, ...stable } = location();
    expect(JSON.parse(serialized)).toEqual(stable); expect(serialized.includes('must-not-persist')).toBe(false);
    expect(input.location.gateway_connection_id).toBe('route-A'); expect(restored.location).toEqual(stable);
    expect(restored.matches(input)).toBe(true); expect(restored.initialContext).toBeUndefined();
    const previousHistory = serializer.deserialize(f.parent, JSON.stringify(location({ gateway_connection_id: 'removed-route' })));
    expect(previousHistory.location).toEqual(stable); expect(previousHistory.matches(input)).toBe(true);
    previousHistory.dispose();
    expect(() => serializer.deserialize(f.parent, '{')).toThrow();
    expect(() => serializer.deserialize(f.parent, JSON.stringify({ version: 1 }))).toThrow('设置历史目标格式无效');
    const plain = serializer.deserialize(f.parent, '');
    expect(plain.location).toBeUndefined(); expect(plain.editorPaneScope).toBeUndefined(); expect(serializer.serialize(plain)).toBe('');
    expect(await plain.resolve()).toBeInstanceOf(Settings2EditorModel);
    restored.dispose(); plain.dispose();
  } finally { f.close(); }
});

scenario('原文件 serializer 保留规范资源与编辑选项，设置 JSON 历史剥离连接而活跃绑定不变', () => {
  const serializer = new FileEditorInputSerializer(), resource = URI.parse('boxteam://gateway-A/profile/settings');
  const active = location(), bindings = [], requests = [];
  const file = { resource, preferredResource: resource, settingsLocation: active,
    getPreferredName: () => '目标 JSON', getPreferredDescription: () => '原描述', getEncoding: () => 'utf8', getPreferredLanguageId: () => 'jsonc' };
  const serialized = JSON.parse(serializer.serialize(file));
  const { gateway_connection_id: _route, ...stable } = active;
  expect(serialized.settingsTarget).toEqual(stable); expect(active.gateway_connection_id).toBe('route-A');
  const restored = { bindSettingsTarget: value => bindings.push(value) };
  const services = new InstantiationService(new ServiceCollection([ITextEditorService, {
    createTextEditor(options) { requests.push(options); return restored; },
  }]));
  try {
    expect(serializer.deserialize(services, JSON.stringify({ ...serialized,
      settingsTarget: { ...stable, gateway_connection_id: 'removed-route' } }))).toBe(restored);
    expect(bindings).toEqual([stable]);
    expect(requests).toEqual([{ resource, label: '目标 JSON', description: '原描述', encoding: 'utf8', languageId: 'jsonc', forceFile: true }]);
    serializer.deserialize(services, JSON.stringify({ ...serialized, settingsTarget: undefined }));
    expect(bindings).toHaveLength(1);
  } finally { services.dispose(); }
});

scenario('原文件 Input 只在同归属的新连接失效 Pane，稳定资源和模型引用不变', () => {
  const input = new FileEditorInput(), model = { dirty: true, etag: 'original-etag' }, reference = { object: model };
  input.setPreferredLanguageId = () => {};
  input.cachedTextFileModelReference = reference;
  const first = location({ view_id: 'settings-profile', workspace_ids: [] }), next = { ...first, gateway_connection_id: 'new-route' };
  let invalidations = 0; input.onDidChangeEditorPaneScope(() => invalidations++);
  try {
    input.bindSettingsTarget(first, { integrationTransport: 'old' });
    input.verifiedSettingsContext = { integrationTransport: 'verified-old' };
    input.bindSettingsTarget(next, { integrationTransport: 'new' });
    expect(input.settingsLocation).toEqual(next); expect(input.editorPaneScopeVersion).toBe(1); expect(invalidations).toBe(1);
    expect(input.verifiedSettingsContext).toBeUndefined(); expect(input.cachedTextFileModelReference).toBe(reference);
    expect(model).toEqual({ dirty: true, etag: 'original-etag' });
    input.bindSettingsTarget(next); expect(input.editorPaneScopeVersion).toBe(1); expect(invalidations).toBe(1);
    input.bindSettingsTarget(settingsHistoryLocation(first));
    expect(input.settingsLocation).toEqual(next); expect(input.editorPaneScopeVersion).toBe(1);
    expect(() => input.bindSettingsTarget({ ...next, profile_id: 'other-profile' })).toThrow('不能重新绑定');
    expect(input.settingsLocation).toEqual(next);
  } finally { input.dispose(); }
});

scenario('显式重选沿原入口重绑同一 UI Input 与 JSON，各组恢复原视图；同连接不重建', async () => {
  const f = fixture(), input = f.input(), first = await f.editor(input, 1), second = await f.editor(input, 2);
  const otherView = f.input(location({ view_id: 'other-view', workspace_ids: ['workspace-C'] }));
  const file = new FileEditorInput(), calls = [], references = [], selected = { gateway_id: 'gateway-A', gateway_connection_id: 'new-route' };
  file.setPreferredLanguageId = () => {};
  file.bindSettingsTarget(location({ view_id: 'settings-profile', workspace_ids: [] }));
  const split = new SideBySideEditorInput('设置 JSON', undefined, new EditorInput(), file, {});
  const view = (group, active, query) => Object.assign(group, { editors: [active], activeEditor: active,
    activeEditorPane: { getViewState: () => ({ query }), options: { marker: query } },
    async openEditor(opened, options) { calls.push({ group: this.id, input: opened, options }); return undefined; } });
  const a = view(first.pane.group, input, '@id:first'), b = view(second.pane.group, input, '@modified'), c = view({ id: 3 }, split, 'json-position');
  const d = view({ id: 4 }, otherView, '@id:other-view');
  const contexts = [];
  switching.capture = async (_parent, _token, target) => {
    expect(target).toBe(selected);
    return { location: location({ gateway_connection_id: 'new-route' }), context: { integrationTransport: 'new-ui' } };
  };
  switching.prepare = async target => { contexts.push(target); return { integrationTransport: 'new-json' }; };
  switching.rebind = (_parent, contexts) => { const ref = { contexts, disposed: false, dispose() { this.disposed = true; } }; references.push(ref); return ref; };
  first.pane.commandService = { executeCommand: async () => selected };
  first.pane.editorGroupService = { groups: [a, b, c, d] };
  try {
    const resource = input.resource, history = new SettingsEditor2InputSerializer().serialize(input);
    first.change('pending.first', 'old-queue');
    await first.pane.switchSettingsGateway();
    expect(first.group.configuration.calls.map(call => call.key)).toEqual(['pending.first']);
    expect(input.resource).toBe(resource); expect(new SettingsEditor2InputSerializer().serialize(input)).toBe(history);
    expect(input.editorPaneScopeVersion).toBe(1); expect(file.editorPaneScopeVersion).toBe(1); expect(split.editorPaneScopeVersion).toBe(1);
    expect(otherView.location).toEqual(location({ view_id: 'other-view', workspace_ids: ['workspace-C'], gateway_connection_id: 'new-route' }));
    expect(otherView.editorPaneScopeVersion).toBe(1);
    expect(input.location.gateway_connection_id).toBe('new-route'); expect(file.settingsLocation.gateway_connection_id).toBe('new-route');
    expect(contexts).toEqual([location({ view_id: 'other-view', workspace_ids: ['workspace-C'], gateway_connection_id: 'new-route' }),
      location({ view_id: 'settings-profile', workspace_ids: [], gateway_connection_id: 'new-route' })]);
    expect(calls.map(call => call.group).sort()).toEqual([1, 2, 3, 4]);
    expect(calls.find(call => call.group === 1)).toMatchObject({ input, options: { pinned: true, viewState: { query: '@id:first' } } });
    expect(calls.find(call => call.group === 2)).toMatchObject({ input, options: { preserveFocus: true, viewState: { query: '@modified' } } });
    expect(calls.find(call => call.group === 3)).toMatchObject({ input: split, options: { preserveFocus: true, viewState: { query: 'json-position' } } });
    expect(calls.every(call => !call.options.forceReload)).toBe(true);
    expect(references).toHaveLength(1); expect(references[0].contexts).toHaveLength(3); expect(references.every(ref => ref.disposed)).toBe(true);
    calls.length = 0; await first.pane.switchSettingsGateway();
    expect(input.editorPaneScopeVersion).toBe(1); expect(file.editorPaneScopeVersion).toBe(1);
    expect(references).toHaveLength(1); expect(calls.map(call => call.group)).toEqual([1]);
  } finally { split.dispose(); split.secondary.dispose(); file.dispose(); f.close(); }
});

scenario('当前 Pane 改投另一 view 时，先创建新 Input，再释放原 Pane 和 child DI 并沿原组打开', async () => {
  const f = fixture(), input = f.input(location({ view_id: 'other-view', workspace_ids: ['workspace-C'] }));
  const { pane, group } = await f.editor(input, 1), calls = [], selected = { gateway_id: 'gateway-A', gateway_connection_id: 'new-route' };
  const captured = { location: location({ gateway_connection_id: 'new-route' }), context: { integrationTransport: 'new-view' } };
  const invalidation = input.onDidChangeEditorPaneScope(() => { pane.dispose(); group.lifetime.dispose(); });
  Object.assign(pane.group, { editors: [input], activeEditor: input,
    activeEditorPane: { getViewState: () => ({ query: '@id:old-view' }), options: {} },
    async openEditor(next, options) {
      expect(pane.input).toBeUndefined();
      expect(() => group.di.invokeFunction(() => {})).toThrow('disposed');
      calls.push({ next, options });
    } });
  pane.editorGroupService = { groups: [pane.group] };
  pane.commandService = { executeCommand: async () => selected };
  switching.capture = async () => captured;
  switching.prepare = async target => {
    expect(target).toEqual(location({ view_id: 'other-view', workspace_ids: ['workspace-C'], gateway_connection_id: 'new-route' }));
    return { integrationTransport: 'old-view-new-route' };
  };
  switching.rebind = () => toDisposable(() => {});
  try {
    await pane.switchSettingsGateway();
    expect(input.editorPaneScopeVersion).toBe(1); expect(calls).toHaveLength(1);
    expect(calls[0].next).not.toBe(input); expect(calls[0].next.location).toEqual(captured.location);
    expect(calls[0].options).toEqual({ pinned: true, viewState: undefined });
  } finally { invalidation.dispose(); for (const call of calls) call.next.dispose(); f.close(); }
});

for (const target of ['same', 'other']) for (const choice of ['save', 'discard', 'cancel']) {
  scenario(`旧连接拒绝并保留延迟草稿后，${target === 'same' ? '同一稳定' : '其它'}目标重选沿原确认${choice}且不处理其它目标`, async () => {
    const f = fixture(), input = f.input(), first = await f.editor(input, 1);
    const otherInput = f.input(location({ view_id: 'other-view', workspace_ids: ['workspace-C'] })), other = await f.editor(otherInput, 2);
    const untouchedInput = f.input(location({ backend_id: 'backend-unaffected' })), untouched = await f.editor(untouchedInput, 3);
    const victim = target === 'same' ? other : first, victimInput = target === 'same' ? otherInput : input;
    const failure = new Error('具名旧连接写入边界：连接已删除'), failed = Promise.withResolvers(), dialogs = [], opened = [], rebound = [];
    const captured = { location: location({ gateway_connection_id: 'new-route', ...(target === 'other' ? { backend_id: 'backend-selected' } : {}) }),
      context: { integrationTransport: 'prepared-new-route' } };
    victim.group.configuration.write = async () => { throw failure; };
    victim.pane.logService.error = error => { victim.logs.push(error); failed.resolve(); };
    const groups = [[first, input], [other, otherInput], [untouched, untouchedInput]].map(([editor, active]) => Object.assign(editor.pane.group, {
      editors: [active], activeEditor: active, activeEditorPane: { getViewState: () => ({}), options: {} },
      async openEditor(next) { opened.push(next); },
    }));
    first.pane.editorGroupService = { groups };
    first.pane.commandService = { executeCommand: async () => ({ gateway_id: 'gateway-A', gateway_connection_id: 'new-route' }) };
    first.pane.fileDialogService = { async showSaveConfirm(names) {
      dialogs.push(names); expect(victimInput.isDirty()).toBe(true); expect(victimInput.editorPaneScopeVersion).toBe(0);
      expect(victim.group.configuration.calls.map(call => call.value)).toEqual([17]);
      return { save: ConfirmResult.SAVE, discard: ConfirmResult.DONT_SAVE, cancel: ConfirmResult.CANCEL }[choice];
    } };
    switching.capture = async () => captured;
    switching.prepare = async () => ({ integrationTransport: 'prepared-other-view' });
    switching.rebind = (_parent, contexts) => { rebound.push(contexts); return toDisposable(() => {}); };
    try {
      victim.change('editor.fontSize', 17, SettingValueType.String, true); await failed.promise;
      victim.change('editor.fontSize', 21); untouched.change('editor.tabSize', 4);
      if (target === 'other') other.change('editor.tabSize', 6);
      const reconnect = first.pane.switchSettingsGateway();
      if (choice === 'save') await expect(reconnect).rejects.toBe(failure);
      else await reconnect;
      expect(dialogs).toEqual([[victimInput.getName()]]);
      expect(untouchedInput.isDirty()).toBe(true); expect(untouched.group.configuration.calls).toEqual([]);
      expect(untouchedInput.editorPaneScopeVersion).toBe(0);
      if (target === 'other') { expect(otherInput.isDirty()).toBe(true); expect(other.group.configuration.calls).toEqual([]); }
      if (choice === 'discard') {
        expect(victimInput.isDirty()).toBe(false); expect(victim.group.configuration.calls.map(call => call.value)).toEqual([17]);
        expect(opened.at(-1).location).toEqual(captured.location);
        expect(rebound).toHaveLength(target === 'same' ? 1 : 0);
      } else {
        expect(victimInput.isDirty()).toBe(true); expect(opened).toEqual([]); expect(rebound).toEqual([]);
        expect(victimInput.location.gateway_connection_id).toBe('route-A'); expect(victimInput.editorPaneScopeVersion).toBe(0);
        expect(victim.group.configuration.calls.map(call => call.value)).toEqual(choice === 'save' ? [17, 21] : [17]);
      }
    } finally { for (const next of opened) if (next !== input && next !== otherInput && next !== untouchedInput) next.dispose(); f.close(); }
  });
}

scenario('明确不保存仍等待旧在途写入失败结算，保留原拒绝诊断后才丢弃草稿并重连', async () => {
  const f = fixture(), input = f.input(), first = await f.editor(input, 1), otherInput = f.input(location({ view_id: 'other-view' }));
  const other = await f.editor(otherInput, 2), entered = Promise.withResolvers(), release = Promise.withResolvers(), confirmed = Promise.withResolvers();
  const failure = new Error('具名旧连接在途写入失败'), changes = [], captured = { location: location({ gateway_connection_id: 'new-route' }), context: {} };
  const groups = [[first, input], [other, otherInput]].map(([editor, active]) => Object.assign(editor.pane.group, {
    editors: [active], activeEditor: active, async openEditor() {},
  }));
  other.group.configuration.write = async () => { entered.resolve(); await release.promise; throw failure; };
  first.pane.editorGroupService = { groups };
  first.pane.commandService = { executeCommand: async () => ({ gateway_id: 'gateway-A' }) };
  first.pane.fileDialogService = { async showSaveConfirm() { confirmed.resolve(); return ConfirmResult.DONT_SAVE; } };
  switching.capture = async () => captured; switching.prepare = async () => ({});
  switching.rebind = () => { changes.push('rebind'); return toDisposable(() => {}); };
  let reconnect;
  try {
    other.change('editor.fontSize', 17); const failedWrite = otherInput.save().then(() => undefined, error => error); await entered.promise;
    other.change('editor.tabSize', 6);
    reconnect = first.pane.switchSettingsGateway(); const result = reconnect.then(() => undefined, error => error); await confirmed.promise;
    expect(changes).toEqual([]); expect(otherInput.isDirty()).toBe(true); expect(otherInput.editorPaneScopeVersion).toBe(0);
    release.resolve(); expect(await failedWrite).toBe(failure); expect(await result).toBeUndefined();
    expect(other.logs).toEqual([failure]); expect(other.group.configuration.calls.map(call => call.key)).toEqual(['editor.fontSize']);
    expect(otherInput.isDirty()).toBe(false); expect(changes).toEqual(['rebind']); expect(otherInput.location.gateway_connection_id).toBe('new-route');
  } finally { release.resolve(); await reconnect?.catch(() => {}); f.close(); }
});

scenario('保存确认等待期间原 Pane 已切换时，迟到的不保存选择不能清除其它 view 草稿或改投', async () => {
  const f = fixture(), input = f.input(), first = await f.editor(input, 1), otherInput = f.input(location({ view_id: 'other-view' }));
  const other = await f.editor(otherInput, 2), entered = Promise.withResolvers(), choice = Promise.withResolvers(), calls = [];
  const groups = [[first, input], [other, otherInput]].map(([editor, active]) => Object.assign(editor.pane.group, {
    editors: [active], activeEditor: active, async openEditor() { calls.push('open'); },
  }));
  first.pane.editorGroupService = { groups }; first.pane.commandService = { executeCommand: async () => ({ gateway_id: 'gateway-A' }) };
  first.pane.fileDialogService = { async showSaveConfirm() { entered.resolve(); return choice.promise; } };
  switching.capture = async () => ({ location: location({ gateway_connection_id: 'new-route' }), context: {} });
  switching.prepare = async () => ({}); switching.rebind = () => { calls.push('rebind'); return toDisposable(() => {}); };
  try {
    other.change('editor.fontSize', 21); const pending = first.pane.switchSettingsGateway(); await entered.promise;
    first.pane.clearInput(); choice.resolve(ConfirmResult.DONT_SAVE); await pending;
    expect(calls).toEqual([]); expect(otherInput.isDirty()).toBe(true); expect(other.group.configuration.calls).toEqual([]);
    expect(otherInput.location.gateway_connection_id).toBe('route-A'); expect(otherInput.editorPaneScopeVersion).toBe(0);
  } finally { choice.resolve(ConfirmResult.CANCEL); f.close(); }
});

scenario('确认保存期间新增草稿仍保留 dirty，重选明确拒绝且不隐式再次 flush 或改投', async () => {
  const f = fixture(), input = f.input(), first = await f.editor(input, 1), entered = Promise.withResolvers(), release = Promise.withResolvers(), calls = [];
  Object.assign(first.pane.group, { editors: [input], activeEditor: input, async openEditor() { calls.push('open'); } });
  first.pane.editorGroupService = { groups: [first.pane.group] };
  first.pane.commandService = { executeCommand: async () => ({ gateway_id: 'gateway-A' }) };
  first.group.configuration.write = async () => { entered.resolve(); await release.promise; };
  switching.capture = async () => ({ location: location({ gateway_connection_id: 'new-route' }), context: {} });
  switching.rebind = () => { calls.push('rebind'); return toDisposable(() => {}); };
  let reconnect;
  try {
    first.change('editor.fontSize', 17); reconnect = first.pane.switchSettingsGateway();
    const result = reconnect.then(() => undefined, error => error); await entered.promise;
    first.change('editor.fontSize', 21); release.resolve(); expect((await result).message).toContain('未结清');
    expect(input.isDirty()).toBe(true); expect(first.group.configuration.calls.map(call => call.value)).toEqual([17]);
    expect(calls).toEqual([]); expect(input.location.gateway_connection_id).toBe('route-A'); expect(input.editorPaneScopeVersion).toBe(0);
  } finally { release.resolve(); await reconnect?.catch(() => {}); f.close(); }
});

for (const stage of ['cancel', 'capture', 'json-prepare']) scenario(`显式重选 ${stage} 未成功时，原 Input、版本与路由零改动`, async () => {
  const f = fixture(), input = f.input(), { pane } = await f.editor(input, 1), file = new FileEditorInput();
  file.setPreferredLanguageId = () => {}; file.bindSettingsTarget(location({ view_id: 'settings-profile', workspace_ids: [] }));
  const calls = [];
  Object.assign(pane.group, { editors: [input, file], activeEditor: input, openEditor: (...args) => calls.push(args) });
  pane.editorGroupService = { groups: [pane.group] };
  pane.commandService = { executeCommand: async () => stage === 'cancel' ? undefined : { gateway_id: 'gateway-A' } };
  switching.capture = async () => {
    if (stage === 'capture') throw new Error('capture denied');
    return { location: location({ gateway_connection_id: 'new-route' }), context: { integrationTransport: 'new' } };
  };
  switching.prepare = async () => { throw new Error('json prepare denied'); };
  switching.rebind = () => { throw new Error('失败或取消不能重绑路由'); };
  try {
    if (stage === 'cancel') await pane.switchSettingsGateway();
    else await expect(pane.switchSettingsGateway()).rejects.toThrow(stage === 'capture' ? 'capture denied' : 'json prepare denied');
    expect(input.location).toEqual(location()); expect(file.settingsLocation.gateway_connection_id).toBe('route-A');
    expect(input.editorPaneScopeVersion).toBe(0); expect(file.editorPaneScopeVersion).toBe(0); expect(calls).toHaveLength(0);
  } finally { file.dispose(); f.close(); }
});

for (const delayed of [false, true]) scenario(`JSON 模型${delayed ? '迟到 resolve' : '既有 lease'}在重选后只保留新传输，关闭 UI 不释放模型引用`, async () => {
  const resource = URI.parse('boxteam://gateway-A/profile/settings'), willDispose = new Emitter(), gate = Promise.withResolvers();
  const model = { resource, dirty: true, etag: 'original-etag', onWillDispose: willDispose.event }, counts = new Map();
  const old = { route: 'old-route' }, next = { route: 'new-route' }, files = {};
  let selected = old, resolved = !delayed;
  const reference = context => {
    counts.set(context.route, (counts.get(context.route) ?? 0) + 1);
    return toDisposable(() => counts.set(context.route, counts.get(context.route) - 1));
  };
  const retainedFileModels = new WeakMap();
  const helpers = evaluate(helperParsed, ['retainSettingsFile', 'rebindSettingsFiles'], {
    retainedFileModels, MutableDisposable, Event, IFileService, ITextFileService, URI,
    targetSettings: () => ({ resource_uri: resource.toString() }), targetWorkspace: () => ({}),
    registerTargetConfigurationFiles: (_files, context) => reference(context),
    rebindTargetConfigurationFiles: (_files, contexts) => { selected = contexts[0]; return reference(selected); },
    retainTargetConfigurationFile: () => reference(selected),
  });
  const di = new InstantiationService(new ServiceCollection([IFileService, files], [ITextFileService, { files: { get: () => resolved ? model : undefined } }]));
  const paneReference = reference(old);
  try {
    const opening = helpers.retainSettingsFile(di, old, resource, async () => { if (delayed) await gate.promise; resolved = true; return model; });
    if (!delayed) expect(await opening).toBe(model);
    const transition = helpers.rebindSettingsFiles(di, [next]);
    gate.resolve(); expect(await opening).toBe(model);
    paneReference.dispose(); transition.dispose();
    expect(counts.get('old-route')).toBe(0); expect(counts.get('new-route')).toBe(1);
    expect(model.dirty).toBe(true); expect(model.etag).toBe('original-etag');
    expect(retainedFileModels.has(model)).toBe(true);
    willDispose.fire(); expect(counts.get('new-route')).toBe(0); expect(retainedFileModels.has(model)).toBe(false);
  } finally { gate.resolve(); willDispose.dispose(); paneReference.dispose(); di.dispose(); }
});

scenario('同一原输入为两组创建不同原模型，各组关闭只释放自己的模型与绑定', async () => {
  const f = fixture();
  try {
    const initial = { integrationTransport: 'initial' }, input = f.input(location(), initial);
    await expect(input.resolve()).rejects.toThrow('设置模型尚未绑定当前编辑器组');
    const a = await f.group(input, 1), b = await f.group(input, 2), first = await input.resolve(a.di), second = await input.resolve(b.di);
    expect(first).toBeInstanceOf(Settings2EditorModel); expect(second).toBeInstanceOf(Settings2EditorModel); expect(first).not.toBe(second);
    expect(f.preparations.map(value => value.context)).toEqual([initial, { integrationTransport: 'prepared', backend: 'backend-A' }]);
    expect(f.preparations.map(value => value.preferences.defaultTargets)).toEqual([[ConfigurationTarget.USER_LOCAL], [ConfigurationTarget.USER_LOCAL]]);
    first.setAdditionalGroups([{ id: 'first-only', sections: [] }]);
    expect(first.settingsGroups.map(group => group.id)).toEqual(['backend-A/1', 'first-only']);
    expect(second.settingsGroups.map(group => group.id)).toEqual(['backend-A/2']);
    let firstEvents = 0, secondEvents = 0;
    first.onDidChangeGroups(() => firstEvents++); second.onDidChangeGroups(() => secondEvents++);
    a.configuration.changed.fire({ source: ConfigurationTarget.DEFAULT });
    expect([firstEvents, secondEvents]).toEqual([1, 0]);
    a.lifetime.dispose();
    expect(first.isDisposed()).toBe(true); expect(second.isDisposed()).toBe(false); expect(input.isDisposed()).toBe(false);
    await expect(input.resolve(a.di)).rejects.toThrow('设置模型尚未绑定当前编辑器组');
    expect(await input.resolve(b.di)).toBe(second);
    b.lifetime.dispose(); expect(second.isDisposed()).toBe(true);
  } finally { f.close(); }
});

scenario('默认窗口输入保留单个原模型，原 dispose 释放模型和配置监听', async () => {
  const f = fixture();
  try {
    const input = f.input(undefined), model = await input.resolve();
    expect(model).toBeInstanceOf(Settings2EditorModel); expect(await input.resolve(f.parent)).toBe(model);
    const lifetime = new DisposableStore();
    expect(await input.prepareEditorPane(f.parent, lifetime, CancellationToken.None)).toBe(f.parent); lifetime.dispose();
    expect(f.preparations).toEqual([]); expect(f.windowPreferences.defaultTargets).toEqual([ConfigurationTarget.USER_LOCAL]);
    let events = 0; model.onDidChangeGroups(() => events++);
    f.defaultConfiguration.changed.fire({ source: ConfigurationTarget.DEFAULT }); expect(events).toBe(1);
    input.dispose(); expect(model.isDisposed()).toBe(true);
    f.defaultConfiguration.changed.fire({ source: ConfigurationTarget.DEFAULT }); expect(events).toBe(1);
  } finally { f.close(); }
});

scenario('延迟变更冻结原 target/resource/language，不随目标切换或搜索改投', async () => {
  const f = fixture();
  try {
    const input = f.input(), e = await f.editor(input, 1), folder = URI.parse('boxteam-file://gateway-A/workspace-A');
    e.pane.settingsTargetsWidget.settingsTarget = folder; e.pane.searchWidget.setValue('@lang:javascript editor');
    e.change('editor.tabSize', 3);
    e.pane.settingsTargetsWidget.settingsTarget = ConfigurationTarget.USER_LOCAL; e.pane.searchWidget.setValue('@lang:python');
    expect(input.isDirty()).toBe(true); expect(e.group.configuration.calls).toEqual([]);
    expect(await input.save()).toBe(input);
    expect(e.group.configuration.calls).toEqual([{ owner: 'backend-A/1', key: 'editor.tabSize', value: 3,
      overrides: { resource: folder, overrideIdentifiers: ['javascript'] }, target: ConfigurationTarget.WORKSPACE_FOLDER,
      options: { handleDirtyFile: 'save' } }]);
    expect(e.group.configuration.inspected).toEqual([{ key: 'editor.tabSize', overrides: { resource: folder, overrideIdentifiers: ['javascript'] } }]);
    expect(e.reports[0].settingsTarget).toBe(folder); expect(input.isDirty()).toBe(false); expect(e.logs).toEqual([]);
  } finally { f.close(); }
});

scenario('连续不同 key 全部保存，同 key 合并但不同语言和目标保留独立写入', async () => {
  const f = fixture();
  try {
    const input = f.input(), e = await f.editor(input, 1);
    e.change('editor.fontSize', 15); e.change('editor.tabSize', 4); e.change('editor.fontSize', 18);
    e.pane.searchWidget.setValue('@lang:javascript'); e.change('editor.fontSize', 20);
    e.pane.settingsTargetsWidget.settingsTarget = ConfigurationTarget.WORKSPACE; e.change('editor.fontSize', 22);
    await input.save();
    expect(e.group.configuration.calls.map(call => [call.key, call.value, call.overrides.overrideIdentifiers, call.target])).toEqual([
      ['editor.fontSize', 18, undefined, ConfigurationTarget.USER_REMOTE], ['editor.tabSize', 4, undefined, ConfigurationTarget.USER_REMOTE],
      ['editor.fontSize', 20, ['javascript'], ConfigurationTarget.USER_REMOTE], ['editor.fontSize', 22, ['javascript'], ConfigurationTarget.WORKSPACE],
    ]);
    expect(input.isDirty()).toBe(false);
  } finally { f.close(); }
});

scenario('原 updateChangedSetting 保留默认值复位、工作区显式值和语言复位规则', async () => {
  const f = fixture();
  try {
    const input = f.input(), e = await f.editor(input, 1);
    e.group.configuration.defaults.set('editor.fontSize', 14);
    e.change('editor.fontSize', 14); await input.save();
    e.pane.settingsTargetsWidget.settingsTarget = ConfigurationTarget.WORKSPACE;
    e.change('editor.fontSize', 14); await input.save();
    e.pane.searchWidget.setValue('@lang:javascript');
    e.change('editor.fontSize', undefined, SettingValueType.Number, true); await input.save();
    expect(e.group.configuration.calls.map(call => [call.value, call.target, call.overrides.overrideIdentifiers])).toEqual([
      [undefined, ConfigurationTarget.USER_REMOTE, undefined], [14, ConfigurationTarget.WORKSPACE, undefined],
      [undefined, ConfigurationTarget.WORKSPACE, ['javascript']],
    ]);
    expect(e.rendered).toEqual([['editor.fontSize', false], ['editor.fontSize', false], ['editor.fontSize', true]]);
    expect(input.isDirty()).toBe(false);
  } finally { f.close(); }
});

scenario('切换原 Pane input 后原输入 save 仍写自己的目标服务，并发布 clean 状态', async () => {
  const f = fixture();
  try {
    const first = f.input(), second = f.input(location({ backend_id: 'backend-B', name: 'Target B' }));
    const a = await f.editor(first, 1), b = await f.editor(second, 1), dirty = [];
    first.onDidChangeDirty(() => dirty.push(first.isDirty()));
    a.change('editor.fontSize', 18); a.pane.clearInput();
    b.change('editor.fontSize', 24);
    await first.save();
    expect(a.group.configuration.calls.map(call => [call.owner, call.value])).toEqual([['backend-A/1', 18]]);
    expect(b.group.configuration.calls).toEqual([]); expect(second.isDirty()).toBe(true);
    expect(first.isDirty()).toBe(false); expect(dirty.at(-1)).toBe(false);
    await second.save();
    expect(b.group.configuration.calls.map(call => [call.owner, call.value])).toEqual([['backend-B/1', 24]]);
  } finally { f.close(); }
});

scenario('写入中的同 key 新值不会被旧完成删除，后续 save 按顺序保存', async () => {
  const f = fixture(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  try {
    const input = f.input(), e = await f.editor(input, 1);
    e.group.configuration.write = async call => { if (call.value === 16) { entered.resolve(); await gate.promise; } };
    e.change('editor.fontSize', 16); const firstSave = input.save(); await entered.promise;
    e.change('editor.fontSize', 19); e.change('editor.tabSize', 2);
    const secondSave = input.save(); gate.resolve(); await Promise.all([firstSave, secondSave]);
    expect(e.group.configuration.calls.map(call => [call.key, call.value])).toEqual([
      ['editor.fontSize', 16], ['editor.fontSize', 19], ['editor.tabSize', 2],
    ]);
    expect(input.isDirty()).toBe(false);
  } finally { gate.resolve(); f.close(); }
});

scenario('原输入 save 等待所有组在途写入，原 revert 丢弃各组未发送变更', async () => {
  const f = fixture(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  try {
    const input = f.input(), a = await f.editor(input, 1), b = await f.editor(input, 2);
    a.group.configuration.write = async () => { entered.resolve(); await gate.promise; };
    a.change('editor.fontSize', 16); b.change('editor.tabSize', 3);
    let saved = false; const save = input.save().then(value => { saved = true; return value; }); await entered.promise;
    expect(saved).toBe(false); expect(input.isDirty()).toBe(true);
    gate.resolve(); expect(await save).toBe(input); expect(input.isDirty()).toBe(false);
    a.change('editor.fontSize', 22); b.change('editor.tabSize', 7); await input.revert();
    expect(input.isDirty()).toBe(false);
    expect(a.group.configuration.calls.map(call => call.value)).toEqual([16]); expect(b.group.configuration.calls.map(call => call.value)).toEqual([3]);
    expect(a.refreshes.at(-1)).toEqual([undefined, true]); expect(b.refreshes.at(-1)).toEqual([undefined, true]);
  } finally { gate.resolve(); f.close(); }
});

scenario('关闭一个原 Pane 解除该组 writer 和模型，另一组 pending 仍由原输入保存', async () => {
  const f = fixture();
  try {
    const input = f.input(), a = await f.editor(input, 1), b = await f.editor(input, 2);
    const firstModel = await input.resolve(a.group.di), secondModel = await input.resolve(b.group.di);
    a.change('editor.fontSize', 16); b.change('editor.tabSize', 3);
    a.pane.dispose(); a.group.lifetime.dispose();
    expect(a.pane.settingsWriterInput).toBeUndefined(); expect(b.pane.settingsWriterInput).toBe(input);
    expect(firstModel.isDisposed()).toBe(true); expect(secondModel.isDisposed()).toBe(false); expect(input.isDirty()).toBe(true);
    await input.save();
    expect(a.group.configuration.calls).toEqual([]); expect(b.group.configuration.calls.map(call => call.value)).toEqual([3]);
    expect(input.isDirty()).toBe(false);
  } finally { f.close(); }
});

scenario('原 revert 等待在途写入后丢弃新草稿，不把新草稿继续发出', async () => {
  const f = fixture(), entered = Promise.withResolvers(), gate = Promise.withResolvers();
  try {
    const input = f.input(), e = await f.editor(input, 1);
    e.group.configuration.write = async () => { entered.resolve(); await gate.promise; };
    e.change('editor.fontSize', 16); const save = input.save(); await entered.promise;
    e.change('editor.fontSize', 22); e.change('editor.tabSize', 7);
    let reverted = false; const revert = input.revert().then(() => { reverted = true; });
    expect(reverted).toBe(false); expect(input.isDirty()).toBe(true);
    gate.resolve(); await Promise.all([save, revert]);
    expect(e.group.configuration.calls.map(call => [call.key, call.value])).toEqual([['editor.fontSize', 16]]);
    expect(input.isDirty()).toBe(false); expect(e.refreshes.at(-1)).toEqual([undefined, true]);
  } finally { gate.resolve(); f.close(); }
});

scenario('保存失败保留 dirty 与未写 key，原 revert 可清除失败草稿', async () => {
  const f = fixture();
  try {
    const input = f.input(), e = await f.editor(input, 1), failure = new Error('integration configuration write rejected');
    e.group.configuration.write = async () => { throw failure; };
    e.change('editor.fontSize', 17); e.change('editor.tabSize', 6);
    await expect(input.save()).rejects.toBe(failure);
    expect(input.isDirty()).toBe(true); expect(e.group.configuration.calls.map(call => call.key)).toEqual(['editor.fontSize']);
    expect(e.rendered).toEqual([]); expect(e.reports).toEqual([]);
    await input.revert(); expect(input.isDirty()).toBe(false);
    e.group.configuration.write = async () => {}; await input.save();
    expect(e.group.configuration.calls.map(call => call.key)).toEqual(['editor.fontSize']);
  } finally { f.close(); }
});

scenario('原 Delayer 自动提交失败向 log 暴露错误，重试 save 保留完整队列', async () => {
  const f = fixture(), logged = Promise.withResolvers();
  try {
    const input = f.input(), e = await f.editor(input, 1), failure = new Error('integration delayed write rejected');
    e.group.configuration.write = async () => { throw failure; };
    e.pane.logService.error = error => { e.logs.push(error); logged.resolve(); };
    e.change('editor.fontSize', 21, SettingValueType.String, true); await logged.promise;
    expect(e.logs).toEqual([failure]); expect(input.isDirty()).toBe(true);
    e.group.configuration.write = async () => {}; await input.save();
    expect(e.group.configuration.calls.map(call => call.value)).toEqual([21, 21]); expect(input.isDirty()).toBe(false);
  } finally { f.close(); }
});

scenario('原 JSON contribution 对同一 model 反序完成的两次准备只保留最新 renderer', async () => {
  const f = jsonRendererFixture();
  try {
    f.showModel();
    const first = f.contribution._createPreferencesRenderer(), second = f.contribution._createPreferencesRenderer();
    expect(f.requests.map(request => request.uri)).toEqual([f.text.uri, f.text.uri]);
    const stale = f.model(ConfigurationTarget.WORKSPACE), current = f.model(ConfigurationTarget.USER_REMOTE);
    f.requests[1].resolve(current.model); await second;
    expect(f.renderers).toHaveLength(1); expect(f.renderers[0].model).toBe(current.model);
    expect(f.renderers[0].kind).toBe('user'); expect(f.renderers[0].renderCount).toBe(1); expect(f.context()).toBe(true);
    f.requests[0].resolve(stale.model); await first;
    expect(stale.model.isDisposed()).toBe(true); expect(stale.reference.disposed).toBe(1);
    expect(current.model.isDisposed()).toBe(false); expect(current.reference.disposed).toBe(0);
    expect(f.contribution.currentRenderer).toBe(f.renderers[0]); expect(f.renderers).toHaveLength(1);
    expect(f.renderers[0].renderCount).toBe(1); expect(f.context()).toBe(true);
  } finally { f.close(); }
});

scenario('原 JSON contribution dispose 后迟到模型立即释放，不创建或调用 renderer', async () => {
  const f = jsonRendererFixture();
  try {
    f.showModel(); const pending = f.contribution._createPreferencesRenderer();
    expect(f.requests).toHaveLength(1);
    const late = f.model(ConfigurationTarget.USER_REMOTE);
    f.contribution.dispose(); f.requests[0].resolve(late.model); await pending;
    expect(late.model.isDisposed()).toBe(true); expect(late.reference.disposed).toBe(1);
    expect(f.renderers).toEqual([]); expect(f.contribution.currentRenderer).toBeUndefined(); expect(f.context()).toBe(false);
  } finally { f.close(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-fingerprints.json'), `${JSON.stringify({ pinnedUpstreamCommit, fingerprints, selectedEditorMembers }, null, 2)}\n`);
  await run.writeResult(!executed.length ? 'skipped' : executed.length === passed.length ? 'passed' : 'failed', {
    boundary: 'Integration：执行完整 sourceTransform 后原 SettingsEditor2Input、serializer、原 Settings2EditorModel/SettingsEditorModel、原 PreferencesService.createSettings2EditorModel、原 prepareSettingsPane、完整 SettingsEditorContribution，以及 AST 选取的 SettingsEditor2 队列/原 setInput/clearInput/updateChangedSetting；使用原 EditorInput/EditorPane/Delayer/InstantiationService/DisposableStore。仅 Composite、配置传输与装配、默认设置数据、文本/代码编辑器、context key 服务、UI 渲染/扩展发现/遥测出口为具名边界；未经过真实 Gateway、配置 HTTP、schema 发现或浏览器页面。',
    registered, executed, passed, filtered: registered.filter(name => !executed.includes(name)),
  });
  await run.close();
});
