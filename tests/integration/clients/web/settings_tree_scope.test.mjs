import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Disposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Event, Emitter } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { Action } from '../../../../src/clients/workbench/upstream/src/vs/base/common/actions.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { Schemas } from '../../../../src/clients/workbench/upstream/src/vs/base/common/network.js';
import { isEqual } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { isUndefinedOrNull } from '../../../../src/clients/workbench/upstream/src/vs/base/common/types.js';
import { escapeRegExpCharacters, isFalsyOrWhitespace } from '../../../../src/clients/workbench/upstream/src/vs/base/common/strings.js';
import * as arrays from '../../../../src/clients/workbench/upstream/src/vs/base/common/arrays.js';
import { InstantiationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiationService.js';
import { ServiceCollection } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IInstantiationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IWorkspaceContextService, WorkbenchState, isWorkspaceFolder } from '../../../../src/clients/workbench/upstream/src/vs/platform/workspace/common/workspace.js';
import { ConfigurationTarget } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configuration.js';
import { ConfigurationScope, EditPresentationTypes, Extensions } from '../../../../src/clients/workbench/upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import { Registry } from '../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { USER_LOCAL_AND_REMOTE_SETTINGS } from '../../../../src/clients/workbench/upstream/src/vs/platform/request/common/request.js';
import { IProductService } from '../../../../src/clients/workbench/upstream/src/vs/platform/product/common/productService.js';
import { ILabelService } from '../../../../src/clients/workbench/upstream/src/vs/platform/label/common/label.js';
import { ILanguageService } from '../../../../src/clients/workbench/upstream/src/vs/editor/common/languages/language.js';
import { APPLICATION_SCOPES, FOLDER_SCOPES, LOCAL_MACHINE_SCOPES, REMOTE_MACHINE_SCOPES, WORKSPACE_SCOPES, IWorkbenchConfigurationService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/configuration/common/configuration.js';
import { IWorkbenchEnvironmentService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/environment/common/environmentService.js';
import { IUserDataProfileService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/userDataProfile/common/userDataProfile.js';
import { IExperimentalSettingsService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/configuration/common/experimentalSettings.js';
import { IManagedSettingsPresentationService } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/configuration/common/managedSettingsPresentation.js';
import { SettingValueType } from '../../../../src/clients/workbench/upstream/src/vs/workbench/services/preferences/common/preferences.js';
import * as preferences from '../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/preferences/common/preferences.js';
import { ITargetSettingsContext } from '../../../../src/clients/workbench/src/settings/scope.js';
import { SourceSettings } from '../../../../src/clients/workbench/src/settings/editor.ts';
import { settingsTreeScopeSourcePaths, transformSettingsTreeScope } from '../../../../src/clients/workbench/scripts/settingsTreeScopeTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_tree_scope.test.mjs').prepare();
const root = resolve('src/clients/workbench'), fingerprints = [], registered = [], passed = [];
const replaceOnce = (source, marker, replacement) => {
  if (source.split(marker).length !== 2) throw new Error(`树转换锚点必须恰好一次：${marker}`);
  return source.replace(marker, () => replacement);
};
async function converted(relative) {
  const path = join(root, 'upstream', relative), original = await readFile(path, 'utf8');
  const contents = transformSettingsTreeScope(root, path, original.replace(/\r\n/g, '\n'), replaceOnce);
  const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
  if (parsed.parseDiagnostics.length) throw new Error(`转换源码不能解析：${relative}`);
  fingerprints.push({ path: relative, original: createHash('sha256').update(original).digest('hex'),
    converted: createHash('sha256').update(contents).digest('hex') });
  await writeFile(join(run.artifactsDir, relative.replaceAll('/', '__')), contents);
  return parsed;
}
function evaluate(parsed, environment, names, select) {
  const source = parsed.statements.filter(node => !ts.isImportDeclaration(node) && (!select || select(node)))
    .map(node => node.getText(parsed).replace(/^export /gm, '')).join('\n');
  // 保留原装饰器元数据，下面经原 InstantiationService 验证真实 DI 构造链。
  const javascript = ts.transpile(source, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None,
    useDefineForClassFields: false, experimentalDecorators: true });
  return new Function(...Object.keys(environment), `${javascript}\nreturn { ${names.join(', ')} };`)(...Object.values(environment));
}

class ActionBarBoundary extends Disposable {
  domNode = { classList: { toggle() {} } };
  push(actions) { this.actions = actions; }
}
class FolderViewBoundary extends Disposable {
  constructor(action) { super(); this.action = action; this.folder = null; }
  setCount() {}
}
const domNode = () => ({ classList: { toggle() {} } });
const services = { IWorkspaceContextService, IInstantiationService, IWorkbenchEnvironmentService, ILabelService, ILanguageService,
  IWorkbenchConfigurationService, IUserDataProfileService, IProductService, IExperimentalSettingsService, IManagedSettingsPresentationService, ITargetSettingsContext };
const widgetParsed = await converted(settingsTreeScopeSourcePaths[0]);
const { SettingsTargetsWidget } = evaluate(widgetParsed, {
  ...services, Widget: Disposable, Emitter, Event, Action, ActionBar: ActionBarBoundary,
  ActionsOrientation: { HORIZONTAL: 0 }, FolderSettingsActionViewItem: FolderViewBoundary,
  DOM: { $: domNode, append: (_parent, child) => child }, URI, isEqual, Schemas, ConfigurationTarget, WorkbenchState, isWorkspaceFolder,
  localize: (_key, text) => text,
}, ['SettingsTargetsWidget'], node => node.name?.text === 'SettingsTargetsWidget');
const treeParsed = await converted(settingsTreeScopeSourcePaths[1]);
const tree = evaluate(treeParsed, {
  ...preferences, ...services, Disposable, Emitter, URI, arrays, Registry, Extensions, SettingValueType,
  ConfigurationTarget, ConfigurationScope, EditPresentationTypes, APPLICATION_SCOPES, FOLDER_SCOPES,
  LOCAL_MACHINE_SCOPES, REMOTE_MACHINE_SCOPES, WORKSPACE_SCOPES, USER_LOCAL_AND_REMOTE_SETTINGS,
  escapeRegExpCharacters, isFalsyOrWhitespace, isUndefinedOrNull, localize: (_key, text) => text,
}, ['SettingsTreeModel', 'SearchResultModel', 'SettingsTreeSettingElement', 'inspectSetting', 'SearchResultIdx']);

const selected = name => ({ _serviceBrand: undefined, target: { location: { version: 1, gateway_id: 'gateway-A', backend_id: 'backend-A',
  profile_id: 'profile-A', view_id: 'view-A', workspace_ids: ['workspace-A'], name }, configuration: {} } });

function fixture(targetContext = { _serviceBrand: undefined, target: undefined }, remoteAuthority, inspect) {
  const calls = [], hostLabels = [], configuration = {
    inspect(key, overrides) { calls.push({ key, overrides }); return inspect?.(key, overrides) ?? { defaultValue: false, userRemoteValue: true }; },
    isSettingAppliedForAllProfiles: () => true,
  };
  const environment = { remoteAuthority, isSessionsWindow: false }, context = {
    onDidChangeWorkbenchState: Event.None, onDidChangeWorkspaceFolders: Event.None,
    getWorkbenchState: () => WorkbenchState.FOLDER,
    getWorkspace: () => ({ name: 'Workspace A', folders: [] }), getWorkspaceFolder: () => undefined,
  };
  const di = new InstantiationService(new ServiceCollection(
    [ITargetSettingsContext, targetContext], [IWorkspaceContextService, context],
    [IWorkbenchEnvironmentService, environment], [ILabelService, { getHostLabel: (_scheme, authority) => { hostLabels.push(authority); return 'Host A'; } }],
    [ILanguageService, { getLanguageName: id => id === 'javascript' ? 'JavaScript' : undefined, isRegisteredLanguageId: () => true }],
    [IWorkbenchConfigurationService, configuration], [IUserDataProfileService, { currentProfile: { isDefault: false, useDefaultFlags: { settings: false } } }],
    [IProductService, {}], [IExperimentalSettingsService, { hasAssignment: () => false }],
    [IManagedSettingsPresentationService, { getValue: () => undefined }],
  ));
  const objects = [];
  return { di, calls, hostLabels, environment,
    widget(options = { enableRemoteSettings: true }) { const widget = di.createInstance(SettingsTargetsWidget, domNode(), options); objects.push(widget); return widget; },
    model(target, languageFilter) {
      const model = di.createInstance(tree.SettingsTreeModel, { settingsTarget: target, languageFilter }, true);
      objects.push(model); return model;
    },
    search(target) { const model = di.createInstance(tree.SearchResultModel, { settingsTarget: target }, null, true); objects.push(model); return model; },
    close() { for (const object of objects.reverse()) object.dispose(); di.dispose(); },
  };
}
const setting = (key, scope, extension = true) => ({ key, scope, type: 'boolean', description: [],
  ...(extension ? { extensionInfo: { id: 'sample.extension' } } : {}) });
function scenario(name, callback) { registered.push(name); test(name, async () => { await callback(); passed.push(name); }); }

scenario('来源设置上下文禁用原 scope picker，不依赖全窗口 remoteAuthority', async () => {
  const f = fixture(selected('Linux A'));
  try {
    const widget = f.widget();
    expect(widget.userLocalSettings.label).toBe('User');
    expect(widget.userRemoteSettings.label).toBe('Remote');
    expect(widget.userLocalSettings.enabled).toBe(false);
    expect(widget.userRemoteSettings.enabled).toBe(false);
    expect(widget.workspaceSettings.enabled).toBe(false);
    expect(widget.folderSettings.action.enabled).toBe(false);
    expect(f.hostLabels).toEqual([]); expect(f.environment.remoteAuthority).toBeUndefined();
  } finally { f.close(); }
});

scenario('默认 Widget 保留原窗口标签和远程资格', () => {
  const local = fixture(), remote = fixture(undefined, 'ssh-remote+host');
  try {
    const localWidget = local.widget(), remoteWidget = remote.widget();
    expect(localWidget.userLocalSettings.label).toBe('User'); expect(localWidget.userRemoteSettings.label).toBe('Remote');
    expect(localWidget.userRemoteSettings.enabled).toBe(false);
    expect(remoteWidget.userRemoteSettings.label).toBe('Remote [Host A]'); expect(remoteWidget.userRemoteSettings.enabled).toBe(true);
    remoteWidget.setResultCount(ConfigurationTarget.USER_LOCAL, 3); expect(remoteWidget.userLocalSettings.label).toBe('User (3)');
    expect(remote.environment.remoteAuthority).toBe('ssh-remote+host');
  } finally { local.close(); remote.close(); }
});

scenario('原 Widget 切换前等待写入结算，失败保留当前目标且不发布目标变更', async () => {
  const f = fixture(), gate = Promise.withResolvers(), error = new Error('原设置写入失败');
  let beforeChanges = 0;
  try {
    const widget = f.widget({ enableRemoteSettings: true, beforeTargetChange: async () => { beforeChanges++; await gate.promise; } });
    widget.settingsTarget = ConfigurationTarget.USER_LOCAL;
    const changes = []; widget.onDidTargetChange(value => changes.push(value));
    const changing = widget.userRemoteSettings.run();
    expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_LOCAL); expect(changes).toEqual([]);
    gate.resolve(); await changing;
    expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(changes).toEqual([ConfigurationTarget.USER_REMOTE]);
    await widget.updateTarget(ConfigurationTarget.USER_REMOTE); expect(beforeChanges).toBe(1);

    const failed = f.widget({ enableRemoteSettings: true, beforeTargetChange: async () => { throw error; } });
    failed.settingsTarget = ConfigurationTarget.USER_LOCAL;
    const failedChanges = []; failed.onDidTargetChange(value => failedChanges.push(value));
    await expect(failed.userRemoteSettings.run()).rejects.toBe(error);
    expect(failed.settingsTarget).toBe(ConfigurationTarget.USER_LOCAL); expect(failedChanges).toEqual([]);
    failed.settingsTarget = ConfigurationTarget.USER_REMOTE;
    await expect(failed.folderSettingsAction.run(undefined)).rejects.toBe(error);
    expect(failed.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE);

    const immediate = f.widget();
    const result = immediate.updateTarget(ConfigurationTarget.USER_LOCAL);
    expect(immediate.settingsTarget).toBe(ConfigurationTarget.USER_LOCAL); await result;
  } finally { gate.resolve(); f.close(); }
});

scenario('原 Widget 等待 LOCAL 保存时再次点击当前 REMOTE，旧完成不能切走或发布旧事件', async () => {
  const f = fixture(), gate = Promise.withResolvers();
  let beforeChanges = 0;
  try {
    const widget = f.widget({ enableRemoteSettings: true, beforeTargetChange: async () => { beforeChanges++; await gate.promise; } });
    widget.settingsTarget = ConfigurationTarget.USER_REMOTE;
    const changes = []; widget.onDidTargetChange(value => changes.push(value));
    const local = widget.userLocalSettings.run();
    expect(beforeChanges).toBe(1); expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(changes).toEqual([]);
    await widget.userRemoteSettings.run();
    expect(beforeChanges).toBe(1);
    gate.resolve(); await local;
    expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(changes).toEqual([]);
  } finally { gate.resolve(); f.close(); }
});

scenario('原 Widget dispose 后迟到的前置保存不能改目标或发布目标变更', async () => {
  const f = fixture(), gate = Promise.withResolvers();
  let beforeChanges = 0;
  try {
    const widget = f.widget({ enableRemoteSettings: true, beforeTargetChange: async () => { beforeChanges++; await gate.promise; } });
    widget.settingsTarget = ConfigurationTarget.USER_REMOTE;
    const changes = []; widget.onDidTargetChange(value => changes.push(value));
    const local = widget.userLocalSettings.run();
    expect(beforeChanges).toBe(1); expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(changes).toEqual([]);
    widget.dispose(); gate.resolve(); await local;
    expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(changes).toEqual([]);
  } finally { gate.resolve(); f.close(); }
});

scenario('SourceSettings.inspect 驱动来源树，不读取窗口配置服务', () => {
  const source = Object.create(SourceSettings.prototype);
  source.location = { source_key: 'mutable_override' };
  source.fields = new Map([['agents.runtime.model', { path: ['agents', 'runtime', 'model'], schema: { default: 'schema-default' } }]]);
  source.model = { textEditorModel: { getValue: () => '{"agents":{"runtime":{"model":"draft-value"}}}' } };
  Object.defineProperty(source, 'document', { value: { writable: true,
    source_effective: { agents: { runtime: { model: 'effective-value' } } },
    layers: [{ source_key: 'inline', precedence: 0, payload: { agents: { runtime: { model: 'inline-default' } } } },
      { source_key: 'mutable_override', precedence: 1, payload: { agents: { runtime: { model: 'stored-value' } } } }] } });
  const f = fixture({ _serviceBrand: undefined, target: source });
  try {
    const model = f.model(ConfigurationTarget.USER_REMOTE);
    model.update({ id: 'root', label: 'Root', settings: [setting('agents.runtime.model', ConfigurationScope.MACHINE, false)] });
    const element = model.getElementsByName('agents.runtime.model')[0];
    element.inspectSelf();
    expect(element).toBeInstanceOf(tree.SettingsTreeSettingElement);
    expect(element.value).toBe('draft-value'); expect(element.scopeValue).toBe('draft-value');
    expect(element.defaultValue).toBe('inline-default'); expect(element.isConfigured).toBe(true);
    expect(element.isSourceReadOnly).toBe(false); expect(f.calls).toEqual([]);
  } finally { f.close(); }
});

scenario('inspectSetting 的语言 override 保留同一 folder resource，区分 A/B 文件夹正文', () => {
  const calls = [], a = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-A'), b = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-B');
  const configuration = { inspect(key, overrides) {
    calls.push({ key, overrides });
    const value = overrides.resource === a ? 'A' : 'B';
    return overrides.overrideIdentifier
      ? { defaultValue: false, workspaceFolderValue: value + '-javascript', workspaceFolder: { override: value + '-javascript' } }
      : { defaultValue: false, workspaceFolderValue: value, overrideIdentifiers: ['javascript', 'typescript'] };
  } };
  const resultA = tree.inspectSetting('editor.test', a, 'javascript', configuration);
  const resultB = tree.inspectSetting('editor.test', b, 'javascript', configuration);
  expect(resultA.targetSelector).toBe('workspaceFolderValue'); expect(resultA.isConfigured).toBe(true);
  expect(resultA.inspectedLanguageOverrides.get('javascript').workspaceFolderValue).toBe('A-javascript');
  expect(resultB.inspectedLanguageOverrides.get('javascript').workspaceFolderValue).toBe('B-javascript');
  expect(calls.map(call => call.overrides)).toEqual([
    { resource: a }, { resource: a, overrideIdentifier: 'javascript' }, { resource: a, overrideIdentifier: 'typescript' },
    { resource: b }, { resource: b, overrideIdentifier: 'javascript' }, { resource: b, overrideIdentifier: 'typescript' },
  ]);
});

scenario('来源设置 inspect 只读取当前来源层正文，不读取语言覆盖', () => {
  const source = Object.create(SourceSettings.prototype);
  source.location = { source_key: 'mutable_override' };
  source.fields = new Map([['agents.runtime.model', { path: ['agents', 'runtime', 'model'], schema: { default: 'schema-default' } }]]);
  source.model = { textEditorModel: { getValue: () => '{"agents":{"runtime":{"model":"draft-value"}}}' } };
  Object.defineProperty(source, 'document', { value: { source_effective: { agents: { runtime: { model: 'effective-value' } } },
    layers: [{ source_key: 'inline', precedence: 0, payload: { agents: { runtime: { model: 'inline-default' } } } },
      { source_key: 'mutable_override', precedence: 1, payload: { agents: { runtime: { model: 'stored-value' } } } }] } });
  expect(source.inspect('agents.runtime.model')).toEqual({ defaultValue: 'inline-default', userRemoteValue: 'draft-value',
    userRemote: { value: 'draft-value' }, value: 'draft-value', default: { value: 'inline-default' } });
  expect(source.inspect('agents.runtime.model', 'javascript')).toEqual({});
  expect(source.inspect('agents.runtime.unknown')).toEqual({});
});

scenario('来源设置元素从 owner 当前文档读取只读状态', () => {
  let writable = false;
  const source = { inspect: () => ({ defaultValue: 'schema', userRemoteValue: 'draft' }),
    get document() { return { writable }; } };
  const f = fixture({ _serviceBrand: undefined, target: source });
  try {
    const model = f.model(ConfigurationTarget.USER_REMOTE);
    model.update({ id: 'root', label: 'Root', settings: [setting('agents.runtime.model', ConfigurationScope.MACHINE)] });
    const element = model.getElementsByName('agents.runtime.model')[0]; element.inspectSelf();
    expect(element.isSourceReadOnly).toBe(true);
    writable = true;
    expect(element.isSourceReadOnly).toBe(false);
  } finally { f.close(); }
});

scenario('窗口设置树的语言值仍属于原 folder，两个 folder 的显示和 scopeValue 隔离', () => {
  const a = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-A'), b = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-B');
  const f = fixture(undefined, undefined, (_key, overrides) => {
    const value = overrides.resource === a ? 'A' : 'B';
    return overrides.overrideIdentifier
      ? { defaultValue: false, workspaceFolderValue: value + '-javascript', workspaceFolder: { override: value + '-javascript' } }
      : { defaultValue: false, workspaceFolderValue: value, overrideIdentifiers: ['javascript'] };
  });
  try {
    const shared = setting('editor.test', ConfigurationScope.LANGUAGE_OVERRIDABLE);
    const elements = [a, b].map(resource => {
      const model = f.model(resource, 'javascript'); model.update({ id: 'root', label: 'Root', settings: [shared] });
      const element = model.getElementsByName(shared.key)[0]; element.inspectSelf(); return element;
    });
    expect(elements.map(element => element.value)).toEqual(['A-javascript', 'B-javascript']);
    expect(elements.map(element => element.scopeValue)).toEqual(['A-javascript', 'B-javascript']);
    expect(elements.every(element => element.isConfigured)).toBe(true);
    expect(f.calls.map(call => call.overrides)).toEqual([
      { resource: a }, { resource: a, overrideIdentifier: 'javascript' },
      { resource: b }, { resource: b, overrideIdentifier: 'javascript' },
    ]);
  } finally { f.close(); }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-fingerprints.json'), JSON.stringify(fingerprints, null, 2));
  await run.writeResult(registered.length === passed.length ? 'passed' : 'failed', {
    boundary: '实际 settingsTreeScopeTransform 后的完整 SettingsTargetsWidget 与 settingsTreeModels 模块；保留原参数装饰器并经原 InstantiationService/ServiceCollection 构造 Model/SearchResultModel/Widget，使用原 Action、URI 和过滤函数。DOM、ActionBar、Folder Action View、配置 inspect 及外围服务为具名装具；不代替 SettingsEditor2、真实配置请求或页面。', registered, passed,
  });
  await run.close();
});
