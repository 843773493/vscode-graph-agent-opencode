/** 控制仅存在于本场景 ASGI 装具的出站故障，不触碰产品配置或业务存储。 */
export async function searchFault(page, query, mode, categories, workspaceIds) {
  return page.evaluate(async ({ query, mode, categories, workspaceIds }) => {
    const credential = await (await fetch('/api/gateway/auth/local-credential')).json();
    const response = await fetch(`/_test/search/${encodeURIComponent(query)}`, {
      method: mode ? 'POST' : 'GET', headers: { 'X-Local-Token': credential.data.token, 'Content-Type': 'application/json' },
      body: mode ? JSON.stringify({ mode, ...(categories ? { categories } : {}), ...(workspaceIds ? { workspace_ids: workspaceIds } : {}) }) : undefined,
    });
    if (!response.ok) throw new Error(`搜索故障装具返回 ${response.status}: ${await response.text()}`);
    return response.json();
  }, { query, mode, categories, workspaceIds });
}
