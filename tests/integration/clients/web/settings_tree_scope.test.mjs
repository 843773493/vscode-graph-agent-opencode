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

scenario('目标 Widget 使用真实 DI、冻结名称和目标 USER_REMOTE，不依赖全窗口 remoteAuthority', async () => {
  const f = fixture(selected('Linux A'));
  try {
    const widget = f.widget();
    expect(widget.userLocalSettings.label).toBe('当前工作台 profile');
    expect(widget.userLocalSettings.tooltip).toBe('当前工作台 profile');
    expect(widget.userRemoteSettings.label).toBe('目标 profile · Linux A');
    expect(widget.userRemoteSettings.tooltip).toBe('目标 profile · Linux A');
    expect(widget.userRemoteSettings.enabled).toBe(true);
    const targets = []; widget.onDidTargetChange(value => targets.push(value));
    await widget.userRemoteSettings.run();
    expect(widget.settingsTarget).toBe(ConfigurationTarget.USER_REMOTE); expect(targets).toEqual([ConfigurationTarget.USER_REMOTE]);
    widget.setResultCount(ConfigurationTarget.USER_LOCAL, 4);
    expect(widget.userLocalSettings.label).toBe('当前工作台 profile (4)');
    widget.updateLanguageFilterIndicators('javascript');
    expect(widget.userLocalSettings.label).toBe('当前工作台 profile [JavaScript]');
    expect(widget.userRemoteSettings.label).toBe('目标 profile · Linux A [JavaScript]');
    expect(f.hostLabels).toEqual([]); expect(f.environment.remoteAuthority).toBeUndefined();
    expect(f.widget({ enableRemoteSettings: false }).userRemoteSettings.enabled).toBe(false);
  } finally { f.close(); }
});

scenario('目标名称缺省展示冻结 backend；默认 Widget 保留原窗口标签和远程资格', () => {
  const target = fixture(selected(''), 'other-host'), local = fixture(), remote = fixture(undefined, 'ssh-remote+host');
  try {
    expect(target.widget().userRemoteSettings.label).toBe('目标 profile · backend-A'); expect(target.hostLabels).toEqual([]);
    const localWidget = local.widget(), remoteWidget = remote.widget();
    expect(localWidget.userLocalSettings.label).toBe('User'); expect(localWidget.userRemoteSettings.label).toBe('Remote');
    expect(localWidget.userRemoteSettings.enabled).toBe(false);
    expect(remoteWidget.userRemoteSettings.label).toBe('Remote [Host A]'); expect(remoteWidget.userRemoteSettings.enabled).toBe(true);
    remoteWidget.setResultCount(ConfigurationTarget.USER_LOCAL, 3); expect(remoteWidget.userLocalSettings.label).toBe('User (3)');
    expect(remote.environment.remoteAuthority).toBe('ssh-remote+host');
  } finally { target.close(); local.close(); remote.close(); }
});

scenario('原 Widget 切换前等待写入结算，失败保留当前目标且不发布目标变更', async () => {
  const f = fixture(selected('Linux A')), gate = Promise.withResolvers(), error = new Error('原设置写入失败');
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
  const f = fixture(selected('Linux A')), gate = Promise.withResolvers();
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
  const f = fixture(selected('Linux A')), gate = Promise.withResolvers();
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

scenario('真实 SettingsTreeModel 把冻结 target 传给 element；目标扩展 APPLICATION 可见且值从 USER_REMOTE 读取', () => {
  const f = fixture(selected('Linux A'), undefined, () => ({ defaultValue: false, applicationValue: 'window application', userRemoteValue: 'target profile' }));
  try {
    const application = setting('sample.application', ConfigurationScope.APPLICATION);
    const builtin = setting('builtin.application', ConfigurationScope.APPLICATION, false);
    const model = f.model(ConfigurationTarget.USER_REMOTE);
    model.update({ id: 'extensions', label: 'Extensions', settings: [application, builtin] });
    const element = model.getElementsByName(application.key)[0], builtinElement = model.getElementsByName(builtin.key)[0];
    expect(element).toBeInstanceOf(tree.SettingsTreeSettingElement);
    expect(element.matchesScope(ConfigurationTarget.USER_REMOTE, false)).toBe(true);
    expect(builtinElement.matchesScope(ConfigurationTarget.USER_REMOTE, false)).toBe(false);
    element.inspectSelf();
    expect(element.value).toBe('target profile'); expect(element.scopeValue).toBe('target profile'); expect(element.isConfigured).toBe(true);
    model.updateElementsByName(application.key); expect(element.value).toBe('target profile');
    expect(f.calls.every(call => call.key === application.key && call.overrides === undefined)).toBe(true);
  } finally { f.close(); }
});

scenario('默认树保留原 APPLICATION 检查规则和 USER_REMOTE 过滤；目标 USER_LOCAL 仍读当前窗口 profile', () => {
  const inspect = () => ({ defaultValue: false, applicationValue: 'application', userRemoteValue: 'remote' });
  const unscoped = fixture(undefined, undefined, inspect), scoped = fixture(selected('Linux A'), undefined, inspect);
  try {
    const application = setting('sample.application', ConfigurationScope.APPLICATION);
    const original = unscoped.model(ConfigurationTarget.USER_REMOTE), local = scoped.model(ConfigurationTarget.USER_LOCAL);
    for (const model of [original, local]) model.update({ id: 'root', label: 'Root', settings: [application] });
    const defaultElement = original.getElementsByName(application.key)[0], localElement = local.getElementsByName(application.key)[0];
    expect(defaultElement.matchesScope(ConfigurationTarget.USER_REMOTE, false)).toBe(false);
    defaultElement.inspectSelf(); localElement.inspectSelf();
    expect(defaultElement.value).toBe('application'); expect(localElement.value).toBe('application');
  } finally { unscoped.close(); scoped.close(); }
});

scenario('原 SearchResultModel 继承 DI 和 scope，真实结果过滤不会丢掉目标扩展 APPLICATION', () => {
  const scoped = fixture(selected('Linux A')), unscoped = fixture();
  try {
    const application = setting('sample.application', ConfigurationScope.APPLICATION), resource = setting('sample.resource', ConfigurationScope.RESOURCE);
    const scopedModel = scoped.search(ConfigurationTarget.USER_REMOTE), defaultModel = unscoped.search(ConfigurationTarget.USER_REMOTE);
    const result = { filterMatches: [application, resource].map(setting => ({ setting, matchType: 0, keyMatchScore: 0, score: 0, matches: [] })) };
    scopedModel.setResult(tree.SearchResultIdx.Local, result); defaultModel.setResult(tree.SearchResultIdx.Local, result);
    expect(scopedModel.root.children.map(child => child.setting.key)).toEqual([application.key, resource.key]);
    expect(defaultModel.root.children.map(child => child.setting.key)).toEqual([resource.key]);
    expect(scopedModel.getUniqueResultsCount()).toBe(2); expect(defaultModel.getUniqueResultsCount()).toBe(1);
    const element = scopedModel.getElementsByName(application.key)[0]; element.inspectSelf(); expect(element.value).toBe(true);
  } finally { scoped.close(); unscoped.close(); }
});

scenario('目标上下文的当前工作台 tab 隐藏所有扩展贡献键，默认窗口原搜索过滤保留', () => {
  const scoped = fixture(selected('Linux A')), unscoped = fixture();
  try {
    const extensionApplication = setting('sample.application', ConfigurationScope.APPLICATION);
    const extensionResource = setting('sample.resource', ConfigurationScope.RESOURCE);
    const extensionUnspecified = setting('sample.unspecified', undefined);
    const builtin = setting('workbench.application', ConfigurationScope.APPLICATION, false);
    const result = { filterMatches: [extensionApplication, extensionResource, extensionUnspecified, builtin]
      .map(setting => ({ setting, matchType: 0, keyMatchScore: 0, score: 0, matches: [] })) };
    for (const target of [ConfigurationTarget.USER_LOCAL, ConfigurationTarget.APPLICATION]) {
      const model = scoped.search(target); model.setResult(tree.SearchResultIdx.Local, result);
      expect(model.root.children.map(child => child.setting.key)).toEqual([builtin.key]);
    }
    const original = unscoped.search(ConfigurationTarget.USER_LOCAL); original.setResult(tree.SearchResultIdx.Local, result);
    expect(original.root.children.map(child => child.setting.key)).toEqual([
      extensionApplication.key, extensionResource.key, extensionUnspecified.key, builtin.key,
    ]);
  } finally { scoped.close(); unscoped.close(); }
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

scenario('真实树 element.inspectSelf 的语言值仍属于原 folder，两个 folder 的显示和scopeValue隔离', () => {
  const a = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-A'), b = URI.parse('boxteam-file://gateway-A/workspace-A/root/folder-B');
  const f = fixture(selected('Linux A'), undefined, (_key, overrides) => {
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
