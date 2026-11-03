import { createServer } from 'node:http';
import { createReadStream, existsSync, readFileSync } from 'node:fs';
import { stat } from 'node:fs/promises';
import { resolve, extname, sep, join, relative } from 'node:path';
import httpProxy from 'http-proxy';

/** @type {Record<string, string>} */
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.wasm': 'application/wasm', '.svg': 'image/svg+xml', '.ttf': 'font/ttf' };
/** @param {{ root: string; gateway?: string }} options */
export function createWorkbenchServer({ root: directory, gateway }) {
  const root = resolve(directory);
  const manifest = join(root, 'immutable-assets.json');
  const assets = existsSync(manifest) ? JSON.parse(readFileSync(manifest, 'utf8')) : [];
  if (!Array.isArray(assets) || !assets.every(path => typeof path === 'string')) throw new Error(`静态资源缓存清单必须是路径列表：${manifest}`);
  const immutableAssets = new Set(assets);
  const proxy = httpProxy.createProxyServer({ target: gateway, ws: true });
  proxy.on('proxyReq', (outgoing, _request, response) => {
    // SSE 请求体结束后，页面关闭只触发响应 close；同步释放该请求的上游连接。
    response.once('close', () => outgoing.destroy());
  });
  proxy.on('error', (cause, request, response) => {
    // 路径足以定位失败；WebSocket 查询参数包含凭据，不得写入日志。
    const path = new URL(request.url ?? '/', 'http://localhost').pathname;
    const message = `Gateway 代理失败：${path}，${cause.message}`;
    console.error(message);
    if ('writeHead' in response) {
      if (!response.headersSent) response.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
      response.end(message);
    } else {
      response.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n');
    }
  });

  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://localhost');
    if (url.pathname.startsWith('/api/')) {
      if (!gateway) { response.writeHead(503); response.end('未指定 --gateway'); return; }
      // HTTP、SSE 和 WebSocket 均透明传输，避免用 fetch 丢失 upgrade 或缓冲流式事件。
      proxy.web(request, response);
      return;
    }
    const path = resolve(root, `.${decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname)}`);
    if (!path.startsWith(root + sep)) { response.writeHead(403); response.end('禁止访问'); return; }
    const file = await stat(path).catch(cause => {
      if (cause.code === 'ENOENT' || cause.code === 'ENOTDIR') return null;
      throw cause;
    });
    if (!file?.isFile()) { response.writeHead(404, { 'Cache-Control': 'no-store' }); response.end(`资源不存在：${url.pathname}`); return; }
    const resourcePath = relative(root, path).split(sep).join('/');
    const callback = resourcePath === 'mcp-oauth-callback.html';
    // 与原 VS Code serveFile 一样只使用 stat 观察值；不为再验证重读 bundle 或计算正文 hash。
    const etag = `W/"${[file.ino, file.size, file.mtimeMs].join('-')}"`;
    const headers = {
      'Content-Type': mime[extname(path)] ?? 'application/octet-stream',
      'Cache-Control': callback ? 'no-store' : immutableAssets.has(resourcePath)
        ? 'public, max-age=31536000, immutable' : 'no-cache',
      ...(callback ? { 'Referrer-Policy': 'no-referrer' } : { ETag: etag }),
    };
    if (!callback && ['GET', 'HEAD'].includes(request.method) && request.headers['if-none-match']?.split(',')
      .some(value => value.trim() === '*' || value.trim().replace(/^W\//, '') === etag.slice(2))) {
      response.writeHead(304, headers); response.end(); return;
    }
    if (request.method === 'HEAD') { response.writeHead(200, { ...headers, 'Content-Length': file.size }); response.end(); return; }
    const stream = createReadStream(path);
    response.once('close', () => stream.destroy());
    stream.on('error', cause => response.destroy(cause));
    stream.once('open', () => { response.writeHead(200, { ...headers, 'Content-Length': file.size }); stream.pipe(response); });
  });
  server.on('upgrade', (request, socket, head) => {
    if (!gateway || !new URL(request.url ?? '/', 'http://localhost').pathname.startsWith('/api/')) {
      socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n');
      return;
    }
    proxy.ws(request, socket, head);
  });
  // SSE 由后端心跳维持，不以开发服务器的空闲期限截断。
  server.timeout = 0;
  server.on('close', () => proxy.close());
  return server;

}
