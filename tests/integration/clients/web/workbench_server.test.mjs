import { expect, test } from 'bun:test';
import { mkdir, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

// 使用与 Electron 相同的 Node HTTP 实现；Gateway 是随机本地端口上的显式替身。
const observer = String.raw`
import { createServer, request } from 'node:http';
import { once } from 'node:events';
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
const input = JSON.parse(readFileSync(0, 'utf8'));
const { createWorkbenchServer } = await import(input.module);
const servers = [], sockets = new Set(), clients = [], upstreams = [];
const observed = { scenario: input.scenario };
async function listen(server) {
  servers.push(server);
  server.on('connection', socket => {
    sockets.add(socket);
    socket.once('close', () => sockets.delete(socket));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return 'http://127.0.0.1:' + server.address().port;
}
async function waitFor(predicate) {
  const end = performance.now() + 1000;
  while (!predicate() && performance.now() < end) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  return predicate();
}
function open(origin, path) {
  const client = request(origin + path, { method: 'POST', headers: {
    'Content-Type': 'application/json', 'X-BoxTeam-Workspace-Id': path,
    'Cookie': 'fixture-session=local',
  } });
  clients.push(client);
  client.end(JSON.stringify({ thread_id: path }));
  return client;
}
try {
  const gateway = createServer((incoming, response) => {
    const upstream = { path: incoming.url, owner: incoming.headers['x-boxteam-workspace-id'],
      cookie: incoming.headers.cookie, body: '', closed: false, response };
    upstreams.push(upstream);
    response.once('close', () => { upstream.closed = true; });
    incoming.on('data', chunk => { upstream.body += chunk; });
    incoming.on('end', () => {
      if (input.scenario === 'pending') return;
      if (input.scenario === 'json') {
        response.writeHead(201, { 'Content-Type': 'application/json', 'X-Request-ID': 'fixture-request',
          'Set-Cookie': 'fixture-session=returned; HttpOnly' });
        response.end(JSON.stringify({ path: upstream.path, owner: upstream.owner,
          cookie: upstream.cookie, body: JSON.parse(upstream.body) }));
      } else {
        response.writeHead(200, { 'Content-Type': 'text/event-stream', 'X-Request-ID': 'fixture-stream' });
        response.write('data: ready\n\n');
      }
    });
  });
  const target = await listen(gateway);
  const origin = await listen(createWorkbenchServer({ root: input.root, gateway: target }));
  if (input.scenario === 'streams') {
    const responses = [];
    for (const path of ['/api/stream/first', '/api/stream/second']) {
      const client = open(origin, path);
      const [response] = await once(client, 'response');
      const [chunk] = await once(response, 'data');
      responses.push(response);
      if (chunk.toString() !== 'data: ready\n\n') throw new Error('初始 SSE 正文丢失');
    }
    clients[0].destroy();
    observed.firstReleased = await waitFor(() => upstreams[0].closed);
    observed.secondStillOpen = !upstreams[1].closed;
    const next = once(responses[1], 'data');
    upstreams[1].response.write('data: second-only\n\n');
    observed.secondMessage = (await next)[0].toString();
    clients[1].destroy();
    observed.secondReleased = await waitFor(() => upstreams[1].closed);
  } else if (input.scenario === 'pending') {
    const client = open(origin, '/api/pending');
    const errors = [];
    client.on('error', error => errors.push(error.code));
    if (!await waitFor(() => upstreams.length === 1 && upstreams[0].body.length > 0)) {
      throw new Error('未取得上游请求');
    }
    client.destroy();
    observed.releasedBeforeHeaders = await waitFor(() => upstreams[0].closed);
    observed.clientErrors = errors;
  } else if (input.scenario === 'json') {
    const client = open(origin, '/api/json');
    const [response] = await once(client, 'response');
    let body = '';
    for await (const chunk of response) body += chunk;
    observed.response = { status: response.statusCode, headers: response.headers, body: JSON.parse(body) };
  } else if (input.scenario === 'static') {
    const read = async (path, headers = {}, method = 'GET') => {
      const response = await fetch(origin + path, { headers, method });
      return { status: response.status, headers: Object.fromEntries(response.headers), body: await response.text() };
    };
    observed.reads = {};
    for (const path of ['/index.html', '/main.js', '/worker.js', '/font-ABC12345.ttf', '/unlisted-ABC12345.js']) {
      const cold = await read(path);
      observed.reads[path] = { cold, warm: await read(path, { 'If-None-Match': cold.headers.etag }) };
    }
    const old = observed.reads['/worker.js'].cold;
    writeFileSync(join(input.root, 'worker.js'), 'worker-version-two');
    observed.updated = await read('/worker.js', { 'If-None-Match': old.headers.etag });
    observed.head = await read('/worker.js', {}, 'HEAD');
    unlinkSync(join(input.root, 'worker.js'));
    observed.missing = await read('/worker.js', { 'If-None-Match': observed.updated.headers.etag });
    observed.oauth = await read('/%6dcp-oauth-callback.html?code=private-code');
    observed.multi = await read('/main.js', { 'If-None-Match': '"different", ' + observed.reads['/main.js'].cold.headers.etag.slice(2) });
  } else throw new Error('未知场景: ' + input.scenario);
  observed.requests = upstreams.map(({ response, ...entry }) => entry);
  // 在 fixture 强制清理之前取证，不能把兜底清理误当成代理释放。
  console.log(JSON.stringify(observed));
} finally {
  for (const client of clients) client.destroy();
  for (const socket of sockets) socket.destroy();
  await Promise.all(servers.map(server => new Promise((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
  })));
}
`;

const scope = await TestRunContext.fromTestFile('tests/integration/clients/web/workbench_server.test.mjs').prepare();
await writeFile(join(scope.artifactsDir, 'observer.mjs'), observer);

for (const scenario of ['streams', 'pending', 'json', 'static']) {
  test(`Workbench HTTP 代理：${scenario}`, async () => {
    const directory = join(scope.artifactsDir, scenario);
    await mkdir(directory);
    const root = join(scope.workspaceRoot, scenario);
    await mkdir(root, { recursive: true });
    if (scenario === 'static') {
      for (const name of ['index.html', 'main.js', 'worker.js', 'font-ABC12345.ttf', 'unlisted-ABC12345.js', 'mcp-oauth-callback.html']) {
        await writeFile(join(root, name), name + '-v1');
      }
      await writeFile(join(root, 'immutable-assets.json'), JSON.stringify(['font-ABC12345.ttf']));
    }
    const input = {
      module: pathToFileURL(resolve('src/clients/workbench/scripts/server.mjs')).href,
      root, scenario,
    };
    // http-proxy 的已知弃用告警与连接行为无关；其余 stderr 仍按原件审计。
    const command = ['node', '--disable-warning=DEP0060', '--input-type=module', '-e', observer];
    const result = spawnSync(command[0], command.slice(1), {
      input: JSON.stringify(input), encoding: 'utf8', timeout: 8000,
    });
    await Promise.all([
      writeFile(join(directory, 'input.json'), JSON.stringify(input, null, 2)),
      writeFile(join(directory, 'stdout.raw'), result.stdout ?? ''),
      writeFile(join(directory, 'stderr.raw'), result.stderr ?? ''),
      writeFile(join(directory, 'result.json'), JSON.stringify({ command, cwd: process.cwd(),
        exitCode: result.status, signal: result.signal, error: result.error?.message, timeoutMs: 8000 }, null, 2)),
    ]);
    expect(result.error).toBeUndefined();
    expect(result.signal).toBeNull();
    expect(result.status).toBe(0);
    expect(result.stderr).toBe('');
    const observed = JSON.parse(result.stdout);
    if (scenario === 'streams') {
      expect(observed.firstReleased).toBe(true);
      expect(observed.secondStillOpen).toBe(true);
      expect(observed.secondMessage).toBe('data: second-only\n\n');
      expect(observed.secondReleased).toBe(true);
      expect(observed.requests.map(entry => ({ owner: entry.owner, body: JSON.parse(entry.body) }))).toEqual([
        { owner: '/api/stream/first', body: { thread_id: '/api/stream/first' } },
        { owner: '/api/stream/second', body: { thread_id: '/api/stream/second' } },
      ]);
    } else if (scenario === 'pending') {
      expect(observed.releasedBeforeHeaders).toBe(true);
      expect(observed.clientErrors).toEqual(['ECONNRESET']);
    } else if (scenario === 'json') {
      expect(observed.response.status).toBe(201);
      expect(observed.response.headers['x-request-id']).toBe('fixture-request');
      expect(observed.response.headers['set-cookie']).toEqual(['fixture-session=returned; HttpOnly']);
      expect(observed.response.body).toEqual({ path: '/api/json', owner: '/api/json',
        cookie: 'fixture-session=local', body: { thread_id: '/api/json' } });
    } else {
      for (const [path, { cold, warm }] of Object.entries(observed.reads)) {
        const policy = path.endsWith('.ttf') ? 'public, max-age=31536000, immutable' : 'no-cache';
        expect(cold.status).toBe(200); expect(cold.body).toBe(path.slice(1) + '-v1');
        expect(cold.headers['cache-control']).toBe(policy);
        expect(warm.status).toBe(304); expect(warm.body).toBe('');
        expect(warm.headers['cache-control']).toBe(policy);
      }
      expect(observed.updated.status).toBe(200); expect(observed.updated.body).toBe('worker-version-two');
      expect(observed.updated.headers.etag).not.toBe(observed.reads['/worker.js'].cold.headers.etag);
      expect(observed.head.status).toBe(200); expect(observed.head.body).toBe('');
      expect(observed.head.headers['content-length']).toBe(String('worker-version-two'.length));
      expect(observed.missing.status).toBe(404); expect(observed.missing.headers['cache-control']).toBe('no-store');
      expect(observed.oauth.status).toBe(200); expect(observed.oauth.headers['cache-control']).toBe('no-store');
      expect(observed.oauth.headers['referrer-policy']).toBe('no-referrer');
      expect(observed.oauth.headers.etag).toBeUndefined();
      expect(observed.multi.status).toBe(304); expect(observed.requests).toEqual([]);
    }
  });
}
