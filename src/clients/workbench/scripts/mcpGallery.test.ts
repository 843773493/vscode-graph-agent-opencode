import { afterEach, expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { CancellationError } from '../upstream/src/vs/base/common/errors.js';
import { VSBuffer, bufferToStream } from '../upstream/src/vs/base/common/buffer.js';
import { DisposableStore, type IDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { NullLogService } from '../upstream/src/vs/platform/log/common/log.js';
import type { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import type { IFileService } from '../upstream/src/vs/platform/files/common/files.js';
import type { IProductService } from '../upstream/src/vs/platform/product/common/productService.js';
import type { ILogService } from '../upstream/src/vs/platform/log/common/log.js';
import type { IRequestService, IRequestContext } from '../upstream/src/vs/platform/request/common/request.js';
import type { IRequestOptions } from '../upstream/src/vs/base/parts/request/common/request.js';
import type { IMcpGalleryManifest, IMcpGalleryManifestService } from '../upstream/src/vs/platform/mcp/common/mcpGalleryManifest.js';
import type { IMcpGalleryService } from '../upstream/src/vs/platform/mcp/common/mcpManagement.js';
import { CustomizationMarketplaceService, type ICustomizationMarketplaceProvider } from '../upstream/src/vs/platform/customizationMarketplace/common/customizationMarketplaceService.js';
import { sourceTransform, pinnedUpstreamCommit, transformedSourcePaths } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const modules = new Map<string, Promise<Record<string, unknown>>>();
const transformed = new Set(transformedSourcePaths.map(path => join(root, 'upstream', path)));

// 加载真实转换结果和原依赖；只移除模块语法，不重写被测方法或协议解析。
async function load<T>(path: string): Promise<T> {
  const full = resolve(root, path);
  if (!modules.has(full)) modules.set(full, (async () => {
    const original = await readFile(full, 'utf8');
    const source = transformed.has(full) ? await transform(full, original) : original;
    const javascript = ts.transpileModule(source, { compilerOptions: {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext,
      experimentalDecorators: true, useDefineForClassFields: false,
    } }).outputText;
    const ast = ts.createSourceFile(full, javascript, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    const bindings: Record<string, unknown> = {};
    const exports: string[] = [];
    let body = '';
    for (const statement of ast.statements) {
      if (ts.isExportDeclaration(statement)) {
        if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
          for (const entry of statement.exportClause.elements) exports.push(`${entry.name.text}: ${entry.propertyName?.text ?? entry.name.text}`);
        }
        continue;
      }
      if (ts.isImportDeclaration(statement)) {
        const specifier = (statement.moduleSpecifier as ts.StringLiteral).text;
        const dependency = resolve(dirname(full), specifier.replace(/\.js$/, '.ts'));
        const imported = transformed.has(dependency) ? await load<Record<string, unknown>>(dependency)
          : await import(pathToFileURL(dependency).href);
        const clause = statement.importClause;
        if (clause?.name) bindings[clause.name.text] = imported.default;
        if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
          for (const entry of clause.namedBindings.elements) bindings[entry.name.text] = imported[entry.propertyName?.text ?? entry.name.text];
        }
        continue;
      }
      if (ts.canHaveModifiers(statement) && ts.getModifiers(statement)?.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
        if (ts.isClassDeclaration(statement) || ts.isFunctionDeclaration(statement)) exports.push(statement.name!.text);
        else if (ts.isVariableStatement(statement)) for (const declaration of statement.declarationList.declarations) exports.push(declaration.name.getText(ast));
      }
      body += statement.getText(ast).replace(/^export /, '') + '\n';
    }
    return new Function(...Object.keys(bindings), body + `\nreturn { ${exports.join(', ')} };`)(...Object.values(bindings)) as Record<string, unknown>;
  })());
  return await modules.get(full)! as T;
}

type ManifestConstructor = new (product: IProductService, remote: { getConnection(): null }, log: ILogService, configuration: IConfigurationService) => IMcpGalleryManifestService & IDisposable;
type GalleryConstructor = new (requests: IRequestService, files: IFileService, log: ILogService, manifests: IMcpGalleryManifestService) => IMcpGalleryService & IDisposable;
type ProviderConstructor = new (gallery: IMcpGalleryService, manifests: IMcpGalleryManifestService) => ICustomizationMarketplaceProvider & IDisposable;
const { WorkbenchMcpGalleryManifestService } = await load<{ WorkbenchMcpGalleryManifestService: ManifestConstructor }>('upstream/src/vs/workbench/services/mcp/browser/mcpGalleryManifestService.ts');
const { McpGalleryService } = await load<{ McpGalleryService: GalleryConstructor }>('upstream/src/vs/platform/mcp/common/mcpGalleryService.ts');
const { McpGalleryMarketplaceProvider, createMcpGalleryMarketplaceProviders } = await load<{
  McpGalleryMarketplaceProvider: ProviderConstructor;
  createMcpGalleryMarketplaceProviders(instantiation: { createInstance(ctor: ProviderConstructor): ICustomizationMarketplaceProvider }): readonly ICustomizationMarketplaceProvider[];
}>('upstream/src/vs/platform/customizationMarketplace/common/mcpGalleryMarketplaceProvider.ts');
const disposables = new DisposableStore();
afterEach(() => disposables.clear());

function configuration(url = '', version?: string) {
  let value = { serviceUrl: url, version };
  const changes = disposables.add(new Emitter<{ affectsConfiguration(key: string): boolean }>());
  return {
    service: { getValue: () => value, onDidChangeConfiguration: changes.event } as unknown as IConfigurationService,
    change(serviceUrl: string, nextVersion?: string) {
      value = { serviceUrl, version: nextVersion };
      changes.fire({ affectsConfiguration: key => key.startsWith('chat.mcp.gallery.') });
    },
  };
}
function manifests(config: ReturnType<typeof configuration>) {
  return disposables.add(new WorkbenchMcpGalleryManifestService(
    { mcpGallery: { serviceUrl: 'https://implicit-default.invalid' } } as IProductService,
    { getConnection: () => null }, new NullLogService(), config.service,
  ));
}
function document(name: string) {
  return { server: { name, description: `description ${name}`, version: '1.0.0',
    packages: [{ registryType: 'npm', identifier: name, transport: { type: 'stdio' } }] }, _meta: {} };
}
function context(data: unknown, status = 200): IRequestContext {
  return { res: { statusCode: status, headers: {} }, stream: bufferToStream(VSBuffer.fromString(typeof data === 'string' ? data : JSON.stringify(data))) };
}
function gallery(manifest: IMcpGalleryManifestService, handler: (options: IRequestOptions, token: CancellationToken) => Promise<IRequestContext> | IRequestContext) {
  return disposables.add(new McpGalleryService({ request: handler } as IRequestService, {} as IFileService, new NullLogService(), manifest));
}

test('纯配置 manifest 默认 v0.1、显式 v0；空配置不使用产品默认源，连续换源没有异步探测', async () => {
  const config = configuration();
  const service = manifests(config);
  expect(await service.getMcpGalleryManifest()).toBeNull();
  config.change('https://one.test///');
  expect(await service.getMcpGalleryManifest()).toMatchObject({ url: 'https://one.test', version: 'v0.1' });
  config.change('https://two.test', 'v0');
  config.change('https://three.test');
  expect(await service.getMcpGalleryManifest()).toMatchObject({ url: 'https://three.test', version: 'v0.1' });
});

test('原 query 实际委托严格 queryPage，原 parser 消费 v0.1/v0，cursor 编码且续页不重发搜索', async () => {
  const requests: string[] = [];
  const config = configuration('https://selected.test');
  const service = gallery(manifests(config), options => {
    requests.push(options.url!);
    if (options.url!.includes('/v0/')) return context({ servers: [{ name: 'selected/legacy', description: 'legacy package', version: '1.0.0',
      packages: [{ registry_type: 'npm', identifier: 'selected-legacy', transport: { type: 'stdio' } }],
      _meta: { 'io.modelcontextprotocol.registry/official': { id: 'legacy-id', is_latest: true } } }], metadata: { count: 1 } });
    const next = options.url!.includes('cursor=');
    return context({ servers: [document(next ? 'selected/second' : 'selected/first')], metadata: { count: 1, nextCursor: next ? undefined : 'opaque+/=' } });
  });
  const pager = await service.query({ text: ' alpha beta ' });
  expect(pager.firstPage.items[0].name).toBe('selected/first');
  expect((await pager.getNextPage(CancellationToken.None)).items[0].name).toBe('selected/second');
  expect(requests).toEqual([
    'https://selected.test/v0.1/servers?limit=50&version=latest&search=alpha%20beta',
    'https://selected.test/v0.1/servers?limit=50&version=latest&cursor=opaque%2B%2F%3D',
  ]);
  config.change('https://selected.test', 'v0');
  const page = await service.queryPage({ pageSize: 100 }, CancellationToken.None);
  expect(page.total).toBeUndefined();
  expect(page.items[0]).toMatchObject({ name: 'selected/legacy', configuration: { packages: [{ registryType: 'npm', identifier: 'selected-legacy' }] } });
  expect(requests.at(-1)).toContain('/v0/servers?limit=100');
});

test('未配置、HTTP失败、协议不支持与无匹配可区分，legacy query 不把失败变空页', async () => {
  const config = configuration();
  let status = 503;
  const service = gallery(manifests(config), () => context({ servers: [], metadata: { count: 0 } }, status));
  await expect(service.query()).rejects.toThrow('not configured');
  config.change('https://selected.test');
  await expect(service.query()).rejects.toThrow('HTTP 503');
  config.change('https://selected.test', 'unknown');
  await expect(service.query()).rejects.toThrow('Unsupported MCP registry protocol version');
  config.change('https://selected.test');
  status = 200;
  expect((await service.query()).firstPage).toEqual({ items: [], hasMore: false });
});

test('真实 request token 在取消及换源时触发，旧 pager 不可继续请求旧来源', async () => {
  const config = configuration('https://old.test');
  const serviceManifest = manifests(config);
  const pending: Array<{ token: CancellationToken; complete(context: IRequestContext): void }> = [];
  let holdRequests = true;
  const service = gallery(serviceManifest, (_options, token) => holdRequests
    ? new Promise(resolve => pending.push({ token, complete: resolve }))
    : context({ servers: [document('old/server')], metadata: { count: 1, nextCursor: 'old-cursor' } }));
  const cancellation = disposables.add(new CancellationTokenSource());
  const initial = service.query(undefined, cancellation.token);
  const initialResult = initial.then(() => undefined, error => error);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(pending).toHaveLength(1);
  cancellation.cancel();
  expect(await initialResult).toBeInstanceOf(CancellationError);
  expect(pending[0].token.isCancellationRequested).toBe(true);
  pending[0].complete(context({ servers: [], metadata: { count: 0 } }));

  const changing = service.query();
  const changedResult = changing.then(() => undefined, error => error);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(pending).toHaveLength(2);
  config.change('https://new.test');
  expect(await changedResult).toBeInstanceOf(CancellationError);
  expect(pending[1].token.isCancellationRequested).toBe(true);
  pending[1].complete(context({ servers: [], metadata: { count: 0 } }));
  holdRequests = false;
  const pager = await service.query();
  config.change('https://third.test');
  await expect(pager.getNextPage(CancellationToken.None)).rejects.toBeInstanceOf(CancellationError);
  expect(pending).toHaveLength(2);
});

test('唯一 marketplace provider 复用 queryPage；URL或协议换源使原聚合缓存和 continuation 失效', async () => {
  const config = configuration('https://only.test');
  const manifest = manifests(config);
  let requests = 0;
  const service = gallery(manifest, () => {
    ++requests;
    return context({ servers: Array.from({ length: 100 }, (_, i) => document(`only/server-${i}`)), metadata: { count: 100 } });
  });
  const provider = disposables.add(new McpGalleryMarketplaceProvider(service, manifest));
  const providers = createMcpGalleryMarketplaceProviders({ createInstance: () => provider });
  expect(providers.map(item => item.id)).toEqual(['mcpGallery.custom']);
  const combined = new CustomizationMarketplaceService(providers);
  const first = await combined.query({ sourceIds: ['mcpGallery'], pageSize: 1 }, CancellationToken.None);
  expect(first.items[0].installation).toMatchObject({ kind: 'mcpGallery', registry: 'custom', registryUrl: 'https://only.test' });
  expect(first.total).toBeUndefined();
  expect(requests).toBe(1);
  config.change('https://only.test', 'v0');
  await expect(combined.query({ sourceIds: ['mcpGallery'], pageSize: 1, cursor: first.nextCursor }, CancellationToken.None)).rejects.toThrow('page is invalid');
  const fresh = await combined.query({ sourceIds: ['mcpGallery'], pageSize: 1 }, CancellationToken.None);
  expect(requests).toBe(2);
  config.change('https://second.test');
  await expect(combined.query({ sourceIds: ['mcpGallery'], pageSize: 1, cursor: fresh.nextCursor }, CancellationToken.None)).rejects.toThrow('page is invalid');
});

test.each(['成功', '失败'])('原列表 show 将 token 交给真实查询，旧查询%s晚到不覆盖新来源', async outcome => {
  const path = join(root, 'upstream/src/vs/workbench/contrib/mcp/browser/mcpServersView.ts');
  const source = ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
  const viewClass = source.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'McpServersListView') as ts.ClassDeclaration;
  const method = viewClass.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(source) === 'show') as ts.MethodDeclaration;
  const javascript = ts.transpileModule(`async function show(query: string) ${method.body!.getText(source)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const show = new Function('CancellationTokenSource', 'CancellationError', 'isCancellationError', 'getErrorMessage', 'Severity', javascript + '\nreturn show;')(
    CancellationTokenSource, CancellationError, (error: unknown) => error instanceof CancellationError, String, { Error: 3 },
  ) as (this: object, query: string) => Promise<unknown>;
  const pending: Array<{ token: CancellationToken; complete(input: object): void; fail(error: Error): void }> = [];
  const view = { currentQuery: '', workspaceMcpQueryFailed: false, input: undefined as unknown, queryCancellation: undefined as CancellationTokenSource | undefined,
    renderInput() {}, query: (_query: string, token: CancellationToken) => new Promise((resolve, reject) => pending.push({ token, complete: resolve, fail: reject })) };
  const old = show.call(view, '@mcp old');
  const oldResult = old.then(() => undefined, error => error);
  const newer = show.call(view, '@mcp new');
  expect(pending[0].token.isCancellationRequested).toBe(true);
  const current = { model: { name: 'new' }, disposables: new DisposableStore() };
  pending[1].complete(current);
  await newer;
  let disposedOld = false;
  if (outcome === '成功') pending[0].complete({ model: { name: 'old' }, disposables: { dispose() { disposedOld = true; } } });
  else pending[0].fail(new Error('旧来源迟到 HTTP 503'));
  expect(await oldResult).toBeInstanceOf(CancellationError);
  expect(view.input).toBe(current);
  expect(disposedOld).toBe(outcome === '成功');
  expect(view.workspaceMcpQueryFailed).toBe(false);
  view.queryCancellation?.dispose(true);
  current.disposables.dispose();
});
