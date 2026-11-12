import { afterAll, describe, expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/scoped_query.test.mjs').prepare();
const workbench = resolve('src/clients/workbench');
const transform = sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []);
const bundle = join(run.artifactsDir, 'query.fixture.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/query.fixture.ts')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(workbench, 'tsconfig.jsonc'), plugins: [buildSourcePlugin(workbench, transform)], logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const scope = { version: 1, workspaces: [{ ...owner, rootUri: root, label: '工作区 A' }] };
const uri = (name = '', target = owner) => m.workspaceFileUri(target, root + (name ? '/' + name : ''), root);

// 这些模块明确整文件生成，不消费上游正文中的文本锚点。
const generatedModules = new Map([
  ['src/vs/workbench/contrib/chat/common/plugins/agentPluginServiceImpl.ts', 'export class AgentPluginService'],
  ['src/vs/workbench/contrib/chat/browser/chat.view.contribution.ts', ''],
  ['src/vs/workbench/services/extensionManagement/common/webExtensionManagementService.ts', 'export { WebExtensionManagementService };'],
  ['src/vs/workbench/services/extensionManagement/common/extensionManagementService.ts', 'export { ExtensionManagementService };'],
  ['src/vs/workbench/services/extensionManagement/common/extensionManagementServerService.ts', 'export { ExtensionManagementServerService };'],
  ['src/vs/workbench/contrib/extensions/browser/extensionsQuickAccess.ts', 'ManagePluginQuickAccessProvider as ManageExtensionsQuickAccessProvider'],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/pluginCustomizationMarketplaceProvider.ts', 'export {}\n'],
  ['src/vs/workbench/contrib/chat/browser/agentPluginsView.ts', 'export {};\n'],
]);
// AST 删除、模板插值与 NLS 改写按语义验证，不把整份原文重复伪称为单锚点冲突。
const structuralModules = new Map([
  ['src/vs/workbench/contrib/chat/browser/chatSlashCommands.ts', ["command: 'plugins'", 'registerSlashCommand']],
  ['src/vs/platform/product/common/product.ts', ['/*BUILD->INSERT_PRODUCT_CONFIGURATION*/', pinnedUpstreamCommit]],
  ['src/vs/workbench/contrib/mcp/common/mcpTypes.ts', ['queryGallery(', 'IMcpWorkbenchService']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/toolsListWidget.ts', ['createInstalledPluginItem', 'selection: file.range']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationDiscoveryPage.ts', ['getPluginCustomizationMarketplaceSourceId', 'marketplace?.sourceId === this.selectedSourceId']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/pluginListWidget.ts', ['showMarketplaceActions', "executeCommand<boolean>('workbench.action.chat.installPluginFromSource')"]],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplace.contribution.ts', ["localize('customizationDiscovery.help.overview'", 'customizationMarketplace.contribution#customizationDiscovery.help.overview']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationCategories.ts', ['PluginMarketplaces', 'CustomizationMigrationCategoryId.McpServers']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationAccessibility.ts', ['migrationHelpPluginMarketplaces', 'migrationHelpOverview']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationDashboard.ts', ['PluginMarketplaces', 'CustomizationMigrationCategoryId.McpServers']],
  ['src/vs/workbench/contrib/chat/common/promptSyntax/service/customizationMigrationService.ts', ['PluginMarketplaces', 'migrateMcpServers', '缺少 ICustomizationMigrationService']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigration.ts', ['isPluginMarketplaceCustomizationMigrationCandidate', 'isMcpServerCustomizationMigrationCandidate']],
]);
// 这两个转换核边界存在后生成正文／裁去尾部，重复尾部不会留下第二套实现。
const boundaryModules = new Map([
  ['src/vs/workbench/contrib/mcp/browser/mcpWorkbenchService.ts', ['上游 MCP 初始上下文贡献缺失', 'export class MCPContextsInitialisation']],
  ['src/vs/workbench/contrib/chat/browser/widget/chatAgentHover.ts', ['Chat Agent 扩展详情悬浮入口缺失', 'export class ChatAgentHover']],
]);
// 唯一 AST owner 和具名边界保留自己的拒绝原因，其余文本 seam 必须明确报告“恰好一次”。
const anchorErrors = new Map([
  ['src/vs/workbench/contrib/chat/browser/actions/chatPluginActions.ts', ['缺少 provider 插件来源安装动作', 'InstallFromSourceAction precondition锚点数量错误']],
  ['src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts', ['原扩展管理 owner 不唯一：WebExtensionsScannerService', '原扩展管理 owner 不唯一：WebExtensionsScannerService']],
  ['src/vs/workbench/contrib/mcp/browser/mcpCommands.ts', ['上游 MCP 命令 seam 缺失：McpAgentHostServerOptionsCommand']],
  ['src/vs/workbench/contrib/mcp/browser/mcpServerActions.ts', ['上游 MCP InstallAction 边界缺失']],
  ['src/vs/workbench/contrib/mcp/browser/mcpServerEditor.ts', ['上游 MCP 详情 action import 锚点缺失']],
  ['src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts', ['原扩展管理 owner 不唯一：ExtensionsViewletViewsContribution', '原扩展管理 owner 不唯一：ExtensionsViewletViewsContribution']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts', ['缺少 CustomizationMarketplaceInstallService']],
  ['src/vs/platform/customizationMarketplace/electron-browser/customizationMarketplaceService.ts', ['上游 MCP 转换边界缺失']],
  ['src/vs/workbench/services/extensions/browser/extensionService.ts', ['内置 Worker 转换边界缺失']],
  ['src/vs/workbench/browser/parts/statusbar/statusbarActions.ts', ['待删除的旧扩展详情类缺失：ManageExtensionAction', '待删除的旧扩展详情类缺失：ManageExtensionAction']],
  ['src/vs/workbench/contrib/extensions/browser/extensionsActions.ts', ['按 Extension ID 搜索的旧 Workbench 命令与固定上游不匹配', '按 Extension ID 搜索的旧 Workbench 命令与固定上游不匹配']],
  ['src/vs/workbench/contrib/remoteTunnel/electron-browser/remoteTunnel.contribution.ts', ['Remote Tunnel 的旧扩展详情动作数量或结构不符', 'Remote Tunnel 的旧扩展详情动作数量或结构不符']],
  ['src/vs/workbench/services/extensionManagement/common/extensionManagement.ts', ['原扩展管理 owner 不唯一：IWebExtensionsScannerService', '原扩展管理 owner 不唯一：IWebExtensionsScannerService']],
  ['src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts', ['欢迎页插件步骤与固定上游不匹配', '欢迎页插件步骤与固定上游不匹配']],
  ['src/vs/workbench/contrib/notebook/browser/contrib/cellStatusBar/statusBarProviders.ts', ['Notebook 未知语言的旧扩展市场入口与固定上游不匹配', 'Notebook 未知语言的旧扩展市场入口与固定上游不匹配']],
  ['src/vs/workbench/api/common/extHostLanguageFeatures.ts', ['CodeActionAdapter 原文档 owner 构造入口已变化']],
  ['src/vs/workbench/contrib/chat/browser/widget/input/editor/chatInputCompletions.ts', ['聊天扩展安装补全登记数量不符：0', '聊天扩展安装补全登记数量不符：2']],
  ['src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationServiceImpl.ts', ['缺少 CustomizationMigrationService']],
  ['src/vs/workbench/services/preferences/common/preferencesEditorInput.ts', ['设置转换锚点缺失：export class SettingsEditor2Input']],
  ['src/vs/workbench/contrib/preferences/browser/preferences.contribution.ts', ['设置转换锚点缺失：\tserialize(input: SettingsEditor2Input)']],
]);
const newlineReceipts = [];
afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-newlines.json'), JSON.stringify({ commit: pinnedUpstreamCommit,
    receipts: newlineReceipts.sort((left, right) => left.path.localeCompare(right.path)) }, null, 2) + '\n');
});

function fixture(context = new m.SearchWorkspaceContext(scope), groupService = { groups: [] }) {
  const configuration = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
    search: { useIgnoreFiles: false, useGlobalIgnoreFiles: false, useParentIgnoreFiles: false, followSymlinks: true } };
  const services = new m.ServiceCollection(
    [m.IConfigurationService, { getValue: () => configuration }], [m.IWorkspaceContextService, context],
    [m.IEditorGroupsService, groupService], [m.ILogService, new m.NullLogService()],
    [m.IPathService, { userHome: () => m.URI.parse('file:///home/native') }],
    [m.IUriIdentityService, { extUri: m.extUri, onDidChange: m.Event.None }],
  );
  const instantiation = new m.InstantiationService(services, true);
  return { context, instantiation, builder: instantiation.createInstance(m.QueryBuilder) };
}

describe('生产 sourceTransform 与原 QueryBuilder 范围接线', () => {
  test('显式转接保留原归属及接收者，新入口独立捕获，不隐式继承旧错误或接受动作', () => {
    const editors = { activeEditor: new m.ThreadInput({ ...owner, sessionId: 'session-a', threadId: 'thread-a', title: 'A', routeId: '' }) };
    const accept = () => {}, additions = [{ label: '原附件' }];
    let focused = true;
    const context = { getContextKeyValue: key => key === 'editorTextFocus' ? focused : undefined };
    const initial = m.prepareSearchInvocation({ providerOptions: { handleAccept: accept, additionPicks: additions, includeHelp: true },
      enabledProviderPrefixes: ['', '?', '%'] }, editors, context);
    focused = false;
    const other = { ...owner, workspaceId: 'workspace-b' };
    editors.activeEditor = new m.ThreadInput({ ...other, sessionId: 'session-b', threadId: 'thread-b', title: 'B', routeId: '' });
    const transfer = m.prepareSearchInvocation({ ...initial, preserveValue: true,
      providerOptions: { ...initial.providerOptions, includeHelp: false } }, editors, context);
    expect(transfer.providerOptions.searchOwner).toEqual(owner);
    expect(transfer.providerOptions.pluginCommandOwner).toEqual(owner);
    expect(new Map(transfer.providerOptions.pluginCommandContext).get('editorTextFocus')).toBe(true);
    expect(transfer.providerOptions.handleAccept).toBe(accept);
    expect(transfer.providerOptions.additionPicks).toBe(additions);
    expect(transfer.providerOptions.includeHelp).toBe(false);
    expect(transfer.enabledProviderPrefixes).toEqual(['', '?', '%']);
    const fresh = m.prepareSearchInvocation(undefined, editors, context);
    expect(fresh.providerOptions.searchOwner).toEqual(other);
    expect(fresh.providerOptions.pluginCommandOwner).toEqual(other);
    expect(new Map(fresh.providerOptions.pluginCommandContext).get('editorTextFocus')).toBe(false);
    expect(fresh.providerOptions.handleAccept).toBeUndefined();
    expect(fresh.enabledProviderPrefixes).toBeUndefined();
    const failed = { providerOptions: { searchOwnerError: '发起身份已失效' } };
    const continued = m.prepareSearchInvocation(failed, editors, context);
    expect(continued.providerOptions.searchOwnerError).toBe('发起身份已失效');
    expect(continued.providerOptions.searchOwner).toBeUndefined();
    expect(fresh.providerOptions.searchOwnerError).toBeUndefined();
    const selected = [owner, other];
    const multi = m.prepareSearchInvocation({ providerOptions: { searchOwners: selected } }, { activeEditor: undefined }, context);
    expect(multi.providerOptions.searchOwners).toBe(selected);
    expect(multi.providerOptions.searchOwner).toBeUndefined();
    expect(multi.providerOptions.searchOwnerError).toBeUndefined();
  });

  test('预览恢复等待关闭时可释放 Provider，迟到恢复不清除下一次定位状态', async () => {
    for (const dispose of [true, false]) {
      let release;
      const closed = new Promise(resolve => { release = resolve; });
      const original = { id: 'original' }, next = { id: 'next' }, restored = [];
      const preview = { isDirty: () => false };
      const group = { isTransient: editor => editor === preview, closeEditor: () => closed,
        openEditor: async editor => { restored.push(editor.id); } };
      const editors = { activeEditorPane: { input: original, group, getControl: () => undefined },
        openEditor: async () => ({ input: preview, group }) };
      const state = new m.PickerEditorState(editors, { groups: [group] });
      state.set();
      await state.openTransientEditor({ resource: m.URI.parse('workbench:/preview') });
      const restoring = state.restore();
      if (dispose) state.dispose();
      else { editors.activeEditorPane.input = next; state.set(); }
      release();
      await restoring;
      expect(restored).toEqual(['original']);
      await state.restore();
      expect(restored).toEqual(dispose ? ['original'] : ['original', 'next']);
      state.dispose();
    }
  });

  test.each(transformedSourcePaths)('LF／CRLF 转换字节一致且检出原件不变：%s', async path => {
    const hash = value => createHash('sha256').update(value).digest('hex');
    const absolute = join(workbench, 'upstream', path), original = await readFile(absolute);
    const lf = original.toString('utf8').replace(/\r\n/g, '\n'), crlf = lf.replace(/\n/g, '\r\n');
    const generatedLf = Buffer.from(await transform(absolute, lf)), generatedCrlf = Buffer.from(await transform(absolute, crlf));
    expect(generatedCrlf.equals(generatedLf)).toBe(true);
    expect(generatedLf.equals(Buffer.from(lf))).toBe(false);
    const unchanged = await readFile(absolute);
    expect(unchanged.equals(original)).toBe(true);
    newlineReceipts.push({ path, original: hash(original), after: hash(unchanged), lf: hash(generatedLf), crlf: hash(generatedCrlf) });
  });

  test('固定提交不符时拒绝，特殊转换清单全部属于正式管线', () => {
    expect(() => sourceTransform(workbench, { commit: 'other' }, [])).toThrow('上游版本');
    for (const path of [...generatedModules.keys(), ...structuralModules.keys(), ...boundaryModules.keys(), ...anchorErrors.keys()]) {
      expect(transformedSourcePaths).toContain(path);
    }
  });

  test.each(transformedSourcePaths)('按真实机制核输入合同且原件不变：%s', async path => {
    const absolute = join(workbench, 'upstream', path), original = await readFile(absolute, 'utf8');
    const generated = await transform(absolute, original);
    expect(generated).not.toBe(original);
    if (generatedModules.has(path)) {
      const required = generatedModules.get(path);
      if (required === '') expect(generated).toBe('');
      else expect(generated).toContain(required);
      expect(await transform(absolute, '')).toBe(generated);
      expect(await transform(absolute, original + original)).toBe(generated);
    } else if (boundaryModules.has(path)) {
      const [missingBoundary, retained] = boundaryModules.get(path);
      await expect(transform(absolute, '')).rejects.toThrow(missingBoundary);
      expect(generated).toContain(retained);
      expect(await transform(absolute, original + original)).toBe(generated);
    } else if (structuralModules.has(path)) {
      const [removed, retained, missingDeclaration] = structuralModules.get(path);
      expect(original).toContain(removed);
      expect(generated).not.toContain(removed);
      expect(generated).toContain(retained);
      if (missingDeclaration) await expect(transform(absolute, '')).rejects.toThrow(missingDeclaration);
    } else {
      const [missingAnchor, duplicateAnchor = '恰好一次'] = anchorErrors.get(path) ?? ['恰好一次'];
      await expect(transform(absolute, '')).rejects.toThrow(missingAnchor);
      await expect(transform(absolute, original + original)).rejects.toThrow(duplicateAnchor);
    }
    expect(await readFile(absolute, 'utf8')).toBe(original);
  });

  test('真实 child 构建只使用明确根，在查询前排除跨 Gateway、跨工作区及原生 extras', () => {
    const f = fixture();
    try {
      const a = uri('same.txt'), b = uri('same.txt', { ...owner, gatewayId: 'gateway-b' });
      const c = uri('same.txt', { ...owner, workspaceId: 'workspace-c' });
      const query = f.builder.text({ pattern: 'Needle' }, [uri()], {
        extraFileResources: [a, b, c, m.URI.parse('workbench:/same.txt')], isSmartCase: true,
      });
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([uri().toString()]);
      expect(query.extraFileResources.map(resource => resource.toString())).toEqual([a.toString()]);
      expect(query.contentPattern.isCaseSensitive).toBe(true);
      expect(f.context.getWorkspaceFolder(b)).toBeNull();
      expect(() => f.builder.text({ pattern: 'needle' }, [b])).toThrow('超出');
    } finally { f.instantiation.dispose(); f.context.dispose(); }
  });

  test('仅已打开保留明确根，不进入按 fsPath 枚举全部编辑器的旧分支', () => {
    const f = fixture(undefined, { get groups() { throw new Error('不应读取其它组资源'); } });
    try {
      const query = f.builder.text({ pattern: 'needle' }, [uri()], { onlyOpenEditors: true, includePattern: 'src/*.ts' });
      expect(query.onlyOpenEditors).toBe(true);
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([uri().toString()]);
      expect(query.includePattern).toEqual({ 'src/*.ts': true });
    } finally { f.instantiation.dispose(); f.context.dispose(); }
  });

  test('显式 search path 不得越出所选根，原生 context 的仅已打开分支保持原行为', () => {
    const f = fixture();
    try {
      expect(() => f.builder.text({ pattern: 'needle' }, [uri()], { includePattern: '/elsewhere', expandPatterns: true })).toThrow('超出');
    } finally { f.instantiation.dispose(); f.context.dispose(); }
    const nativeRoot = m.URI.parse('workbench:/Workspace'), nativeFile = m.URI.joinPath(nativeRoot, 'one.txt');
    const native = {
      getWorkspace: () => ({ id: 'native', folders: [] }), getWorkbenchState: () => 1,
      getWorkspaceFolder: () => null, isInsideWorkspace: () => false };
    const n = fixture(native, { groups: [{ editors: [{ resource: nativeFile }] }] });
    try {
      const query = n.builder.file([nativeRoot], { onlyOpenEditors: true });
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([nativeRoot.toString()]);
      expect(query.folderQueries[0].includePattern).toEqual({ 'one.txt': true });
    } finally { n.instantiation.dispose(); }
  });

  test('结果转交后 child 随原结果模型释放，关闭 Picker 不提前销毁', () => {
    const context = new m.SearchWorkspaceContext(scope), resultLifetime = new m.DisposableStore();
    let disposed = 0;
    context.ownChild({ dispose() { disposed++; } });
    context.transferTo({ ownSearchContext(child) { resultLifetime.add(child); } });
    context.dispose();
    expect(disposed).toBe(0);
    resultLifetime.dispose();
    expect(disposed).toBe(1);
    const cancelled = new m.SearchWorkspaceContext(scope);
    cancelled.ownChild({ dispose() { disposed++; } }); cancelled.dispose();
    expect(disposed).toBe(2);
  });

  test('范围变更发出原事件，旧 join 迟到不覆盖新范围；查询可反解原稳定根', async () => {
    const context = new m.SearchWorkspaceContext();
    let release;
    const waiting = new Promise(resolve => { release = resolve; });
    const changing = context.onWillChangeWorkspaceFolders(event => {
      if (event.changes.added[0].name === '旧等待') event.join(waiting);
    });
    const changed = [];
    const subscription = context.onDidChangeWorkspaceFolders(event => changed.push(event));
    try {
      const unbound = fixture(context);
      try { expect(() => unbound.builder.text({ pattern: 'needle' }, [])).toThrow('明确'); }
      finally { unbound.instantiation.dispose(); }
      const old = context.updateScope({ ...scope, workspaces: [{ ...scope.workspaces[0], label: '旧等待' }] });
      await context.updateScope(scope);
      release(); await old;
      expect(context.getWorkspace().folders[0].name).toBe('工作区 A');
      expect(changed).toHaveLength(1);
      expect(m.scopeFromQuery({ folderQueries: [{ folder: uri('nested'), folderName: '工作区 A' }] })).toEqual(scope);
      expect(() => m.scopeFromQuery({ folderQueries: [{ folder: m.URI.parse('workbench:/Workspace') }] })).toThrow('稳定');
    } finally { changing.dispose(); subscription.dispose(); context.dispose(); }
  });
});
