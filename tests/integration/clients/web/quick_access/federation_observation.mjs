import { expect } from 'playwright/test';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../../../../../src/shared/sse.js';

/** 浏览器只观察真实联邦／文件请求；不从单个 SSE 请求臆测 Gateway 的实际分发。 */
export function searchRequest(request) {
  const path = new URL(request.url()).pathname;
  if (path !== '/api/gateway/search' && !['/api/v1/session-catalog/search', '/api/v1/session-threads/search', '/api/v1/workspace/files/roots'].includes(path)
    && path !== '/api/v1/resources/navigation') return undefined;
  const body = request.method() === 'POST' ? request.postDataJSON() : undefined;
  if (path === '/api/v1/resources/navigation' && body?.operation !== 'search') return undefined;
  return { path, body, query: path === '/api/gateway/search' ? body.queries[0].query : new URL(request.url()).searchParams.get('query') ?? body?.query,
    owners: path === '/api/gateway/search' ? (body.scope.mode === 'resume' ? body.scope.scope.owners : body.scope.owners).map(owner => owner.workspace_id) : undefined,
    route: request.headers()['x-boxteam-workspace-id'], issuedAt: Date.now() / 1000,
    deadline: body?.deadline_at ?? request.headers()['x-boxteam-name-search-deadline'] ?? request.headers()['x-boxteam-resource-navigation-deadline'] };
}

export async function searchReceipt(request) {
  const response = await request.response(), observed = searchRequest(request);
  const header = response.headers()['x-request-id'];
  if (observed.path !== '/api/gateway/search' || response.status() !== 200) {
    const body = await response.json();
    expect(body.request_id).toBeTruthy(); expect(body.request_id).toBe(header);
    return { path: observed.path, query: observed.query, status: response.status(), requestId: body.request_id, header };
  }
  const events = [];
  // 复用唯一协议分帧器；这里读取完成的原始响应，不另造 SSE 编码或领域假数据。
  await consumeSseResponse(new Response(await response.body(), { headers: response.headers() }), {
    events: { search: defineSseEvent(decodeJsonSseData, event => events.push(event)) },
  });
  expect(events[0].type).toBe('scope'); expect(events.at(-1).type).toBe('end');
  expect(events.every((event, index) => event.sequence === index + 1 && event.request_id === header)).toBe(true);
  return { path: observed.path, query: observed.query, status: response.status(), requestId: header, header,
    scope: events[0].scope.scope, summary: events.at(-1).summary };
}

/** 控制仅存在于本场景 ASGI 装具的出站故障，不触碰产品配置或业务存储。 */
export async function searchFault(page, query, mode, categories) {
  return page.evaluate(async ({ query, mode, categories }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(`/_test/search/${encodeURIComponent(query)}`, {
      method: mode ? 'POST' : 'GET', headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
      body: mode ? JSON.stringify({ mode, ...(categories ? { categories } : {}) }) : undefined,
    });
    if (!response.ok) throw new Error(`搜索故障装具返回 ${response.status}: ${await response.text()}`);
    return response.json();
  }, { query, mode, categories });
}
