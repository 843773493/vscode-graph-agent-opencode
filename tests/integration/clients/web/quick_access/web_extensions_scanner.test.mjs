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
