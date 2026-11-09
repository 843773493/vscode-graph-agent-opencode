import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/clients/web/plugin_command_registry.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const files = [testFile, ...[
  'src/extensions/pluginCommandRegistry.ts', 'scripts/pluginCommandTransform.mjs', 'scripts/sourceTransform.mjs',
  'upstream/src/vs/platform/actions/common/actions.ts', 'upstream/src/vs/platform/action/common/action.ts',
  'upstream/src/vs/platform/commands/common/commands.ts', 'upstream/src/vs/platform/contextkey/common/contextkey.ts',
  'upstream/src/vs/platform/keybinding/common/keybindingsRegistry.ts', 'upstream/src/vs/platform/keybinding/common/keybindingResolver.ts',
  'upstream/src/vs/workbench/services/commands/common/commandService.ts',
  'upstream/src/vs/base/common/keybindingParser.ts', 'upstream/src/vs/base/common/keybindings.ts',
  'upstream/src/vs/base/common/keyCodes.ts', 'upstream/src/vs/base/common/platform.ts',
  'upstream/src/vs/base/common/lifecycle.ts', 'upstream/src/vs/base/common/event.ts',
].map(path => `src/clients/workbench/${path}`),
  'src/clients/web/src/types/protocol_generated/boxteam/gateway/v2/plugin_commands.ts',
  'src/clients/web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.ts'];
const sourceFingerprints = Object.fromEntries(await Promise.all(files.map(async path => [
  path, createHash('sha256').update(await readFile(path)).digest('hex'),
])));
const checks = [];

async function bundle(platform, filename) {
  const platformValues = { linux: [false, false, 'Linux'], mac: [false, true, 'Macintosh'], win: [true, false, 'Windows'] };
  const [windows, macintosh, operatingSystem] = platformValues[platform];
  const outfile = join(run.artifactsDir, filename);
  const built = await build({
    stdin: { contents: `
export { PluginCommandContributions, pluginCommandId, pluginGatewayContextKey, pluginWorkspaceContextKey,
  pluginCommandPaletteContext, pluginCommandPaletteActions } from './src/clients/workbench/src/extensions/pluginCommandRegistry.ts';
export { CommandsRegistry } from './src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
export { MenuId, MenuRegistry, isIMenuItem } from './src/clients/workbench/upstream/src/vs/platform/actions/common/actions.js';
export { MenuItemAction } from './src/clients/workbench/upstream/src/vs/platform/actions/common/actions.js';
export { KeybindingsRegistry } from './src/clients/workbench/upstream/src/vs/platform/keybinding/common/keybindingsRegistry.js';
export { ScanCodeChord } from './src/clients/workbench/upstream/src/vs/base/common/keybindings.js';
export { KeybindingParser } from './src/clients/workbench/upstream/src/vs/base/common/keybindingParser.js';
export { URI } from './src/clients/workbench/upstream/src/vs/base/common/uri.js';
export { CommandService } from './src/clients/workbench/upstream/src/vs/workbench/services/commands/common/commandService.js';
export { IExtensionService } from './src/clients/workbench/upstream/src/vs/workbench/services/extensions/common/extensions.js';
export { InstantiationService } from './src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from './src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
export { NullLogService } from './src/clients/workbench/upstream/src/vs/platform/log/common/log.js';
`, resolveDir: resolve('.') },
    outfile, bundle: true, format: 'esm', platform: 'node', target: 'es2024', metafile: true,
    tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent',
    plugins: [{ name: 'registry-platform-fixture', setup(builder) {
      builder.onLoad({ filter: /\/base\/common\/platform\.ts$/ }, async args => {
        let contents = await transform(args.path, await readFile(args.path, 'utf8'));
        contents = contents
          .replace('export const isWindows = _isWindows;', `export const isWindows = ${windows};`)
          .replace('export const isMacintosh = _isMacintosh;', `export const isMacintosh = ${macintosh};`)
          .replace('export const OS = (_isMacintosh || _isIOS ? OperatingSystem.Macintosh : (_isWindows ? OperatingSystem.Windows : OperatingSystem.Linux));',
            `export const OS = OperatingSystem.${operatingSystem};`);
        return { contents, loader: 'ts', resolveDir: dirname(args.path) };
      });
    } }, buildSourcePlugin(root, transform)],
  });
  await writeFile(join(run.artifactsDir, filename.replace(/\.mjs$/, '.meta.json')), JSON.stringify(built.metafile));
  return await import(pathToFileURL(outfile).href);
}

const m = await bundle('linux', 'plugin-command-registry-linux.mjs');
const a = { gateway_id: 'gateway-command-test', workspace_id: 'workspace-a' };
const b = { gateway_id: 'gateway-command-test', workspace_id: 'workspace-b' };
const commandId = 'run';
const packageId = 'example.commands';
const pluginId = 'same-plugin';

function publication(target, { revision, version, title, enablement, location, when, key, linuxKey = 'alt+[KeyL]',
  macKey = 'meta+[KeyM]', winKey = 'ctrl+[KeyW]', state = 'ready', error, instanceId } = {}) {
  const instance = instanceId ?? `${target.workspace_id}:${version}:${state}`;
  return {
    binding: { package_id: packageId, plugin_id: pluginId, package_version: version, workspace_id: target.workspace_id, enabled: true },
    revision, state, ...(error ? { error } : {}), instance_id: instance,
    commands: [{ id: commandId, title, handler: 'perform', enablement, arguments_schema: undefined, result_schema: undefined, required_capabilities: [] }],
    registrations: [{ command_id: commandId, handler: 'perform', registration_id: `${instance}:run` }],
    menus: [{ id: `${target.workspace_id}:menu`, location, command_id: commandId, group: 'navigation', order: 1, when }],
    keybindings: [{ id: `${target.workspace_id}:key`, command_id: commandId, key, mac: macKey, win: winKey, linux: linuxKey, when }],
  };
}

function snapshot(target, item) {
  return { gateway_id: target.gateway_id, workspace_id: target.workspace_id, publications: [item] };
}

function context(target, values = {}) {
  const all = { [m.pluginGatewayContextKey]: target.gateway_id, [m.pluginWorkspaceContextKey]: target.workspace_id, ...values };
  return { getValue: key => all[key] };
}

function commandEntries(menu, id) {
  return m.MenuRegistry.getMenuItems(menu).filter(item => m.isIMenuItem(item) && item.command.id === id);
}

function sourceFor(menu, id, workspaceId) {
  return commandEntries(menu, id).map(item => item.command)
    .find(action => action.pluginCommandArgs?.[0]?.target.workspace_id === workspaceId);
}

function keysFor(module, target, item) {
  const registry = new module.PluginCommandContributions(() => ({ gatewayId: target.gateway_id, workspaceId: target.workspace_id }), async () => undefined);
  registry.replace(target, { gateway_id: target.gateway_id, workspace_id: target.workspace_id, publications: [item] });
  return { registry, id: module.pluginCommandId(packageId, pluginId, commandId), items: module.KeybindingsRegistry.getDefaultKeybindings() };
}

test('同逻辑命令共用dispatcher并按目标隔离声明，拒绝无效更新且独立释放', async () => {
  const executions = [];
  let selectedOwner = { gatewayId: a.gateway_id, workspaceId: a.workspace_id };
  const registry = new m.PluginCommandContributions(() => selectedOwner,
    async (_accessor, source, args) => { executions.push({ target: source.target, title: source.command.title, args }); return source.target.workspace_id; });
  const itemA1 = publication(a, { revision: 'a-r1', version: '1.0.0', title: 'A command', enablement: 'editorTextFocus',
    location: 'editor/title', when: 'resourceScheme == file', key: 'ctrl+[KeyA]', linuxKey: 'alt+[KeyA]', macKey: 'meta+[KeyA]', winKey: 'ctrl+[KeyA]' });
  const itemB = publication(b, { revision: 'b-r1', version: '1.0.0', title: 'B command', enablement: 'editorHasSelection',
    location: 'editor/context', when: 'resourceExtname == .ts', key: 'shift+[KeyB]', linuxKey: 'ctrl+[KeyB]', macKey: 'meta+[KeyB]', winKey: 'ctrl+[KeyB]' });
  const id = m.pluginCommandId(packageId, pluginId, commandId);

  try {
    registry.replace(a, snapshot(a, itemA1));
    const dispatcher = m.CommandsRegistry.getCommand(id)?.handler;
    expect(dispatcher).toBeFunction();
    registry.replace(b, snapshot(b, itemB));
    expect(m.CommandsRegistry.getCommand(id)?.handler).toBe(dispatcher);

    const aTitle = sourceFor(m.MenuId.EditorTitle, id, a.workspace_id);
    const bTitle = sourceFor(m.MenuId.EditorContext, id, b.workspace_id);
    expect(aTitle?.title).toBe('A command');
    expect(bTitle?.title).toBe('B command');
    expect(sourceFor(m.MenuId.EditorContext, id, a.workspace_id)).toBeUndefined();
    expect(sourceFor(m.MenuId.EditorTitle, id, b.workspace_id)).toBeUndefined();
    expect(commandEntries(m.MenuId.CommandPalette, id).map(item => item.command.title).sort()).toEqual(['A command', 'B command']);

    const aContext = context(a, { editorTextFocus: true, resourceScheme: 'file', resourceExtname: '.ts' });
    const bContext = context(b, { editorHasSelection: true, resourceScheme: 'file', resourceExtname: '.ts' });
    expect(aTitle.precondition.evaluate(aContext)).toBe(true);
    expect(aTitle.precondition.evaluate(bContext)).toBe(false);
    expect(bTitle.precondition.evaluate(bContext)).toBe(true);
    expect(bTitle.precondition.evaluate(aContext)).toBe(false);
    expect(commandEntries(m.MenuId.EditorTitle, id).find(item => item.command.title === 'A command').when.evaluate(aContext)).toBe(true);
    expect(commandEntries(m.MenuId.EditorTitle, id).find(item => item.command.title === 'A command').when.evaluate(bContext)).toBe(false);
    expect(commandEntries(m.MenuId.EditorContext, id).find(item => item.command.title === 'B command').when.evaluate(bContext)).toBe(true);
    expect(commandEntries(m.MenuId.EditorContext, id).find(item => item.command.title === 'B command').when.evaluate(aContext)).toBe(false);
    const bindings = m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id);
    const bindingA = bindings.find(item => item.commandArgs.target.workspace_id === a.workspace_id);
    const bindingB = bindings.find(item => item.commandArgs.target.workspace_id === b.workspace_id);
    expect(bindingA.keybinding.chords[0].getHashCode()).toBe(m.KeybindingParser.parseKeybinding('alt+[KeyA]').chords[0].getHashCode());
    expect(bindingB.keybinding.chords[0].getHashCode()).toBe(m.KeybindingParser.parseKeybinding('ctrl+[KeyB]').chords[0].getHashCode());
    expect(bindingA.when.evaluate(aContext)).toBe(true);
    expect(bindingA.when.evaluate(bContext)).toBe(false);
    expect(bindingB.when.evaluate(bContext)).toBe(true);
    expect(bindingB.when.evaluate(aContext)).toBe(false);

    const menuCount = commandEntries(m.MenuId.CommandPalette, id).length;
    const keyCount = m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id).length;
    const firstA = sourceFor(m.MenuId.EditorTitle, id, a.workspace_id);
    registry.replace(a, snapshot(a, itemA1));
    expect(commandEntries(m.MenuId.CommandPalette, id)).toHaveLength(menuCount);
    expect(m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id)).toHaveLength(keyCount);
    expect(sourceFor(m.MenuId.EditorTitle, id, a.workspace_id)).toBe(firstA);

    for (const invalid of [
      { ...itemA1, menus: [{ ...itemA1.menus[0], when: 'editorTextFocus && (' }] },
      { ...itemA1, menus: [{ ...itemA1.menus[0], when: 'privateContext' }] },
      { ...itemA1, menus: [{ ...itemA1.menus[0], command_id: 'missing' }] },
      { ...itemA1, keybindings: [{ ...itemA1.keybindings[0], key: 'ctrl+UnknownKeyName' }] },
    ]) {
      expect(() => registry.replace(a, snapshot(a, invalid))).toThrow();
      expect(sourceFor(m.MenuId.EditorTitle, id, a.workspace_id)?.title).toBe('A command');
      expect(m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id)).toHaveLength(keyCount);
      selectedOwner = { gatewayId: a.gateway_id, workspaceId: a.workspace_id };
      expect(await m.CommandsRegistry.getCommand(id).handler({}, 'still-A')).toBe(a.workspace_id);
    }

    const itemA1b = structuredClone(itemA1);
    itemA1b.revision = 'a-r1b';
    itemA1b.binding.package_version = '1.0.1';
    registry.replace(a, snapshot(a, itemA1b));
    expect(m.CommandsRegistry.getCommand(id)?.handler).toBe(dispatcher);
    expect(() => dispatcher({}, firstA.pluginCommandArgs[0], 'old-version')).toThrow('已经失效');
    expect(executions.some(item => item.args.includes('old-version'))).toBe(false);

    const itemA2 = publication(a, { revision: 'a-r2', version: '2.0.0', title: 'A command updated', enablement: 'editorHasSelection',
      location: 'editor/context', when: 'resourceExtname == .md', key: 'ctrl+[KeyC]', linuxKey: 'ctrl+[KeyC]' });
    registry.replace(a, snapshot(a, itemA2));
    expect(sourceFor(m.MenuId.EditorTitle, id, a.workspace_id)).toBeUndefined();
    const updatedA = sourceFor(m.MenuId.EditorContext, id, a.workspace_id);
    expect(updatedA?.title).toBe('A command updated');
    expect(updatedA?.precondition.evaluate(context(a, { editorHasSelection: true }))).toBe(true);
    expect(updatedA?.precondition.evaluate(context(a, { editorTextFocus: true }))).toBe(false);
    const updatedBindings = m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id);
    expect(updatedBindings).toHaveLength(2);
    expect(updatedBindings.find(item => item.commandArgs.target.workspace_id === a.workspace_id).keybinding.chords[0].getHashCode())
      .toBe(m.KeybindingParser.parseKeybinding('ctrl+[KeyC]').chords[0].getHashCode());
    expect(m.CommandsRegistry.getCommand(id)?.handler).toBe(dispatcher);
    selectedOwner = { gatewayId: b.gateway_id, workspaceId: b.workspace_id };
    const beforeStale = executions.length;
    expect(() => dispatcher({}, firstA.pluginCommandArgs[0], 'stale')).toThrow('已经失效');
    expect(executions).toHaveLength(beforeStale);
    expect(executions.some(item => item.args.includes('stale'))).toBe(false);

    let latestA;
    for (const [state, error] of [['failed', '启动失败'], ['starting', undefined]]) {
      const unavailable = publication(a, { revision: `a-${state}`, version: '2.0.0', title: `A ${state}`,
        enablement: 'editorHasSelection', location: 'editor/context', when: 'resourceExtname == .md', key: 'ctrl+[KeyC]', state, error });
      registry.replace(a, snapshot(a, unavailable));
      const action = sourceFor(m.MenuId.EditorContext, id, a.workspace_id);
      latestA = action.pluginCommandArgs[0];
      expect(action?.precondition.evaluate(context(a, { editorHasSelection: true, resourceExtname: '.md' }))).toBe(false);
      expect(() => dispatcher({}, action.pluginCommandArgs[0], 'blocked')).toThrow('尚不可执行');
    }

    registry.remove(a);
    expect(sourceFor(m.MenuId.EditorContext, id, a.workspace_id)).toBeUndefined();
    expect(m.KeybindingsRegistry.getDefaultKeybindings().filter(item => item.command === id)).toHaveLength(1);
    expect(sourceFor(m.MenuId.EditorContext, id, b.workspace_id)?.title).toBe('B command');
    selectedOwner = { gatewayId: b.gateway_id, workspaceId: b.workspace_id };
    const beforeRemovedSource = executions.length;
    expect(() => dispatcher({}, latestA, 'removed-A-frozen')).toThrow('已经失效');
    expect(executions).toHaveLength(beforeRemovedSource);
    expect(await m.CommandsRegistry.getCommand(id).handler({}, 'only-B')).toBe(b.workspace_id);
    expect(executions.at(-1)).toMatchObject({ target: b, title: 'B command', args: ['only-B'] });
    selectedOwner = { gatewayId: a.gateway_id, workspaceId: a.workspace_id };
    expect(() => m.CommandsRegistry.getCommand(id).handler({}, 'removed-A')).toThrow('缺少唯一明确');
    checks.push('单dispatcher、目标菜单/标题/启用条件隔离、同快照幂等、无效快照预校验原子性、声明更新和冻结参数失效、不可执行状态及单目标释放');
  } finally {
    registry.dispose();
  }
});

test('实际MenuItemAction.run给插件传冻结source，普通菜单保留resource args；登记失败只回滚本次新增', async () => {
  const executions = [];
  const ownerA = { gatewayId: a.gateway_id, workspaceId: a.workspace_id };
  let currentOwner = ownerA;
  const activations = [];
  const legacyReads = [];
  const extensionService = {
    whenInstalledExtensionsRegistered: () => new Promise(() => {}),
    activationEventIsDone: () => false,
    activateByEvent: async event => { activations.push(event); },
    getExtensionHosts: () => { legacyReads.push('getExtensionHosts'); return []; },
    getHostExtensions: () => { legacyReads.push('getHostExtensions'); return []; },
  };
  const di = new m.InstantiationService(new m.ServiceCollection([m.IExtensionService, extensionService]), true);
  const commandService = new m.CommandService(di, extensionService, new m.NullLogService());
  const registry = new m.PluginCommandContributions(() => currentOwner,
    async (_accessor, source, args) => { executions.push({ source, args }); return source.target.workspace_id; });
  const itemA = publication(a, { revision: 'action-a', version: '1.0.0', title: 'Action A', enablement: 'editorTextFocus',
    location: 'editor/title', when: 'resourceScheme == file', key: 'ctrl+[KeyA]', linuxKey: 'alt+[KeyA]', macKey: 'meta+[KeyA]', winKey: 'ctrl+[KeyA]' });
  const itemB = publication(b, { revision: 'action-b', version: '1.0.0', title: 'Action B', enablement: 'editorTextFocus',
    location: 'editor/title', when: 'resourceScheme == file', key: 'ctrl+[KeyB]', linuxKey: 'alt+[KeyB]', macKey: 'meta+[KeyB]', winKey: 'ctrl+[KeyB]' });
  const id = m.pluginCommandId(packageId, pluginId, commandId);
  try {
    registry.replace(a, snapshot(a, itemA));
    registry.replace(b, snapshot(b, itemB));
    const pluginAction = sourceFor(m.MenuId.EditorTitle, id, a.workspace_id);
    const menuContext = { contextMatchesRules: expression => !expression || expression.evaluate(context(a, { editorTextFocus: true, resourceScheme: 'file' })) };
    const run = new m.MenuItemAction(pluginAction, undefined, { args: [m.URI.parse('file:///editor.ts')], shouldForwardArgs: true }, undefined, undefined, menuContext, commandService);
    const pluginResult = run.run('editor-context');
    currentOwner = { gatewayId: ownerA.gatewayId, workspaceId: 'workspace-b' };
    expect(await pluginResult).toBe(a.workspace_id);
    expect(executions[0].source.target.workspace_id).toBe(a.workspace_id);
    expect(executions[0].args).toEqual([]);
    expect(activations).toEqual([]);
    expect(legacyReads).toEqual([]);

    const ordinaryArgs = [], ordinaryId = 'ordinary.command';
    const ordinary = new m.MenuItemAction({ id: ordinaryId, title: 'Ordinary' }, undefined,
      { args: ['resource-default'], shouldForwardArgs: true }, undefined, undefined, { contextMatchesRules: () => true },
      { executeCommand: async (_command, ...args) => ordinaryArgs.push(...args) });
    await ordinary.run('editor-context');
    expect(ordinaryArgs).toEqual(['resource-default', 'editor-context']);
    const beforeRemoved = activations.length;
    const legacyReadsBeforeRemove = legacyReads.length;
    registry.remove(a);
    await expect(run.run('editor-context')).rejects.toThrow('已经失效');
    expect(executions).toHaveLength(1);
    expect(await commandService.executeCommand(id, 'still-B')).toBe(b.workspace_id);
    expect(executions.at(-1).source.target.workspace_id).toBe(b.workspace_id);
    expect(activations).toHaveLength(beforeRemoved);
    expect(legacyReads).toHaveLength(legacyReadsBeforeRemove);
  } finally { registry.dispose(); commandService.dispose(); di.dispose(); }

  const transactional = new m.PluginCommandContributions(() => ownerA, async (_accessor, source) => source.target.workspace_id);
  try {
    transactional.replace(a, snapshot(a, itemA));
    const before = commandEntries(m.MenuId.EditorTitle, id).map(action => action.command.pluginCommandArgs[0].target.workspace_id).sort();
    const append = m.MenuRegistry.appendMenuItem;
    let failed = false;
    m.MenuRegistry.appendMenuItem = function (...args) {
      if (!failed && args[0] === m.MenuId.EditorTitle) { failed = true; throw new Error('menu registration fixture failure'); }
      return append.apply(this, args);
    };
    try { expect(() => transactional.replace(b, snapshot(b, itemB))).toThrow('menu registration fixture failure'); }
    finally { m.MenuRegistry.appendMenuItem = append; }
    expect(commandEntries(m.MenuId.EditorTitle, id).map(action => action.command.pluginCommandArgs[0].target.workspace_id).sort()).toEqual(before);
    expect(sourceFor(m.MenuId.EditorTitle, id, a.workspace_id)?.title).toBe('Action A');
    expect(sourceFor(m.MenuId.EditorTitle, id, b.workspace_id)).toBeUndefined();
    expect(m.CommandsRegistry.getCommand(id)).toBeDefined();
    expect(await m.CommandsRegistry.getCommand(id).handler({}, 'sibling-still-runs')).toBe(a.workspace_id);
    transactional.replace(b, snapshot(b, itemB));
    expect(sourceFor(m.MenuId.EditorTitle, id, b.workspace_id)?.title).toBe('Action B');
    expect(commandEntries(m.MenuId.EditorTitle, id)).toHaveLength(2);
    checks.push('原MenuItemAction.run与真实CommandService只传冻结source且不混入resource参数，普通命令保留原args；明确登记故障仅回滚本目标、兄弟可执行且同快照可重装');
  } finally { transactional.dispose(); }
});

test('Quick Access上下文只冻结插件命令，原生菜单读取实时上下文', () => {
  const values = { editorTextFocus: true, editorHasSelection: false, resourceScheme: 'file' };
  const live = {
    getContextKeyValue: key => values[key],
    createOverlay(entries) {
      const overlay = new Map(entries);
      return { getContextKeyValue: key => overlay.has(key) ? overlay.get(key) : values[key] };
    },
  };
  const frozen = m.pluginCommandPaletteContext(live, {
    pluginCommandOwner: { gatewayId: a.gateway_id, workspaceId: a.workspace_id },
    pluginCommandContext: [['editorTextFocus', true], ['resourceScheme', 'file']],
  });
  values.editorTextFocus = false;
  values.resourceScheme = 'untitled';
  expect(live.getContextKeyValue('editorTextFocus')).toBe(false);
  expect(live.getContextKeyValue('resourceScheme')).toBe('untitled');
  expect(frozen.getContextKeyValue(m.pluginGatewayContextKey)).toBe(a.gateway_id);
  expect(frozen.getContextKeyValue(m.pluginWorkspaceContextKey)).toBe(a.workspace_id);
  expect(frozen.getContextKeyValue('editorTextFocus')).toBe(true);
  expect(frozen.getContextKeyValue('resourceScheme')).toBe('file');

  const native = new m.MenuItemAction({ id: 'native.command', title: 'Native' }, undefined, undefined, undefined, undefined,
    { contextMatchesRules: () => true }, { executeCommand: async () => undefined });
  const plugin = new m.MenuItemAction({ id: 'plugin.command', title: 'Plugin', pluginCommandArgs: [Object.freeze({})] }, undefined,
    undefined, undefined, undefined, { contextMatchesRules: () => true }, { executeCommand: async () => undefined });
  const seen = [];
  const actions = m.pluginCommandPaletteActions({ getMenuActions(_id, context) { seen.push(context); return [['commands', [native, plugin]]]; } }, live, frozen);
  expect(seen).toEqual([live, frozen]);
  expect(actions.map(([, items]) => items)).toEqual([[native], [plugin]]);
  checks.push('插件Quick Access上下文冻结Gateway/workspace及公开UI键；原生命令菜单仍使用实时ContextKeyService');
});

test('客户端平台键位选择及原KeybindingParser扫描码解析', async () => {
  const expected = [
    'linux', 'mac', 'win',
  ];
  for (const platform of expected) {
    const module = platform === 'linux' ? m : await bundle(platform, `plugin-command-registry-${platform}.mjs`);
    const target = { gateway_id: `gateway-${platform}`, workspace_id: `workspace-${platform}` };
    const item = publication(target, { revision: 'r1', version: '1.0.0', title: 'Scan code', enablement: 'editorTextFocus',
      location: 'editor/title', when: 'editorTextFocus', key: 'ctrl+[KeyA]' });
    const { registry, id, items } = keysFor(module, target, item);
    try {
      const matching = items.filter(binding => binding.command === id);
      expect(matching).toHaveLength(1);
      const chord = matching[0].keybinding.chords[0];
      expect(chord).toBeInstanceOf(module.ScanCodeChord);
      const expectedKey = platform === 'linux' ? 'alt+[KeyL]' : platform === 'mac' ? 'meta+[KeyM]' : 'ctrl+[KeyW]';
      const parsed = module.KeybindingParser.parseKeybinding(expectedKey).chords[0];
      expect(chord.equals(parsed)).toBe(true);
      expect(chord.ctrlKey).toBe(platform === 'win');
      expect(chord.altKey).toBe(platform === 'linux');
      expect(chord.metaKey).toBe(platform === 'mac');
    } finally { registry.dispose(); }
  }
  checks.push('Linux、macOS、Windows 分别选取各自键位声明；真实KeybindingParser保留扫描码及平台修饰键');
});

test('真实转换CommandService对自有命令绕过旧宿主发现，首次调用冻结owner且移除后即时拒绝', async () => {
  const platform = await bundle('linux', 'plugin-command-service.mjs');
  const targetA = { gateway_id: 'gateway-command-service', workspace_id: 'workspace-a' };
  const targetB = { gateway_id: 'gateway-command-service', workspace_id: 'workspace-b' };
  let currentOwner = targetA;
  const activations = [], calls = [];
  const extensionService = {
    whenInstalledExtensionsRegistered: () => new Promise(() => {}),
    activationEventIsDone: event => { activations.push(['activationEventIsDone', event]); return false; },
    activateByEvent: async event => { activations.push(['activateByEvent', event]); },
    getExtensionHosts: () => { activations.push(['getExtensionHosts']); return []; },
    getHostExtensions: () => { activations.push(['getHostExtensions']); return []; },
  };
  const di = new platform.InstantiationService(new platform.ServiceCollection([platform.IExtensionService, extensionService]), true);
  const commandService = new platform.CommandService(di, extensionService, new platform.NullLogService());
  const registry = new platform.PluginCommandContributions(() => ({ gatewayId: currentOwner.gateway_id, workspaceId: currentOwner.workspace_id }),
    async (_accessor, source, args) => { calls.push({ source, args }); return source.target.workspace_id; });
  const item = publication(targetA, { revision: 'r1', version: '1.0.0', title: 'Programmatic', enablement: undefined,
    location: 'commandPalette', when: undefined, key: undefined });
  const id = platform.pluginCommandId(packageId, pluginId, commandId);
  try {
    registry.replace(targetA, snapshot(targetA, item));
    const result = commandService.executeCommand(id, 'from-programmatic');
    currentOwner = targetB;
    expect(await result).toBe(targetA.workspace_id);
    expect(calls).toHaveLength(1);
    expect(calls[0].source.target).toEqual(targetA);
    expect(calls[0].args).toEqual(['from-programmatic']);
    expect(activations).toEqual([]);

    registry.remove(targetA);
    await expect(commandService.executeCommand(id, 'after-remove')).rejects.toThrow(`command '${id}' not found`);
    expect(activations).toEqual([]);
    expect(calls).toHaveLength(1);
    checks.push('sourceTransform后的真实CommandService.executeCommand经过DI直接分发并在任何await前捕获owner；移除后_tryExecuteCommand立即拒绝，不读取旧宿主或触发activation');
  } finally {
    registry.dispose();
    commandService.dispose();
    di.dispose();
  }
});

afterAll(async () => {
  const status = checks.length === 5 ? 'passed' : 'failed';
  const evidence = {
    boundary: '真实 PluginCommandContributions、原 CommandsRegistry/MenuRegistry/KeybindingsRegistry、sourceTransform 后的上游源码；平台模块值为显式 Linux/macOS/Windows 构建装具。未经过浏览器、Gateway、工作区后端或真实多目标网络。',
    upstreamCommit: pinnedUpstreamCommit,
    checks,
    sourceFingerprints,
  };
  await writeFile(join(run.artifactsDir, 'plugin-command-registry-evidence.json'), `${JSON.stringify(evidence, null, 2)}\n`);
  await run.writeResult(status, evidence);
  await run.close();
});
