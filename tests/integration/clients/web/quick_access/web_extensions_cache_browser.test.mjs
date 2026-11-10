import { afterAll, beforeAll, expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/web_extensions_cache_browser.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'web-extensions-cache-browser.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { WebExtensionsScannerService } from '${upstream}workbench/services/extensionManagement/browser/webExtensionsScannerService.js';
export { BuiltinExtensionsScannerService } from '${upstream}workbench/services/extensionManagement/browser/builtinExtensionsScannerService.js';
export { ExtensionManifestPropertiesService } from '${upstream}workbench/services/extensions/common/extensionManifestPropertiesService.js';
export { WorkspaceTrustEnablementService } from '${upstream}workbench/services/workspaces/common/workspaceTrust.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { createFileSystemProviderError, FileSystemProviderErrorCode } from '${upstream}platform/files/common/files.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { ConfigurationService } from '${upstream}platform/configuration/common/configurationService.js';
export { NullPolicyService } from '${upstream}platform/policy/common/policy.js';
export { InMemoryStorageService, StorageScope, StorageTarget } from '${upstream}platform/storage/common/storage.js';
export { ExtensionStorageService } from '${upstream}platform/extensionManagement/common/extensionStorage.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
export { URI } from '${upstream}base/common/uri.js';
export { isWeb } from '${upstream}base/common/platform.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const moduleUrl = `data:text/javascript;base64,${(await readFile(bundle)).toString('base64')}`;
const receipt = {
  boundary: '真实Chromium中运行 sourceTransform 后的原 system／custom builtin scanner、manifest兼容性、FileService、URI identity及内存配置／扩展存储服务；仅资源读取、gallery和lifecycle为受控依赖。缓存项只作为坏项／坏正文和Provider读取错误的故障预备，不证明安装、Gateway或扩展宿主。',
  checks: [],
};
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'web-extensions-cache-browser-checks.json'), JSON.stringify(receipt, null, 2) + '\n'); }
});
async function exerciseInBrowser({ moduleUrl, kind }) {
  globalThis._VSCODE_FILE_ROOT = 'https://extensions.invalid/out/';
  const m = await import(moduleUrl);
  if (!m.isWeb) throw new Error('缓存边界测试必须在真实Web运行时执行');
  const manifest = version => ({ publisher: 'cache-probe', name: 'browser-probe', version,
    engines: { vscode: '^1.145.0' }, browser: './extension.js' });
  // 原内建扫描器读取正常DOM配置；v1与缓存v2区分是否仍读到了坏项之后的合法记录。
  const bundled = document.createElement('div');
  bundled.id = 'vscode-workbench-builtin-extensions';
  bundled.setAttribute('data-settings', JSON.stringify([{ extensionPath: 'browser-probe', packageJSON: manifest('1.0.0') }]));
  document.body.append(bundled);
  class ReadFaultProvider extends m.InMemoryFileSystemProvider {
    denied;
    permissionError = m.createFileSystemProviderError('缓存测试具名读取权限失败', m.FileSystemProviderErrorCode.NoPermissions);
    reads = [];
    async readFile(resource) {
      this.reads.push(resource.toString());
      if (resource.toString() === this.denied) {
        throw this.permissionError;
      }
      return super.readFile(resource);
    }
  }
  const log = new m.NullLogService(), provider = new ReadFaultProvider(), files = new m.FileService(log);
  files.registerProvider('vscode-userdata', provider);
  const identity = new m.UriIdentityService(files);
  const environment = { userRoamingDataHome: m.URI.parse('vscode-userdata:/User'), cacheHome: m.URI.parse('vscode-userdata:/Cache'),
    options: {}, isBuilt: false, disableWorkspaceTrust: true };
  const product = { version: '1.145.0' }, settings = m.URI.parse('vscode-userdata:/User/settings.json');
  await files.createFolder(environment.userRoamingDataHome);
  await files.writeFile(settings, m.VSBuffer.fromString('{}'));
  const configuration = new m.ConfigurationService(settings, files, new m.NullPolicyService(), log);
  await configuration.initialize();
  const trust = new m.WorkspaceTrustEnablementService(configuration, environment);
  const properties = new m.ExtensionManifestPropertiesService(product, configuration, trust, log);
  const storage = new m.InMemoryStorageService();
  storage.store('additionalBuiltinExtensions', JSON.stringify({ extensions: [], extensionGalleryResources: [] }), m.StorageScope.APPLICATION, m.StorageTarget.MACHINE);
  const extensionStorage = new m.ExtensionStorageService(storage, product, log);
  const location = m.URI.parse('https://extensions.invalid/browser-probe/2.0.0'), resources = new Map([
    [m.URI.joinPath(location, 'package.json').toString(), JSON.stringify(manifest('2.0.0'))],
    [m.URI.joinPath(location, 'package.nls.json').toString(), '{}'],
  ]);
  const resourceReads = [], galleryCalls = [];
  const loader = {
    async readExtensionResource(uri) {
      resourceReads.push(uri.toString());
      if (!resources.has(uri.toString())) throw new Error(`受控扩展资源不存在：${uri}`);
      return resources.get(uri.toString());
    },
    isExtensionGalleryResource: async () => false,
  };
  const gallery = { isEnabled: () => true, async getExtensions() { galleryCalls.push('getExtensions'); throw new Error('本测试不允许市场网络读取'); } };
  const builtin = new m.BuiltinExtensionsScannerService(environment, identity, loader, product, log);
  const service = new m.WebExtensionsScannerService(environment, builtin, files, log, gallery, properties, loader,
    extensionStorage, storage, product, { when: () => new Promise(() => {}) });
  const attempt = async operation => {
    try {
      const value = await operation();
      return { ok: true, extensions: (Array.isArray(value) ? value : [value]).map(extension => ({
        id: extension.identifier.id, version: extension.manifest.version, isValid: extension.isValid,
      })) };
    } catch (error) {
      return { ok: false, name: error.name, message: error.message, originalPermissionError: error === provider.permissionError };
    }
  };
  try {
    const cacheEntry = { identifier: { id: 'cache-probe.browser-probe' }, version: '2.0.0', location: location.toJSON(),
      manifest: manifest('2.0.0'), defaultManifestTranslations: null };
    const validCache = JSON.stringify([cacheEntry]);
    const target = m.URI.joinPath(environment.userRoamingDataHome,
      kind === 'system' ? 'systemExtensionsCache.json' : 'customBuiltinExtensionsCache.json');
    const sources = [
      { name: 'invalid-entry-before-valid', text: JSON.stringify([{ identifier: { id: 'cache-probe.broken' }, version: '1.0.0' }, cacheEntry]) },
      { name: 'invalid-json', text: '{"broken":' },
      { name: 'unknown-structure', text: '{"unknownFormat":1}' },
      { name: 'read-permission-denied', text: validCache, denied: true },
    ];
    const results = [], readsBefore = resourceReads.length;
    for (const source of sources) {
      await files.writeFile(target, m.VSBuffer.fromString(source.text));
      const firstRead = provider.reads.length;
      provider.denied = source.denied ? target.toString() : undefined;
      let scan;
      try {
        scan = await attempt(() => kind === 'system' ? service.scanSystemExtensions() : service.scanCustomBuiltinExtensions());
      } finally { provider.denied = undefined; }
      const readPaths = provider.reads.slice(firstRead);
      results.push({ name: source.name, scan, readPaths,
        sourcePreserved: (await files.readFile(target)).value.toString() === source.text });
    }
    return { isWeb: m.isWeb, nodeProcess: typeof globalThis.process, kind, target: target.toString(), results, galleryCalls,
      extraResourceReads: resourceReads.slice(readsBefore) };
  } finally {
    service.dispose(); extensionStorage.dispose(); storage.dispose(); properties.dispose(); trust.dispose();
    configuration.dispose(); identity.dispose(); files.dispose(); provider.dispose(); log.dispose();
  }
}

async function exercise(kind) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), pageErrors = [];
    page.on('pageerror', error => pageErrors.push(String(error)));
    await page.setContent('<!doctype html><title>Web扩展缓存边界</title>');
    const result = await page.evaluate(exerciseInBrowser, { moduleUrl, kind });
    receipt.checks.push({ ...result, pageErrors });
    expect(pageErrors).toEqual([]);
    expect(result.isWeb).toBe(true);
    expect(result.nodeProcess).toBe('undefined');
    expect(result.galleryCalls).toEqual([]);
    return result;
  } finally { await context.close(); }
}

for (const kind of ['system', 'custom']) test(`${kind}派生缓存跳过前置坏项，坏正文与读取失败仍沿原扫描入口回退`, async () => {
  const result = await exercise(kind);
  expect(result.extraResourceReads).toEqual([]);
  for (const observed of result.results) {
    expect(observed.readPaths).toContain(result.target);
    expect(observed.sourcePreserved).toBe(true);
    expect(observed.scan.ok).toBe(true);
    const versions = observed.name === 'invalid-entry-before-valid' ? ['2.0.0'] : kind === 'system' ? ['1.0.0'] : [];
    expect(observed.scan.extensions).toEqual(versions.map(version => ({ id: 'cache-probe.browser-probe', version, isValid: true })));
  }
});
