/** 控制仅存在于本场景 ASGI 装具的出站故障，不触碰产品配置或业务存储。 */
export async function searchFault(page, query, mode, categories, workspaceIds) {
  // 被测页面可能占满同源 SSE 连接；控制请求不得排在正等待释放的流后面。
  const request = page.context().request, base = new URL(page.url()).origin;
  const credential = await (await request.get(`${base}/api/gateway/auth/local-credential`)).json();
  const response = await request.fetch(`${base}/_test/search/${encodeURIComponent(query)}`, {
    method: mode ? 'POST' : 'GET', headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
    data: mode ? { mode, ...(categories ? { categories } : {}), ...(workspaceIds ? { workspace_ids: workspaceIds } : {}) } : undefined,
  });
  if (!response.ok()) throw new Error(`搜索故障装具返回 ${response.status()}: ${await response.text()}`);
  return response.json();
}
