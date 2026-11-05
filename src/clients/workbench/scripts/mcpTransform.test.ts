import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import ts from 'typescript';
import { sourceTransform, pinnedUpstreamCommit } from './sourceTransform.mjs';
import { mcpSourcePaths } from './mcpTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const sources = new Map(await Promise.all(mcpSourcePaths.map(async path => {
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

test.each(['AddConfigurationAction', 'InstallFromManifestAction', 'OpenUserMcpResourceCommand', 'OpenRemoteUserMcpResourceCommand', 'OpenWorkspaceMcpResourceCommand', 'OpenWorkspaceFolderMcpResourceCommand', 'SetOAuthClientSecret', 'RemoveStoredInput', 'EditStoredInput'])(
  '原 %s 命令 run 实际拒绝，不能触达旧配置、安装或凭据 writer', async kind => {
    const body = methodBody('mcpCommands.ts', kind, 'run');
    const run = new Function('unavailableWorkspaceMcpWrite', `return (async () => ${body})();`) as (unavailable: () => Error) => Promise<never>;
    const reason = new Error('未实现的 owner 写能力');
    await expect(run(() => reason)).rejects.toBe(reason);
    const node = declaration('mcpCommands.ts', kind);
    expect(node.getText()).toContain('id: McpCommandIds.');
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
  const run = new Function('return ' + js)() as (this: { workspaceMcpQueryFailed: boolean; currentQuery: string; input?: unknown }, query: string) => Promise<unknown>;
  const original = new Error('首次目录 503');
  const view = { workspaceMcpQueryFailed: false, currentQuery: '', input: undefined,
    mpcViewOptions: {}, query: async () => { throw original; } };
  await expect(run.call(view, '')).rejects.toBe(original);
  expect(view.workspaceMcpQueryFailed).toBe(true);
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
