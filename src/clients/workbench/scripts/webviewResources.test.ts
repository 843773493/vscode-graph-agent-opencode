import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { CharCode } from '../upstream/src/vs/base/common/charCode.js';
import { Schemas } from '../upstream/src/vs/base/common/network.js';
import { extUri, isEqualOrParent } from '../upstream/src/vs/base/common/resources.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { getMediaMime, Mimes } from '../upstream/src/vs/base/common/mime.js';
import { extname } from '../upstream/src/vs/base/common/path.js';
import { FileOperationError, FileOperationResult, IFileService } from '../upstream/src/vs/platform/files/common/files.js';
import { ILogService } from '../upstream/src/vs/platform/log/common/log.js';
import { IUriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { getWebviewContentMimeType } from '../upstream/src/vs/platform/webview/common/mimeTypes.js';
import { webviewRootResourceAuthority } from '../upstream/src/vs/workbench/contrib/webview/common/webview.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
async function source(path: string) {
  const full = join(root, 'upstream', path);
  return ts.createSourceFile(full, await transform(full, await readFile(full, 'utf8')), ts.ScriptTarget.Latest, true);
}
const common = await source('src/vs/workbench/contrib/webview/common/webview.ts');
const functions = common.statements.filter(node => ts.isFunctionDeclaration(node)
  && ['asWebviewUri', 'encodeAuthority'].includes(node.name?.text ?? ''));
const compiled = ts.transpile(functions.map(node => node.getText().replace(/^export /, '')).join('\n'),
  { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext });
const asWebviewUri = new Function('CharCode', 'Schemas', 'URI', 'isEqualOrParent', 'webviewRootResourceAuthority',
  `${compiled}\nreturn asWebviewUri;`)(CharCode, Schemas, URI, isEqualOrParent, webviewRootResourceAuthority) as
  (resource: URI, remote: { isRemote: boolean; authority?: string } | undefined, roots?: readonly URI[]) => URI;

test.each(['http', 'https'])('允许根中的 %s 扩展文件走原 Worker，外部和相邻目录 URL 保持原样', scheme => {
  const base = URI.parse(`${scheme}://127.0.0.1:8011/extensions/markdown`);
  const resource = URI.joinPath(base, 'media/pre.js').with({ query: 'version=2', fragment: 'entry' });
  const projected = asWebviewUri(resource, undefined, [base]);
  expect(projected.scheme).toBe('https');
  expect(projected.authority).toBe(`${scheme}+127-002e0-002e0-002e1-003a8011.${webviewRootResourceAuthority}`);
  expect(projected.path).toBe(resource.path);
  expect(projected.query).toBe(resource.query);
  expect(projected.fragment).toBe(resource.fragment);
  for (const external of [URI.parse('https://example.com/image.png'),
    URI.parse(`${scheme}://127.0.0.1:8011/extensions/markdown-other/pre.js`),
    URI.parse(`${scheme}://127.0.0.1:8012/extensions/markdown/pre.js`)]) {
    expect(asWebviewUri(external, undefined, [base])).toBe(external);
  }
  expect(asWebviewUri(resource, undefined, [])).toBe(resource);
  expect(asWebviewUri(resource, undefined)).toBe(resource);
});

test('业务文件保留原 Gateway／工作区 URI 投影及远端文件语义', () => {
  const resource = URI.parse('boxteam-file://gateway-a/workspace-a/root/style.css');
  expect(asWebviewUri(resource, undefined).authority).toBe(`boxteam-file+gateway-002da.${webviewRootResourceAuthority}`);
  expect(asWebviewUri(resource, undefined).path).toBe(resource.path);
  const remote = asWebviewUri(URI.file('/workspace/style.css'), { isRemote: true, authority: 'ssh-remote+target' });
  expect(remote.authority).toBe(`vscode-remote+ssh-002dremote-002btarget.${webviewRootResourceAuthority}`);
});

test('原扩展 API 每次读取当前 localResourceRoots，显式空根不会沿默认扩展根加载', async () => {
  const host = await source('src/vs/workbench/api/common/extHostWebview.ts');
  const declaration = host.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtHostWebview');
  if (!declaration || !ts.isClassDeclaration(declaration)) throw new Error('原 ExtHostWebview 缺失');
  const method = declaration.members.find(node => ts.isMethodDeclaration(node) && node.name.getText() === 'asWebviewUri');
  if (!method || !ts.isMethodDeclaration(method) || !method.body) throw new Error('原 asWebviewUri 缺失');
  const create = new Function('asWebviewUri', 'getDefaultLocalResourceRoots', `return class {
    #hasCalledAsWebviewUri = false; #remoteInfo; #extension; #workspace; #options;
    constructor(extension, options) { this.#extension = extension; this.#options = options; }
    setRoots(roots) { this.#options = { localResourceRoots: roots }; }
    asWebviewUri(resource) ${method.body.getText()}
  }`);
  const extension = URI.parse('http://127.0.0.1:8011/extensions/markdown');
  const resource = URI.joinPath(extension, 'media/pre.js');
  const Webview = create(asWebviewUri, () => [extension]);
  const view = new Webview({}, {});
  expect(view.asWebviewUri(resource).authority).toEndWith(webviewRootResourceAuthority);
  view.setRoots([]);
  expect(view.asWebviewUri(resource)).toBe(resource);
  view.setRoots([extension]);
  expect(view.asWebviewUri(resource).authority).toEndWith(webviewRootResourceAuthority);
});

test('HTTP 占位 stat 不作为 Worker validator，业务文件仍沿原条件读取且根拒绝先于读取', async () => {
  const loader = await source('src/vs/workbench/contrib/webview/browser/resourceLoading.ts');
  const executable = ts.transpile(loader.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText().replace(/^export /, '')).join('\n'),
  { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext });
  const load = new Function('IUriIdentityService', 'IFileService', 'ILogService', 'Schemas',
    'FileOperationError', 'FileOperationResult', 'getWebviewContentMimeType', 'isWindows',
    `${executable}\nreturn loadLocalResource;`)(
    IUriIdentityService, IFileService, ILogService, Schemas,
    FileOperationError, FileOperationResult, getWebviewContentMimeType, false) as
    (accessor: { get: (id: unknown) => unknown }, resource: URI,
    options: { ifNoneMatch: string; roots: readonly URI[] }, token: unknown) =>
    Promise<{ etag?: string; mtime?: number; mimeType?: string; type: number }>;
  const reads: { uri: URI; options: { etag?: string } }[] = [];
  const services = new Map<unknown, unknown>([
    [IUriIdentityService, { extUri }], [ILogService, { trace() {}, error() {} }],
    [IFileService, { async readFileStream(uri: URI, options: { etag?: string }) {
      reads.push({ uri, options });
      return { value: undefined, etag: 'observed-version', mtime: 42, size: 10 };
    } }],
  ]);
  for (const scheme of ['http', 'https', 'boxteam-file']) {
    const root = URI.parse(`${scheme}://target/extensions/markdown`);
    const result = await load({ get: id => services.get(id) }, URI.joinPath(root, 'media/pre.js'),
      { ifNoneMatch: 'old-version', roots: [root] }, {});
    const isHttp = scheme !== 'boxteam-file';
    expect(reads.at(-1)?.options.etag).toBe(isHttp ? undefined : 'old-version');
    expect(result.etag).toBe(isHttp ? undefined : 'observed-version');
    expect(result.mtime).toBe(isHttp ? undefined : 42);
    expect(result.mimeType).toBe('application/javascript');
  }
  const count = reads.length;
  const denied = await load({ get: id => services.get(id) }, URI.parse('http://target/secret.js'),
    { ifNoneMatch: 'old-version', roots: [URI.parse('http://target/extensions/markdown')] }, {});
  expect(denied.type).toBe(2);
  expect(reads).toHaveLength(count);
});

test('原 Worker 响应保留真实字体 MIME，不影响脚本与 CSS', async () => {
  const mimeSource = await source('src/vs/platform/webview/common/mimeTypes.ts');
  const executable = ts.transpile(mimeSource.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText().replace(/^export /, '')).join('\n'),
  { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext });
  const mime = new Function('getMediaMime', 'Mimes', 'extname', `${executable}\nreturn getWebviewContentMimeType;`)(
    getMediaMime, Mimes, extname) as (resource: URI) => string;
  for (const [extension, expected] of [['ttf', 'font/ttf'], ['otf', 'font/otf'],
    ['woff', 'font/woff'], ['woff2', 'font/woff2'], ['css', 'text/css'], ['js', 'application/javascript']]) {
    expect(mime(URI.parse(`boxteam-file://gateway/workspace/file.${extension}`))).toBe(expected!);
  }
});
