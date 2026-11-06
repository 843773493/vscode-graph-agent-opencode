import { afterAll, expect, spyOn, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/web_extensions_scanner.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'web-extensions-scanner.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { WebExtensionsScannerService } from '${upstream}workbench/services/extensionManagement/browser/webExtensionsScannerService.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { FileOperationError, FileOperationResult } from '${upstream}platform/files/common/files.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
export { URI } from '${upstream}base/common/uri.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const receipt = { boundary: '真实WebExtensionsScannerService、FileService、URI identity及内存文件Provider；仅扩展资源读取／内建目录及兼容能力为受控依赖，不代替真实安装与扩展宿主', checks: [] };

async function fixture(callback) {
  const log = new m.NullLogService(), files = new m.FileService(log), provider = new m.InMemoryFileSystemProvider();
  files.registerProvider('vscode-userdata', provider);
  const identity = new m.UriIdentityService(files), scanners = [], reads = [];
  const profile = m.URI.parse('vscode-userdata:/profiles/default/extensions.json');
  const other = m.URI.parse('vscode-userdata:/profiles/other/extensions.json');
  await Promise.all([files.createFolder(m.URI.joinPath(profile, '..')), files.createFolder(m.URI.joinPath(other, '..'))]);
  const resources = new Map();
  const extension = (name, version) => {
    const location = m.URI.parse(`https://extensions.invalid/${name}/${version}`);
    const manifest = { publisher: 'cache-probe', name, version, engines: { vscode: '^1.145.0' }, browser: './extension.js',
      contributes: { configuration: { properties: { [`${name}.version`]: { type: 'string', default: version } } } } };
    resources.set(m.URI.joinPath(location, 'package.json').toString(), JSON.stringify(manifest));
    resources.set(m.URI.joinPath(location, 'package.nls.json').toString(), '{}');
    resources.set(location.toString(), JSON.stringify([m.URI.joinPath(location, 'package.nls.json').toString()]));
    return location;
  };
  const scanner = () => {
    const service = new m.WebExtensionsScannerService({ userRoamingDataHome: m.URI.parse('vscode-userdata:/User'), options: {} },
      { scanBuiltinExtensions: async () => [] }, files, log, { isEnabled: () => false },
      { canExecuteOnWeb: manifest => Boolean(manifest.browser) }, {
        async readExtensionResource(uri) {
          reads.push(uri.toString());
          if (!resources.has(uri.toString())) throw new Error(`受控扩展资源不存在：${uri}`);
          return resources.get(uri.toString());
        }, isExtensionGalleryResource: async () => false,
        getExtensionGalleryResourceURL: async ({ name, version }) => m.URI.parse(`https://extensions.invalid/${name}/${version}`),
      }, {}, { get: (_key, _scope, fallback) => fallback }, { version: '1.145.0' },
      { defaultProfile: { extensionsResource: profile } }, identity, { when: () => new Promise(() => {}) });
    scanners.push(service); return service;
  };
  try { await callback({ files, profile, other, scanner, extension, resources, reads }); }
  finally { for (const scanner of scanners) scanner.dispose(); identity.dispose(); files.dispose(); provider.dispose(); }
}

test('原profile记录跨scanner复用manifest，显式升级与卸载不改变另一profile', async () => {
  await fixture(async f => {
    const service = f.scanner(), first = f.extension('probe', '1.0.0'), next = f.extension('probe', '2.0.0');
    await service.addExtension(first, {}, f.profile);
    await service.addExtension(first, {}, f.other);
    const reads = f.reads.length;
    const reopened = f.scanner();
    expect((await reopened.scanUserExtensions(f.profile)).map(e => e.manifest.version)).toEqual(['1.0.0']);
    expect(f.reads).toHaveLength(reads);
    await reopened.addExtension(next, {}, f.profile);
    const upgraded = await reopened.scanUserExtensions(f.profile);
    expect(upgraded).toHaveLength(1);
    expect(upgraded[0].isValid).toBe(true);
    expect(upgraded[0].manifest.contributes.configuration.properties['probe.version'].default).toBe('2.0.0');
    expect((await reopened.scanUserExtensions(f.other)).map(e => e.manifest.version)).toEqual(['1.0.0']);
    await reopened.removeExtension(upgraded[0], f.profile);
    expect(await f.scanner().scanUserExtensions(f.profile)).toEqual([]);
    expect((await f.scanner().scanUserExtensions(f.other)).map(e => e.manifest.version)).toEqual(['1.0.0']);
    receipt.checks.push('原安装记录带manifest跨scanner复用；显式升级／卸载保持profile隔离，不读取旧版本manifest');
  });
});

for (const method of ['location', 'gallery']) {
  for (const [label, patch, message] of [
    ['engine', { engines: { vscode: '^999.0.0' } }, /not compatible.*requires/i],
    ['version', { version: 'invalid-version' }, /not semver/i],
    ['browser', { browser: 42 }, /browser.*string/i],
  ]) test(`${method}安装在写入前拒绝不兼容${label}，保留现有版本和其它profile`, async () => {
    await fixture(async f => {
      const service = f.scanner(), current = f.extension('compatible', '1.0.0'), candidate = f.extension('compatible', '2.0.0');
      await service.addExtension(current, {}, f.profile);
      await service.addExtension(current, {}, f.other);
      const originals = await Promise.all([f.profile, f.other].map(async uri => (await f.files.readFile(uri)).value.toString()));
      const manifestUri = m.URI.joinPath(candidate, 'package.json').toString();
      f.resources.set(manifestUri, JSON.stringify({ ...JSON.parse(f.resources.get(manifestUri)), ...patch }));
      const attempt = method === 'location' ? service.addExtension(candidate, {}, f.profile)
        : service.addExtensionFromGallery({ identifier: { id: 'cache-probe.compatible' }, publisher: 'cache-probe',
          name: 'compatible', version: '2.0.0', properties: { targetPlatform: 'web' }, assets: {} }, {}, f.profile);
      await expect(attempt).rejects.toThrow(message);
      expect(await Promise.all([f.profile, f.other].map(async uri => (await f.files.readFile(uri)).value.toString()))).toEqual(originals);
      expect((await service.scanUserExtensions(f.profile)).map(extension => extension.manifest.version)).toEqual(['1.0.0']);
      receipt.checks.push(`${method}的${label}校验在发布前拒绝，旧版本及另一profile原件保留`);
    });
  });
}

test('原gallery仍可发布有效版本，manifest警告保持可安装', async () => {
  await fixture(async f => {
    const service = f.scanner(), location = f.extension('gallery-valid', '1.0.0');
    const manifestUri = m.URI.joinPath(location, 'package.json').toString();
    // 上游仅把非便携入口记作警告；安装拒绝只消费原isValid，不另定更严格规则。
    f.resources.set(manifestUri, JSON.stringify({ ...JSON.parse(f.resources.get(manifestUri)), browser: '../extension.js' }));
    const installed = await service.addExtensionFromGallery({ identifier: { id: 'cache-probe.gallery-valid' },
      publisher: 'cache-probe', name: 'gallery-valid', version: '1.0.0', properties: { targetPlatform: 'web' }, assets: {} }, {}, f.profile);
    expect(installed.isValid).toBe(true);
    expect(installed.validations).not.toBeEmpty();
    expect((await service.scanUserExtensions(f.profile)).map(extension => extension.identifier.id)).toEqual(['cache-probe.gallery-valid']);
    receipt.checks.push('原gallery有效版本可安装；仅有manifest警告不被扩为拒绝');
  });
});

for (const method of ['location', 'gallery']) test(`${method}首次安装不兼容版本不创建安装记录`, async () => {
  await fixture(async f => {
    const service = f.scanner(), location = f.extension('fresh-invalid', '1.0.0');
    const manifestUri = m.URI.joinPath(location, 'package.json').toString();
    f.resources.set(manifestUri, JSON.stringify({ ...JSON.parse(f.resources.get(manifestUri)), engines: { vscode: '^999.0.0' } }));
    const attempt = method === 'location' ? service.addExtension(location, {}, f.profile)
      : service.addExtensionFromGallery({ identifier: { id: 'cache-probe.fresh-invalid' }, publisher: 'cache-probe',
        name: 'fresh-invalid', version: '1.0.0', properties: { targetPlatform: 'web' }, assets: {} }, {}, f.profile);
    await expect(attempt).rejects.toThrow(/not compatible.*requires/i);
    expect(await f.files.exists(f.profile)).toBe(false);
    receipt.checks.push(`${method}首次校验拒绝不创建安装记录`);
  });
});

for (const original of ['{"broken":', '{"unknownFormat":1}', JSON.stringify([{ identifier: { id: 'cache-probe.lost' }, version: '1.0.0' }])]) {
  test(`损坏安装记录不得伪报空列表或被新安装覆盖：${original}`, async () => {
    await fixture(async f => {
      await f.files.writeFile(f.profile, m.VSBuffer.fromString(original));
      const service = f.scanner(), location = f.extension('replacement', '1.0.0');
      await expect(service.scanUserExtensions(f.profile)).rejects.toThrow();
      await expect(service.addExtension(location, {}, f.profile)).rejects.toThrow();
      expect((await f.files.readFile(f.profile)).value.toString()).toBe(original);
      receipt.checks.push('损坏／未知结构的原安装记录读取与新增均失败，原字节保留');
    });
  });
}

test('原安装记录读取权限失败向上抛出原错误，不写成新安装列表', async () => {
  await fixture(async f => {
    const service = f.scanner(), location = f.extension('replacement', '1.0.0');
    await service.addExtension(location, {}, f.profile);
    const original = (await f.files.readFile(f.profile)).value.toString();
    const read = f.files.readFile.bind(f.files), denied = new m.FileOperationError('受控profile拒绝访问', m.FileOperationResult.FILE_PERMISSION_DENIED);
    const interception = spyOn(f.files, 'readFile').mockImplementation((resource, ...args) => {
      if (resource.toString() === f.profile.toString()) return Promise.reject(denied);
      return read(resource, ...args);
    });
    try {
      await expect(service.scanUserExtensions(f.profile)).rejects.toBe(denied);
      await expect(service.addExtension(location, {}, f.profile)).rejects.toBe(denied);
    } finally { interception.mockRestore(); }
    expect((await f.files.readFile(f.profile)).value.toString()).toBe(original);
    receipt.checks.push('profile权限错误保持原归属并阻止覆盖；初始不存在仍允许首次安装');
  });
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'web-extensions-scanner-checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
