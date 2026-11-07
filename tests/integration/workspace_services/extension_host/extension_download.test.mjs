import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { basename, join, resolve } from 'node:path';
import { after, afterEach, beforeEach, test } from 'node:test';
import { pathToFileURL } from 'node:url';
import { gzipSync } from 'node:zlib';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

if (process.versions.bun) throw new Error('下载传输测试必须使用固定上游的专用 Node 执行');
if (!process.env.BOXTEAM_TEST_NODE_HOST_MODULES) throw new Error('下载传输测试必须显式指定冻结 Node 依赖');
const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_download.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'extension-download.mjs');
const sourcePaths = ['src/clients/workbench/node/extensionDownload.ts',
  'src/clients/workbench/node/extensionManagement.ts', 'src/clients/workbench/node/extensionManagementProcess.ts',
  'src/clients/workbench/scripts/nativeManagementTransform.mjs', 'src/clients/workbench/scripts/sourceTransform.mjs',
  'tests/integration/workspace_services/extension_host/extension_download.test.mjs'];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path =>
  [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const sourceBefore = await fingerprint();
await symlink(resolve(process.env.BOXTEAM_TEST_NODE_HOST_MODULES), join(run.artifactsDir, 'node_modules'), 'junction');
await build({ stdin: { contents: `
export * from './src/clients/workbench/node/extensionDownload.js';
export { RequestService } from '${upstream}platform/request/node/requestService.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { asText } from '${upstream}platform/request/common/request.js';
export { DownloadService } from '${upstream}platform/download/common/downloadService.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { DiskFileSystemProvider } from '${upstream}platform/files/node/diskFileSystemProvider.js';
export { URI } from '${upstream}base/common/uri.js';
export { AdapterLogger, LogLevel, NullLogService } from '${upstream}platform/log/common/log.js';
export { LogService } from '${upstream}platform/log/common/logService.js';
export { TestConfigurationService } from '${upstream}platform/configuration/test/common/testConfigurationService.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node',
  target: 'node24', packages: 'external', tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime: 'node' }))] });
const m = await import(pathToFileURL(bundle).href);
const backend = 'download-backend', capability = 'a'.repeat(32), originalToken = process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN;
const proxyKeys = ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy'];
const savedEnvironment = Object.fromEntries(proxyKeys.map(key => [key, process.env[key]]));
const cleanups = [], seen = [];
let callback;
const server = createServer((request, response) => {
  void (async () => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : undefined;
    const record = { method: request.method, path: request.url, headers: request.headers, body };
    seen.push(record);
    await callback(record, request, response);
  })().catch(error => response.destroy(error));
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const origin = `http://127.0.0.1:${server.address().port}`;
const transport = (suffix = capability, requestId = 'original-request') => ({
  url: `${origin}/api/gateway/internal/extension-management/${backend}/downloads/${suffix}`, request_id: requestId,
});
const within = (operation, value = transport()) => m.withExtensionDownloadTransport(value, backend, operation);
function service(http = {}, log = new m.NullLogService()) {
  const configuration = new m.TestConfigurationService({ http: { proxyStrictSSL: true, ...http } });
  const request = new m.ExtensionDownloadRequestService(configuration, { args: { 'force-disable-user-env': true } }, log);
  cleanups.push(() => { request.dispose(); log.dispose(); configuration.onDidChangeConfigurationEmitter.dispose(); });
  return { request, configuration };
}
const download = (request, options = {}, token = m.CancellationToken.None) => request.request({
  url: 'https://artifacts.example.test/item.vsix', callSite: 'extensionGalleryService.download', ...options,
}, token);
beforeEach(() => {
  seen.length = 0;
  for (const key of proxyKeys) delete process.env[key];
  process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN = 'test-local-token';
  callback = (_record, _request, response) => { response.writeHead(200); response.end('artifact'); };
});
afterEach(async () => {
  for (const cleanup of cleanups.splice(0).reverse()) await cleanup();
});
after(async () => {
  server.closeAllConnections();
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  for (const [key, value] of Object.entries(savedEnvironment)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
  if (originalToken === undefined) delete process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN;
  else process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN = originalToken;
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ sourceBefore, sourceAfter: await fingerprint(),
    boundary: '固定 Node、真实 RequestService/nodeRequest/HTTP/gzip/取消/DownloadService/磁盘；Python 回调与原站为具名本机 HTTP 边界，配置为原 TestConfigurationService。未验证真实 Gateway/Python/签名/完整安装。' }, null, 2));
  await run.close();
});

test('五个制品 callSite 只 POST 本次 Python 回调，保留原状态、headers 与 gzip 解压', async () => {
  const { request } = service();
  callback = (_record, _request, response) => {
    response.writeHead(206, { 'content-encoding': 'gzip', 'x-original-response': 'retained' });
    response.end(gzipSync('compressed artifact'));
  };
  for (const callSite of ['extensionGalleryService.download', 'extensionGalleryService.download.fallback',
    'extensionGalleryService.signature', 'extensionGalleryService.signature.fallback', 'extensionManagement.downloadVsix']) {
    const result = await within(() => download(request, { callSite, timeout: 1700, followRedirects: 2,
      headers: { Authorization: 'Bearer gallery', 'X-Multi': ['one', 'two'], Missing: undefined,
        'x-local-token': 'must-not-forward', 'X-Request-ID': 'must-not-forward',
        'x-boxteam-extension-download-error': 'must-not-forward' } }));
    assert.equal(result.res.statusCode, 206);
    assert.equal(result.res.headers['x-original-response'], 'retained');
    assert.equal(result.res.headers['content-encoding'], 'gzip');
    assert.equal(await m.asText(result), 'compressed artifact');
    const last = seen.at(-1);
    assert.equal(last.method, 'POST');
    assert.equal(last.path, new URL(transport().url).pathname);
    assert.equal(last.headers['x-local-token'], 'test-local-token');
    assert.equal(last.headers['x-request-id'], 'original-request');
    assert.deepEqual(last.body, { call_site: callSite, url: 'https://artifacts.example.test/item.vsix',
      headers: { Authorization: 'Bearer gallery', 'X-Multi': ['one', 'two'] },
      timeout_ms: 1700, follow_redirects: 2, proxy_url: null, strict_ssl: true });
  }
});

test('缺少传输明确失败，原查询与其他 callSite 保留直接请求及继承的认证 API', async () => {
  const { request } = service();
  assert.ok(request instanceof m.RequestService);
  assert.equal(await request.resolveProxy('https://example.test'), undefined);
  assert.equal(await request.lookupAuthorization({}), undefined);
  await assert.rejects(download(request), /缺少本次安装的 Python 传输/);
  for (const callSite of ['extensionGalleryService.query', 'extensionGalleryService.manifest',
    'extensionGalleryService.download.other', 'other.download']) {
    assert.equal(await m.asText(await request.request({ type: 'GET', url: `${origin}/original`, callSite }, m.CancellationToken.None)), 'artifact');
  }
  assert.ok(seen.every(record => record.method === 'GET' && record.path === '/original'));
  assert.ok(seen.every(record => record.headers['x-local-token'] === undefined));
});

test('原日志链只记录下载 host、状态和错误类别，不写签名 URL 或敏感请求／响应头', async () => {
  const entries = [];
  const logger = new m.AdapterLogger({ log: (level, args) => entries.push({ level, args }) }, m.LogLevel.Trace);
  cleanups.push(() => logger.dispose());
  const { request } = service({}, new m.LogService(logger));
  const signedUrl = 'https://artifacts.example.test/private-package.vsix?sig=signed-query-secret&token=url-token-secret';
  const options = { url: signedUrl, logUrl: true,
    headers: { Authorization: 'Bearer request-auth-secret', 'X-Download-Token': 'request-header-secret' } };
  callback = (_record, _request, response) => {
    response.writeHead(206, { 'x-provider-access-token': 'response-header-secret', 'set-cookie': 'response-cookie-secret' });
    response.end('artifact');
  };
  const result = await within(() => download(request, options));
  assert.equal(result.res.headers['x-provider-access-token'], 'response-header-secret');
  assert.equal(await m.asText(result), 'artifact');
  assert.ok(entries.some(({ args }) => args[0].includes('artifacts.example.test - begin')));
  assert.ok(entries.some(({ args }) => args[0].includes('artifacts.example.test - end') && args.includes(206)));
  callback = (_record, _request, response) => {
    response.writeHead(502, { 'x-boxteam-extension-download-error': 'transport', 'x-provider-access-token': 'error-header-secret' });
    response.end(`request failed at ${signedUrl}`);
  };
  await assert.rejects(within(() => download(request, options)), error => error.message.includes(signedUrl));
  assert.ok(entries.some(({ args }) => args[0].includes('artifacts.example.test - error') && args.includes('Error')));
  const logged = JSON.stringify(entries);
  for (const secret of [signedUrl, 'private-package.vsix', 'signed-query-secret', 'url-token-secret',
    'request-auth-secret', 'request-header-secret', 'response-header-secret', 'response-cookie-secret', 'error-header-secret']) {
    assert.equal(logged.includes(secret), false, `日志泄漏 ${secret}`);
  }
});

test('回调能力严格绑定当前 Backend，非法 URL 和 request_id 在发请求前拒绝', () => {
  for (const url of [transport().url.replace('127.0.0.1', 'localhost'), transport().url.replace('127.0.0.1', '127.1'),
    transport().url.replace('http:', 'https:'), transport().url.replace('http://', 'http://user:password@'),
    transport().url + '?secret=1', transport().url + '#fragment', transport().url + '?', transport().url + '#',
    transport().url.replace(backend, 'other-backend'), transport().url + '/extra',
    transport().url.replace(capability, 'a'.repeat(31)), transport().url.replace(capability, 'g'.repeat(32))]) {
    assert.throws(() => within(() => assert.fail('非法回调不能进入安装'), { ...transport(), url }));
  }
  for (const request_id of ['', undefined, 'bad\r\nid']) {
    assert.throws(() => within(() => assert.fail('非法 request_id 不能进入安装'), { ...transport(), request_id }));
  }
  const ipv6 = { ...transport(), url: transport().url.replace('127.0.0.1', '[::1]') };
  assert.equal(within(() => 'validated', ipv6), 'validated');
  assert.equal(seen.length, 0);
});

test('AsyncLocalStorage 隔离交错安装，退出后与未提供能力的安装均不继承', async () => {
  const { request } = service(), gate = Promise.withResolvers();
  const first = within(async () => { await gate.promise; return m.asText(await download(request)); }, transport('b'.repeat(32), 'request-B'));
  const second = within(async () => { gate.resolve(); await Promise.resolve(); return m.asText(await download(request)); }, transport('c'.repeat(32), 'request-C'));
  assert.deepEqual(await Promise.all([first, second]), ['artifact', 'artifact']);
  assert.deepEqual(seen.map(record => [record.path.split('/').at(-1), record.headers['x-request-id']]).sort(),
    [['b'.repeat(32), 'request-B'], ['c'.repeat(32), 'request-C']]);
  await assert.rejects(m.withExtensionDownloadTransport(undefined, backend, () => download(request)), /缺少本次安装/);
  await assert.rejects(download(request), /缺少本次安装/);
});

test('保留原配置与环境代理优先级、TLS 和代理授权，undefined 选项不序列化', async () => {
  const { request, configuration } = service({ proxy: 'https://configured-proxy.test:8443', proxyStrictSSL: false,
    proxyAuthorization: 'Basic proxy-only' });
  process.env.HTTPS_PROXY = 'http://environment-proxy.test:8181';
  await within(() => download(request, { headers: { 'proxy-authorization': 'lower-priority-request-auth' } }));
  assert.deepEqual(seen.at(-1).body, { call_site: 'extensionGalleryService.download', url: 'https://artifacts.example.test/item.vsix',
    headers: { 'Proxy-Authorization': 'Basic proxy-only' }, proxy_url: 'https://configured-proxy.test:8443', strict_ssl: false });
  await configuration.setUserConfiguration('http.proxy', '');
  await within(() => download(request));
  assert.equal(seen.at(-1).body.proxy_url, 'http://environment-proxy.test:8181');
  delete process.env.HTTPS_PROXY;
  process.env.https_proxy = 'http://lowercase-proxy.test:8182';
  process.env.HTTP_PROXY = 'http://http-proxy.test:8183';
  await within(() => download(request));
  assert.equal(seen.at(-1).body.proxy_url, 'http://lowercase-proxy.test:8182');
  await within(() => download(request, { url: 'http://artifacts.example.test/item.vsix' }));
  assert.equal(seen.at(-1).body.proxy_url, 'http://http-proxy.test:8183');
  delete process.env.HTTP_PROXY;
  delete process.env.https_proxy;
  await assert.rejects(within(() => download(request)), /代理认证.*没有有效代理/);
  await configuration.setUserConfiguration('http.proxy', 'socks5://unsupported.test:1080');
  await assert.rejects(within(() => download(request)), /只支持 HTTP\(S\) 代理/);
  await assert.rejects(within(() => download(request, { type: 'POST' })), /只支持 HTTP\(S\) GET/);
});

test('Python 传输错误显式抛出，原站 404 仍返回原状态与正文', async () => {
  const { request } = service();
  callback = (_record, _request, response) => {
    response.writeHead(502, { 'x-boxteam-extension-download-error': 'transport' }); response.end('TLS certificate verification failed');
  };
  await assert.rejects(within(() => download(request)), /Python 扩展下载传输失败: TLS certificate verification failed/);
  callback = (_record, _request, response) => { response.writeHead(404); response.end('original missing'); };
  const result = await within(() => download(request));
  assert.equal(result.res.statusCode, 404);
  assert.equal(await m.asText(result), 'original missing');
});

test('gzip 响应头已到达后原始正文断开，消费流必须失败并允许后续请求', { timeout: 3000 }, async () => {
  const { request } = service(), disconnect = Promise.withResolvers();
  const compressed = gzipSync('compressed artifact that must not be accepted partially');
  callback = async (_record, _request, response) => {
    response.writeHead(200, { 'content-encoding': 'gzip', 'content-length': String(compressed.length) });
    response.write(compressed.subarray(0, 24));
    await disconnect.promise;
    response.destroy();
  };
  const context = await within(() => download(request));
  assert.equal(context.res.statusCode, 200);
  const rejected = assert.rejects(m.asText(context), /aborted|premature close|ECONNRESET/i);
  disconnect.resolve();
  await rejected;
  assert.equal(seen.length, 1);
  callback = (_record, _request, response) => response.end('after-disconnect');
  assert.equal(await m.asText(await within(() => download(request))), 'after-disconnect');
});

test('回调 POST 不因连接中断重放，也不跟随 307 重定向', async () => {
  const { request } = service();
  callback = (_record, incoming) => incoming.socket.destroy();
  await assert.rejects(within(() => download(request)), /socket hang up/);
  assert.equal(seen.length, 1);
  callback = (_record, _request, response) => { response.writeHead(307, { location: `${origin}/must-not-follow` }); response.end('redirect'); };
  const result = await within(() => download(request));
  assert.equal(result.res.statusCode, 307);
  assert.equal(await m.asText(result), 'redirect');
  assert.equal(seen.length, 2);
});

test('等待原回调响应时取消沿 nodeRequest 中止本次连接且不重发', async () => {
  const { request } = service(), accepted = Promise.withResolvers(), closed = Promise.withResolvers();
  callback = (_record, incoming, response) => {
    response.on('close', () => closed.resolve());
    accepted.resolve();
  };
  const source = new m.CancellationTokenSource();
  try {
    const pending = within(() => download(request, {}, source.token));
    const rejected = assert.rejects(pending, error => error.name === 'Canceled');
    await accepted.promise;
    source.cancel();
    await rejected;
    await closed.promise;
    assert.equal(seen.length, 1);
  } finally { source.dispose(); }
});

test('原 DownloadService 本地文件无需传输，远程导入沿接缝写入原磁盘 Provider', async () => {
  const { request } = service(), log = new m.NullLogService(), files = new m.FileService(log);
  const disk = new m.DiskFileSystemProvider(log), registration = files.registerProvider('file', disk);
  const downloads = new m.DownloadService(request, files);
  try {
    const workspace = join(run.workspaceRoot, basename(run.runRoot));
    await mkdir(workspace, { recursive: true });
    const local = join(workspace, 'input.vsix'), copied = join(workspace, 'copied.vsix');
    await writeFile(local, 'local-vsix');
    await downloads.download(m.URI.file(local), m.URI.file(copied), 'extensionManagement.downloadVsix');
    assert.equal(await readFile(copied, 'utf8'), 'local-vsix');
    assert.equal(seen.length, 0);
    const remote = m.URI.parse('https://artifacts.example.test/import.vsix'), imported = m.URI.file(join(workspace, 'imported.vsix'));
    await assert.rejects(downloads.download(remote, imported, 'extensionManagement.downloadVsix'), /缺少本次安装/);
    await within(() => downloads.download(remote, imported, 'extensionManagement.downloadVsix'));
    assert.equal(await readFile(imported.fsPath, 'utf8'), 'artifact');
    assert.equal(seen[0].body.call_site, 'extensionManagement.downloadVsix');
  } finally { registration.dispose(); disk.dispose(); files.dispose(); log.dispose(); }
});
