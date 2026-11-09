import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import ts from 'typescript';
import { sourceTransform, pinnedUpstreamCommit } from './sourceTransform.mjs';
import { mcpSourcePaths } from './mcpTransform.mjs';
import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError, getErrorMessage } from '../upstream/src/vs/base/common/errors.js';
import { DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const sources = new Map(await Promise.all([...mcpSourcePaths,
  'src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts',
  'src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts',
  'src/vs/workbench/contrib/welcomeWalkthrough/browser/editor/vs_code_editor_walkthrough.ts',
  'src/vs/workbench/browser/parts/editor/editorStatus.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsQuickAccess.ts',
].map(async path => {
  const full = join(root, 'upstream', path);
  const transformed = await transform(full, await readFile(full, 'utf8'));
  return [path.split('/').at(-1)!, ts.createSourceFile(full, transformed, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)] as const;
})));
function source(name: string) { return sources.get(name)!; }
function declaration(name: string, kind: string) {
  const node = source(name).statements.find(node => ts.isClassDeclaration(node) && node.name?.text === kind);
  if (!node || !ts.isClassDeclaration(node)) throw new Error(`原声明缺失：${kind}`);
  return node;
}
function methodBody(name: string, kind: string, method: string) {
  const node = declaration(name, kind).members.find(node => ts.isMethodDeclaration(node) && node.name.getText() === method);
  if (!node || !ts.isMethodDeclaration(node) || !node.body) throw new Error(`原方法缺失：${kind}.${method}`);
  return node.body.getText();
}

test('实际插件管理注册只提供自有入口，搜索和安装回执仍沿原服务', async () => {
  const parsed = source('extensions.contribution.ts');
  const actions = parsed.statements.filter(node => !ts.isImportDeclaration(node) && !ts.isVariableStatement(node)
    && !(ts.isExpressionStatement(node) && (node.expression.getText().startsWith('registerSingleton(')
      || node.expression.getText().startsWith('Registry.'))));
  const commands = new Map<string, (accessor: object, query?: string) => unknown>();
  const workbench = Symbol('workbench'), views = Symbol('views');
  const calls: unknown[] = [];
  let accessorValid = true;
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(actions.map(node => node.getText()).join('\n'));
  new Function('CommandsRegistry', 'Action2', 'registerAction2', 'IExtensionsWorkbenchService', 'IViewsService',
    'localize2', 'refreshIcon', 'MenuId', 'ContextKeyExpr', 'VIEWLET_ID', 'installServicePluginPackage', javascript)(
    { registerCommand(id: string, handler: (accessor: object, query?: string) => unknown) { commands.set(id, handler); } },
    class { constructor(readonly options: { id: string }) {} },
    (Action: new () => { options: { id: string }; run(accessor: object): unknown }) => {
      const action = new Action(); commands.set(action.options.id, accessor => action.run(accessor));
    }, workbench, views, (_key: string, value: string) => ({ value, original: value }), {}, { ViewContainerTitle: 'title' },
    { equals: () => ({}) }, 'extensions', async () => { calls.push('install'); accessorValid = false; },
  );
  expect([...commands.keys()].sort()).toEqual([
    'boxteam.plugins.installPackage', 'workbench.extensions.action.clearExtensionsSearchResults',
    'workbench.extensions.action.focusExtensionsView', 'workbench.extensions.action.installedExtensions',
    'workbench.extensions.action.refreshExtension', 'workbench.extensions.search',
  ].sort());
  const failure = new Error('原刷新失败');
  let rejectRefresh = false;
  const accessor = { get(id: symbol) {
    if (!accessorValid) throw new Error('ServicesAccessor 已随同步命令调用结束');
    if (id === workbench) return { openSearch(query: string) { calls.push(query); } };
    expect(id).toBe(views);
    return { getActiveViewPaneContainerWithId: () => ({ async refresh() { calls.push('refresh'); if (rejectRefresh) throw failure; } }) };
  } };
  await commands.get('workbench.extensions.search')!(accessor, '@mcp');
  await commands.get('workbench.extensions.action.installedExtensions')!(accessor);
  await commands.get('boxteam.plugins.installPackage')!(accessor);
  expect(calls).toEqual(['@mcp', '', 'install', 'refresh']);
  accessorValid = true;
  rejectRefresh = true;
  await expect(commands.get('workbench.extensions.action.refreshExtension')!(accessor)).rejects.toBe(failure);
});

test('欢迎页的插件按钮可打开自有包，教程保留内置能力而不宣传旧市场', () => {
  const parsed = source('gettingStartedContent.ts');
  const cards: { id: string; title: string; description: string; media: { path: string } }[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isObjectLiteralExpression(node)) {
      const id = node.properties.find(property => ts.isPropertyAssignment(property) && property.name.getText(parsed) === 'id');
      if (id && ts.isPropertyAssignment(id) && ts.isStringLiteral(id.initializer)
          && ['extensionsWebWeb', 'extensions'].includes(id.initializer.text)) {
        const expression = new Bun.Transpiler({ loader: 'ts' }).transformSync(`const step = ${node.getText(parsed)};`);
        cards.push(new Function('localize', 'Button', `${expression}\nreturn step;`)(
          (_key: string, text: string, button: string) => text.replace('{0}', button ?? ''),
          (text: string, command: string) => `[${text}](${command})`,
        ));
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  expect(cards.map(card => card.id).sort()).toEqual(['extensions', 'extensionsWebWeb']);
  for (const card of cards) {
    expect(card.title).toBe('管理插件包');
    const command = /\]\(command:([^)]+)\)/.exec(card.description)?.[1];
    expect(command).toBe('workbench.extensions.action.installedExtensions');
    expect(source('extensions.contribution.ts').text).toContain(`'${command}'`);
    expect(card.media.path).toBe('empty');
  }
  expect(parsed.text).not.toContain('command:workbench.extensions.action.show');
  const walkthrough = source('vs_code_editor_walkthrough.ts');
  const content = walkthrough.statements.find(ts.isFunctionDeclaration)!;
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(content.getText(walkthrough).replace('export default ', ''));
  const render = new Function('platform', 'IWorkbenchEnvironmentService', `${javascript}\nreturn content;`)(
    { isWeb: true }, 'environment',
  ) as (accessor: object) => string;
  const markdown = render({ get: () => ({ remoteAuthority: undefined }) });
  expect(markdown).toContain('### IntelliSense');
  expect(markdown).toContain('context.strokeStyle');
  expect(markdown).toContain('command:workbench.action.openGlobalSettings');
  expect(markdown).toContain('command:workbench.action.openSnippets');
  expect(markdown).not.toContain('command:workbench.extensions.');
  expect(markdown).not.toContain('extension gallery');
});

test('语言选择器保留语言和配置选项，既不查询市场也不提供失效搜索', async () => {
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(
    `class Action { async run(accessor) ${methodBody('editorStatus.ts', 'ChangeLanguageAction', 'run')} }`,
  );
  const ids = ['IQuickInputService', 'IEditorService', 'ILanguageService', 'ILanguageDetectionService',
    'ITextFileService', 'IPreferencesService', 'IConfigurationService', 'ITelemetryService'];
  const Action = new Function(...ids, 'getCodeEditor', 'EditorResourceAccessor', 'SideBySideEditor', 'Schemas',
    'getIconClassesForLanguageId', 'extname', 'basename', 'localize', javascript + '\nreturn Action;')(
    ...ids, (control: object) => control, { getOriginalUri: () => ({ scheme: 'boxteam-file', path: '/source.ts' }) },
    { PRIMARY: 1 }, { untitled: 'untitled' }, () => [], () => '.ts', () => 'source.ts',
    (_key: string, message: string, ...args: string[]) => message.replace(/\{(\d+)\}/g, (_, index) => args[Number(index)] ?? ''),
  ) as new () => { run(accessor: object): Promise<void> };
  let picks: { id?: string; label: string }[] = [];
  const services: Record<string, object> = {
    IQuickInputService: { async pick(items: typeof picks) { picks = items; return undefined; } },
    IEditorService: { activeTextEditorControl: { getModel: () => ({ getLanguageId: () => 'typescript', getValueLength: () => 0 }) } },
    ILanguageService: { getLanguageName: () => 'TypeScript', getSortedRegisteredLanguageNames: () => [{ languageName: 'TypeScript', languageId: 'typescript' }],
      getExtensions: () => ['.ts'] },
    ILanguageDetectionService: {}, ITextFileService: {}, IPreferencesService: {}, IConfigurationService: {}, ITelemetryService: {},
  };
  await new Action().run({ get(id: string) {
    if (!(id in services)) throw new Error(`语言选择器不能访问旧市场服务：${id}`);
    return services[id];
  } });
  expect(picks.some(pick => pick.id === 'typescript')).toBe(true);
  expect(picks.some(pick => pick.label.includes('Configure File Association'))).toBe(true);
  expect(picks.some(pick => pick.label.includes('language based settings'))).toBe(true);
  expect(picks.some(pick => /marketplace|extension/i.test(pick.label))).toBe(false);
});

test('ext快速访问仅保留原管理入口，不再包含旧市场安装实现或注册', () => {
  const parsed = source('extensionsQuickAccess.ts');
  expect(parsed.statements.filter(ts.isClassDeclaration).map(node => node.name?.text)).toEqual(['ManageExtensionsQuickAccessProvider']);
  const contribution = source('extensions.contribution.ts');
  const quickAccess: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && node.expression.getText(contribution).endsWith('.registerQuickAccessProvider')) {
      quickAccess.push(node.arguments[0]!.getText(contribution));
    }
    ts.forEachChild(node, visit);
  };
  visit(contribution);
  expect(quickAccess).toHaveLength(1);
  expect(quickAccess[0]).toContain('ManageExtensionsQuickAccessProvider');
  expect(contribution.text).not.toContain('InstallExtensionQuickAccessProvider');
});

test('浏览器内置管理只返回system和custom内置项，不再查询已退役的用户安装域', async () => {
  const path = join(root, 'src/extensions/nativeManagement.ts');
  const parsed = ts.createSourceFile(path, await readFile(path, 'utf8'), ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WebExtensionManagementService');
  if (!owner || !ts.isClassDeclaration(owner)) throw new Error('内置扩展管理服务缺失');
  const method = owner.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(parsed) === 'getInstalled');
  if (!method) throw new Error('内置扩展清单入口缺失');
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class Service { ${method.getText(parsed)} }`);
  const Service = new Function('ExtensionType', 'toLocalExtension', javascript + '\nreturn Service;')(
    { System: 0, User: 1 }, (extension: object) => extension,
  ) as new () => { getInstalled(type?: number): Promise<{ identifier: string; installationTarget: object }[]> };
  const service = new Service(), calls: string[] = [];
  Object.assign(service, { assertProfile: () => {}, profiles: { currentProfile: { id: 'original-profile' } }, scanner: {
    scanSystemExtensions: async () => { calls.push('system'); return [{ identifier: 'system' }]; },
    scanCustomBuiltinExtensions: async () => { calls.push('custom'); return [{ identifier: 'custom' }]; },
  }, readTargetOptions: () => { throw new Error('不能发现旧安装域'); } });
  expect((await service.getInstalled()).map(item => item.identifier)).toEqual(['system', 'custom']);
  expect(calls.splice(0)).toEqual(['system', 'custom']);
  expect((await service.getInstalled(0)).map(item => item.identifier)).toEqual(['system']);
  expect(calls.splice(0)).toEqual(['system']);
  const user = await service.getInstalled(1);
  expect(user.map(item => item.identifier)).toEqual(['custom']);
  expect(user[0]!.installationTarget).toEqual({ kind: 'web', profileId: 'original-profile' });
  expect(calls).toEqual(['custom']);
});

test('实际转换只注册一个项目 MCP 服务，原列表／详情继续注册，第二发现与工具 client 不再实例化', () => {
  const registrations: string[][] = [];
  const collect = (node: ts.Node) => {
    if (ts.isCallExpression(node) && ['registerSingleton', 'registerAction2', 'registerWorkbenchContribution2'].includes(node.expression.getText())) {
      registrations.push([node.expression.getText(), ...node.arguments.map(argument => argument.getText())]);
    }
    ts.forEachChild(node, collect);
  };
  collect(source('mcp.contribution.ts'));
  expect(registrations.filter(call => call[0] === 'registerSingleton' && call[1] === 'IMcpService')).toEqual([
    ['registerSingleton', 'IMcpService', 'WorkspaceMcpService', 'InstantiationType.Delayed'],
  ]);
  expect(registrations.filter(call => call[0] === 'registerSingleton' && call[1] === 'IMcpWorkbenchService')).toEqual([
    ['registerSingleton', 'IMcpWorkbenchService', 'WorkspaceMcpWorkbenchService', 'InstantiationType.Eager'],
  ]);
  expect(registrations).toContainEqual(['registerAction2', 'ListMcpServerCommand']);
  expect(registrations).toContainEqual(['registerAction2', 'McpServerOptionsCommand']);
  expect(registrations.some(call => call.some(value => /McpDiscovery|McpLanguageModelToolContribution|McpGatewayToolBrokerContribution/.test(value)))).toBe(false);
  expect(source('mcp.contribution.ts').text).toContain('EditorPaneDescriptor.create(');
});

test('动态MCP Provider的扩展API在浏览器与Node宿主拒绝注册，不发布到原MCP注册表', async () => {
  const apiSource = source('extHost.api.impl.ts');
  const nodeRuntimeTransform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime: 'node' });
  const apiPath = join(root, 'upstream/src/vs/workbench/api/common/extHost.api.impl.ts');
  const nodeRuntimeSource = ts.createSourceFile(apiPath,
    await nodeRuntimeTransform(apiPath, await readFile(apiPath, 'utf8')),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const getBody = (parsed: ts.SourceFile): string => {
    let body: ts.Block | undefined;
    const visit = (node: ts.Node) => {
      if (ts.isMethodDeclaration(node) && node.name.getText(parsed) === 'registerMcpServerDefinitionProvider') body = node.body;
      ts.forEachChild(node, visit);
    };
    visit(parsed);
    if (!body) throw new Error('动态 MCP Provider API 入口缺失');
    return body.getText(parsed);
  };
  const browserBody = getBody(apiSource);
  const nodeBody = getBody(nodeRuntimeSource);
  expect(browserBody).not.toContain('registerMcpConfigurationProvider');
  expect(nodeBody).not.toContain('registerMcpConfigurationProvider');

  for (const body of [browserBody, nodeBody]) {
    const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(
      'function registerMcpServerDefinitionProvider(id: string, _provider: object) ' + body,
    );
    let registryCalls = 0;
    const register = new Function('extHostMcp', javascript + '\nreturn registerMcpServerDefinitionProvider;')(
      { registerMcpConfigurationProvider() { registryCalls += 1; } },
    ) as (id: string, provider: object) => never;
    expect(() => register('legacy-provider', {})).toThrow('已停用');
    expect(registryCalls).toBe(0);
  }
});

test.each(['InstallFromManifestAction', 'OpenUserMcpResourceCommand', 'OpenRemoteUserMcpResourceCommand', 'OpenWorkspaceMcpResourceCommand', 'OpenWorkspaceFolderMcpResourceCommand', 'SetOAuthClientSecret', 'RemoveStoredInput', 'EditStoredInput'])(
  '原 %s 命令 run 实际拒绝，不能触达旧配置、安装或凭据 writer', async kind => {
    const body = methodBody('mcpCommands.ts', kind, 'run');
    const run = new Function('unavailableWorkspaceMcpWrite', `return (async () => ${body})();`) as (unavailable: () => Error) => Promise<never>;
    const reason = new Error('未实现的 owner 写能力');
    await expect(run(() => reason)).rejects.toBe(reason);
    const node = declaration('mcpCommands.ts', kind);
    expect(node.getText()).toContain('id: McpCommandIds.');
  },
);

test('原 Add Action 原样转交显式 URI，同一 seam 承接命令、Chat 和列表入口，旧 writer 不可达', async () => {
  const calls: unknown[][] = [];
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`function run(accessor: object, configUri?: object | string) ${methodBody('mcpCommands.ts', 'AddConfigurationAction', 'run')}`);
  const run = new Function('addWorkspaceMcpConfiguration', javascript + '\nreturn run;')(
    async (...args: unknown[]) => { calls.push(args); },
  ) as (accessor: object, configUri?: object | string) => Promise<void>;
  const accessor = {}, folder = { scheme: 'boxteam-file', path: '/exact-root' };
  await run(accessor);
  await run(accessor, folder);
  await run(accessor, 'file:///exact/.vscode/mcp.json');
  expect(calls).toEqual([[accessor, undefined], [accessor, folder], [accessor, 'file:///exact/.vscode/mcp.json']]);
  const imports = source('mcpCommands.ts').statements.filter(ts.isImportDeclaration).map(node => node.moduleSpecifier.getText());
  expect(imports.some(value => value.includes('mcpCommandsAddConfiguration'))).toBe(false);
  expect(source('mcpCommands.ts').text).not.toContain('createInstance(McpAddConfigurationCommand');
});

test('实际Add注册能从F1显示，不受已移除的Chat setup门控，也不提供未支持的旧文件菜单', () => {
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(declaration('mcpCommands.ts', 'AddConfigurationAction').getText().replace('export ', ''));
  const Add = new Function('Action2', 'McpCommandIds', 'localize2', 'category', javascript + '\nreturn AddConfigurationAction;')(
    class { constructor(readonly options: object) {} }, { AddConfiguration: 'original-add' },
    (_key: string, text: string) => text, 'MCP',
  ) as new () => { options: { id: string; f1: boolean; title: string; precondition?: object; menu?: object } };
  const action = new Add();
  expect(action.options).toMatchObject({ id: 'original-add', f1: true, title: 'Add Server...' });
  expect(action.options.precondition).toBeUndefined(); expect(action.options.menu).toBeUndefined();
});

test('实际List注册能从F1进入原列表，ChatInput菜单仍保留原展示条件', () => {
  const constructor = declaration('mcpCommands.ts', 'ListMcpServerCommand').members.find(ts.isConstructorDeclaration)!;
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class List extends Action2 { ${constructor.getText()} }`);
  const chatHidden = {}, chatDisabled = {};
  const expression = { and: (...values: unknown[]) => ({ and: values }), or: (...values: unknown[]) => ({ or: values }), equals: () => ({}) };
  const List = new Function('Action2', 'McpCommandIds', 'localize2', 'Codicon', 'category', 'ContextKeyExpr',
    'mcpAutoStartConfig', 'McpAutoStartValue', 'McpContextKeys', 'ChatContextKeys', 'ChatModeKind', 'MenuId', javascript + '\nreturn List;')(
    class { constructor(readonly options: object) {} }, { ListServer: 'original-list' }, (_key: string, text: string) => text,
    { server: {} }, 'MCP', expression, 'autostart', { Never: 'never' }, { hasUnknownTools: {}, hasServersWithErrors: {} },
    { Setup: { hidden: { negate: () => chatHidden }, disabledInWorkspace: { negate: () => chatDisabled } },
      chatModeKind: { isEqualTo: () => ({}) }, lockedToCodingAgent: { negate: () => ({}) } }, { Agent: 'agent' }, { ChatInput: 'chat-input' },
  ) as new () => { options: { id: string; f1: boolean; title: string; precondition?: object; menu: { id: string; when: { and: unknown[] } }[] } };
  const action = new List();
  expect(action.options).toMatchObject({ id: 'original-list', f1: true, title: 'List Servers' });
  expect(action.options.precondition).toBeUndefined();
  expect(action.options.menu[0]!.id).toBe('chat-input');
  expect(action.options.menu[0]!.when.and).toContainEqual({ and: [chatHidden, chatDisabled] });
});

test.each([false, true])('原列表恢复 Add 行并等待同一命令；有现存Server=%j时默认选择精确Server', async hasServer => {
  class ProjectServer {
    readonly collection = { order: 0, id: 'group', label: '原工作区' };
    readonly definition = { id: 'existing-server', label: '原 Server' };
    readonly enablement = { read: () => 0 };
    readonly statusLabel = 'disabled';
  }
  const server = new ProjectServer();
  class ProjectService {
    readonly local = hasServer ? [server] : [];
    readonly servers = { read: () => this.local };
    async activateCollections() {}
    observe() { return { dispose() {} }; }
  }
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`async function run(services: object) ${methodBody('mcpCommands.ts', 'ListMcpServerCommand', '_runLocal')}`);
  const run = new Function('WorkspaceMcpService', 'WorkspaceMcpServer', 'DisposableStore', 'autorun', 'groupBy', 'localize',
    'ThemeIcon', 'Codicon', 'isContributionDisabled', 'McpConnectionState', 'McpCommandIds', javascript + '\nreturn run;')(
    ProjectService, ProjectServer, DisposableStore, (callback: (reader: object) => void) => { callback({}); return { dispose() {} }; },
    (items: typeof server[]) => items.length ? { group: items } : {}, (_key: string, text: string) => text,
    { asClassName: () => 'add-icon' }, { add: {} }, () => false, { toString: () => 'unused' }, { AddConfiguration: 'original-add' },
  ) as (services: object) => Promise<void>;
  let accept = () => {}, initial: object[] = [], complete = () => {}, settled = false;
  const commandCalls: unknown[][] = [];
  const pick = { items: [] as { id: string }[], activeItems: [] as { id: string }[],
    onDidAccept(callback: () => void) { accept = callback; return { dispose() {} }; },
    onDidHide() { return { dispose() {} }; }, dispose() {},
    show() { initial = [...this.activeItems]; this.activeItems = [this.items[0]!]; queueMicrotask(accept); },
  };
  const running = run({ mcpService: new ProjectService(), quickInput: { createQuickPick: () => pick },
    commandService: { executeCommand: (...args: unknown[]) => { commandCalls.push(args); return new Promise<void>(resolve => { complete = resolve; }); } },
  }).then(() => { settled = true; });
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(pick.items[0]!.id).toBe('$add');
  expect(initial).toEqual(hasServer ? [{ id: 'existing-server', label: '原 Server', description: 'disabled' }] : []);
  expect(commandCalls).toEqual([['original-add']]); expect(settled).toBe(false);
  complete(); await running; expect(settled).toBe(true);
});

test('原 Browse 与 BrowsePage 都交给同一个 MCP 查询 seam，不扩大为 All Sources 查询', async () => {
  const serviceId = Symbol('IMcpWorkbenchService');
  const calls: string[] = [];
  const accessor = { get(id: symbol) { expect(id).toBe(serviceId); return { openSearch: async (text: string) => { calls.push(text); } }; } };
  for (const kind of ['McpBrowseCommand', 'McpBrowsePageCommand']) {
    const run = new Function('IMcpWorkbenchService', `return async function(accessor) ${methodBody('mcpCommands.ts', kind, 'run')};`)(serviceId) as (accessor: object) => Promise<void>;
    await run(accessor);
  }
  expect(calls).toEqual(['', '']);
});

test('新增原 MCP 搜索视图的查询拒绝进入既有通知通道，不产生未处理拒绝遮住整个工作台', async () => {
  const body = methodBody('extensionsViewlet.ts', 'ExtensionsViewPaneContainer', 'onDidAddViewDescriptors');
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class Container extends Base { onDidAddViewDescriptors(added: object[]) ${body} }`);
  const Container = new Function('Base', javascript + '\nreturn Container;')(class { onDidAddViewDescriptors(added: object[]) { return added; } }) as
    new () => { onDidAddViewDescriptors(added: object[]): object[]; showExtensionsViews(added: object[]): Promise<void>; onError(error: Error): void };
  const container = new Container();
  const failure = new Error('目录未配置');
  const received: Error[] = [];
  container.showExtensionsViews = async () => { throw failure; };
  container.onError = error => { received.push(error); };
  const added = [{}];
  expect(container.onDidAddViewDescriptors(added)).toBe(added);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(received).toEqual([failure]);
});

test('插件包刷新保留原 MCP 列表查询，错误交回原命令，不再检查 VSIX 更新', async () => {
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`async function refresh() ${methodBody('extensionsViewlet.ts', 'ExtensionsViewPaneContainer', 'refresh')}`);
  const refresh = new Function(javascript + '\nreturn refresh;')() as (this: object) => Promise<void>;
  const events: unknown[] = [];
  const panes = [{}];
  const failure = new Error('目录 HTTP 503');
  await expect(refresh.call({ panes,
    getView: (id: string) => { expect(id).toBe('boxteam.views.extensionPackages'); return { refresh: async () => { events.push('packages'); } }; },
    showExtensionsViews: async (views: object[]) => { expect(views).toBe(panes); events.push('mcp'); throw failure; },
  })).rejects.toBe(failure);
  expect(events).toEqual(['packages', 'mcp']);
});

test.each([
  ['@mcp server', true, false], ['@agentPlugins name', false, true], ['my-package', false, false],
  ['@installed @mcp server', true, false], ['@installed @agentPlugins name', false, true],
  ['package@mcp', false, false], ['@mcp-suffix', false, false],
])(
  '原搜索入口 %s 在同一容器分派并保留查询', async (query, mcp, agentPlugins) => {
    const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`function search(value: string) ${methodBody('extensionsViewlet.ts', 'ExtensionsViewPaneContainer', 'search')}`);
    const search = new Function(javascript + '\nreturn search;')() as (this: object, query: string) => void;
    const values: Record<string, unknown> = {};
    const key = (name: string) => ({ set: (value: unknown) => { values[name] = value; } });
    const views = [{}];
    search.call({ panes: views, queryValue: '', contextKeyService: { bufferChangeEvents: (run: () => void) => run() },
      searchMcpServersContextKey: key('mcp'), searchAgentPluginsContextKey: key('agentPlugins'), defaultViewsContextKey: key('default'),
      searchHasTextContextKey: key('hasText'), extensionsSearchValueContextKey: key('query'),
      getView: () => ({ search: (value: string) => { values.packages = value; } }),
      showExtensionsViews: async (received: object[]) => { expect(received).toBe(views); values.shown = true; },
      onError: (error: Error) => { throw error; },
    }, query);
    await Promise.resolve();
    expect(values).toEqual({ mcp, agentPlugins, default: false, hasText: true, query, packages: query, shown: true });
  },
);

test('原活动栏进度结束时只清理自身，原查询 Promise 仍拒绝且不另生未处理拒绝', async () => {
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class Progress {
    withPaneCompositeProgress(paneCompositeId: string, viewContainerLocation: number, task: () => Promise<unknown>, options: object) ${methodBody('progressService.ts', 'ProgressService', 'withPaneCompositeProgress')}
    showOnActivityBar(viewletId: string, options: object, promise: Promise<unknown>) ${methodBody('progressService.ts', 'ProgressService', 'showOnActivityBar')}
  }`);
  let time = 0;
  const events: string[] = [];
  const Progress = new Function('ViewContainerLocation', 'ProgressBadge', 'dispose', 'Date', javascript + '\nreturn Progress;')(
    { Sidebar: 0 }, class {}, (value: { dispose(): void } | undefined) => value?.dispose(), { now: () => time += 400 },
  ) as new () => { paneCompositeService: object; activityService: object; withPaneCompositeProgress(id: string, location: number, task: () => Promise<unknown>, options: object): Promise<unknown> };
  const progress = new Progress();
  progress.paneCompositeService = { getProgressIndicator: () => undefined };
  progress.activityService = { showViewContainerActivity: () => { events.push('show'); return { dispose: () => events.push('dispose') }; } };
  let fail!: (error: Error) => void;
  const task = new Promise<never>((_resolve, reject) => { fail = reject; });
  const returned = progress.withPaneCompositeProgress('extensions', 0, () => task, { delay: 1 });
  expect(returned).toBe(task);
  const failure = new Error('目录 HTTP 503');
  const rejection = returned.then(() => undefined, error => error);
  await new Promise(resolve => setTimeout(resolve, 10));
  fail(failure);
  expect(await rejection).toBe(failure);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(events).toEqual(['show', 'dispose']);
});

test.each(['当前续页失败', '已取消查询', '已替换模型', '取消错误', '旧条目晚到'])(
  '原分页 renderer 的%s沿当前 owner 处理，不覆盖新来源或产生未处理拒绝', async state => {
    const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(declaration('listPaging.ts', 'PagedRenderer').getText());
    const Renderer = new Function('CancellationTokenSource', 'isCancellationError', javascript + '\nreturn PagedRenderer;')(
      CancellationTokenSource, isCancellationError,
    ) as new (renderer: object, modelProvider: () => object, onResolveError: (error: unknown) => void) => {
      renderElement(index: number, height: number, data: { data: object; disposable?: { dispose(): void } }): void;
    };
    let fail!: (error: Error) => void;
    let complete!: (entry: object) => void;
    const pending = new Promise<object>((resolve, reject) => { complete = resolve; fail = reject; });
    const model = { isResolved: () => false, resolve: () => pending };
    let current: object = model;
    const messages: unknown[] = [];
    const notifications: unknown[] = [];
    const rendered: object[] = [];
    const body = declaration('mcpServersView.ts', 'McpServersListView').members.find(node => ts.isMethodDeclaration(node) && node.name.getText() === 'renderBody')!;
    let callback: ts.ArrowFunction | undefined;
    const findCallback = (node: ts.Node) => {
      if (ts.isPropertyAssignment(node) && node.name.getText() === 'onResolveError' && ts.isArrowFunction(node.initializer)) callback = node.initializer;
      ts.forEachChild(node, findCallback);
    };
    findCallback(body);
    if (!callback) throw new Error('原列表续页错误出口缺失');
    const onResolveError = new Function('getErrorMessage', 'Severity', `return function(error) ${callback.body.getText()};`)(getErrorMessage, { Error: 3 }) as (this: object, error: unknown) => void;
    const view = { updateBody: (message: unknown) => messages.push(message), notificationService: { error: (error: unknown) => notifications.push(error) } };
    const renderer = new Renderer({ renderPlaceholder() {}, renderElement: (entry: object) => rendered.push(entry) }, () => current, error => onResolveError.call(view, error));
    const data: { data: object; disposable?: { dispose(): void } } = { data: {} };
    renderer.renderElement(50, 72, data);
    if (state === '已取消查询') data.disposable!.dispose();
    if (state === '已替换模型' || state === '旧条目晚到') current = {};
    const failure = state === '取消错误' ? new CancellationError() : new Error('目录续页 HTTP 503');
    if (state === '旧条目晚到') complete({ id: 'old-source' });
    else fail(failure);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(messages).toEqual(state === '当前续页失败' ? [{ text: failure.message, severity: 3 }] : []);
    expect(notifications).toEqual(state === '当前续页失败' ? [failure] : []);
    expect(rendered).toEqual([]);
    data.disposable!.dispose();
  },
);

test('guard 迁移严格限定原 Start/Stop，未迁移的安装／配置动作保留 local 类型收窄', () => {
  for (const kind of ['StartServerAction', 'StopServerAction']) {
    expect(methodBody('mcpServerActions.ts', kind, 'getServer')).not.toContain('!this.mcpServer.local');
  }
  for (const kind of ['UninstallAction', 'ShowServerJsonConfigurationAction']) {
    const node = declaration('mcpServerActions.ts', kind);
    expect(node.getText()).toContain('!this.mcpServer.local');
  }
  expect(methodBody('mcpServerActions.ts', 'StopServerAction', 'update')).toContain('server.isPending');
  expect(methodBody('mcpServerActions.ts', 'AuthServerAction', 'run')).toContain('await server.authorize()');
});

test('目录条目即使与工作区连接同 ID，原 Start/Stop/Authorize 也不能操作该连接', async () => {
  let authorized = 0;
  class ProjectServer {
    readonly id = 'shared-id';
    readonly definition = { id: this.id };
    readonly canAuthorize = true;
    async authorize() { ++authorized; }
  }
  const connection = new ProjectServer();
  const service = { servers: { get: () => [connection] } };
  const gallery = { id: connection.id };
  for (const kind of ['StartServerAction', 'StopServerAction']) {
    const getServer = new Function('WorkspaceMcpServer', `return function() ${methodBody('mcpServerActions.ts', kind, 'getServer')};`)(ProjectServer) as (this: object) => unknown;
    expect(getServer.call({ mcpServer: gallery, mcpService: service })).toBeUndefined();
    expect(getServer.call({ mcpServer: connection, mcpService: service })).toBe(connection);
  }
  const updateBody = new Bun.Transpiler({ loader: 'ts' }).transformSync(`function update() ${methodBody('mcpServerActions.ts', 'AuthServerAction', 'update')}`);
  const runBody = new Bun.Transpiler({ loader: 'ts' }).transformSync(`async function run() ${methodBody('mcpServerActions.ts', 'AuthServerAction', 'run')}`);
  const { update, run } = new Function('WorkspaceMcpServer', updateBody + runBody + '\nreturn { update, run };')(ProjectServer) as {
    update(this: object): void; run(this: object): Promise<void>;
  };
  const action = { mcpServer: gallery as object, enabled: true, class: '' };
  update.call(action);
  expect(action.enabled).toBe(false);
  await expect(run.call(action)).rejects.toThrow('原 MCP 目标已移除');
  expect(authorized).toBe(0);
  action.mcpServer = connection;
  update.call(action);
  expect(action.enabled).toBe(true);
  await run.call(action);
  expect(authorized).toBe(1);
});

test('原 renderer 状态刷新与成员删除使用最新目录，详情读取同一模型并观察可见性', () => {
  const renderer = methodBody('mcpServersView.ts', 'McpServerRenderer', 'renderElement');
  expect(renderer).toContain('data.description.textContent = mcpServer.description;\n\t\t\t\tupdateEnablement();');
  const merge = methodBody('mcpServersView.ts', 'McpServersListView', 'mergeChangedMcpServers');
  expect(merge).not.toContain('newMcpServers.some(r => r.name === oldMcpServer.name)');
  expect(methodBody('mcpServerEditor.ts', 'McpServerEditor', 'renderNavbar')).toContain('new Cache(() => extension.getReadme(token))');
  expect(methodBody('mcpServerEditor.ts', 'McpServerEditor', 'setEditorVisible')).toContain('server.observe()');
});

test('首次目录失败保留原 show 拒绝，恢复只由同一 owner 的可用目录变化触发', async () => {
  const body = methodBody('mcpServersView.ts', 'McpServersListView', 'show');
  const js = new Bun.Transpiler({ loader: 'ts' }).transformSync(`async function run(query: string) ${body}`);
  const run = new Function('CancellationTokenSource', 'isCancellationError', 'getErrorMessage', 'Severity', 'return ' + js)(
    CancellationTokenSource, isCancellationError, getErrorMessage, { Error: 3 },
  ) as (this: { workspaceMcpQueryFailed: boolean; currentQuery: string; input?: unknown }, query: string) => Promise<unknown>;
  const original = new Error('首次目录 503');
  const view = { workspaceMcpQueryFailed: false, currentQuery: '', input: undefined,
    queryCancellation: undefined as CancellationTokenSource | undefined,
    updateBody(message: { text: string }) { expect(message.text).toBe(original.message); },
    mpcViewOptions: {}, query: async () => { throw original; } };
  await expect(run.call(view, '')).rejects.toBe(original);
  expect(view.workspaceMcpQueryFailed).toBe(true);
  view.queryCancellation?.dispose(true);
  const constructor = declaration('mcpServersView.ts', 'McpServersListView').members.find(ts.isConstructorDeclaration)!;
  expect(constructor.getText()).toContain('this.mcpWorkbenchService.isLocalCatalogAvailable');
  expect(constructor.getText()).toContain('this.workspaceMcpQueryFailed');
  expect(declaration('mcpWorkbenchService.ts', 'MCPContextsInitialisation').getText()).toContain('mcpWorkbenchService.whenInitialLocalMcpServersLoaded.then(');
  expect(declaration('mcpWorkbenchService.ts', 'MCPContextsInitialisation').getText()).not.toContain('queryLocal().finally(');
});

test('项目原列表自身接回 Extensions 隐藏作用域，恢复后隐藏旧错误且普通原视图不改作用域', async () => {
  const body = methodBody('mcpServersView.ts', 'McpServersListView', 'renderBody');
  const js = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class View extends Base { renderBody(container: HTMLElement) ${body} }`);
  const stop = new Error('样式作用域已就绪，后续 DOM 创建由产品验收覆盖');
  const View = new Function('Base', 'dom', `${js}\nreturn View;`)(class { renderBody() {} }, {
    $: () => ({}), append: () => { throw stop; },
  }) as new () => { id: string; element: { classList: { add(token: string): void } }; renderBody(container: object): void };
  const classes = new Set<string>();
  const view = new View();
  view.id = 'boxteam.mcp.view';
  view.element = { classList: { add: token => { classes.add(token); } } };
  expect(() => view.renderBody({})).toThrow(stop);
  expect(classes.has('extensions-viewlet')).toBe(true);
  classes.clear();
  view.id = 'workbench.mcp.installed';
  expect(() => view.renderBody({})).toThrow(stop);
  expect(classes.size).toBe(0);

  const update = new Function('alert', `return function(message) ${methodBody('mcpServersView.ts', 'McpServersListView', 'updateBody')};`)(() => {}) as
    (this: object, message?: unknown) => void;
  const messageClasses = new Set<string>();
  const toggle = (token: string, added: boolean) => { if (added) messageClasses.add(token); else messageClasses.delete(token); };
  update.call({ input: { model: { length: 2 } }, isBodyVisible: () => false, bodyTemplate: {
    mcpServersList: { classList: { toggle() {} } }, messageContainer: { classList: { toggle } },
  } });
  expect(messageClasses.has('hidden')).toBe(true);
  const css = await readFile(join(root, 'upstream/src/vs/workbench/contrib/extensions/browser/media/extensionsViewlet.css'), 'utf8');
  expect(css).toMatch(/\.extensions-viewlet \.hidden\s*\{\s*display:\s*none !important;\s*visibility:\s*hidden;/);
});
