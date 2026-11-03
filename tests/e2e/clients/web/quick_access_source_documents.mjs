import { expect } from 'playwright/test';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { searchRequest, searchReceipt } from '../../../support/workbench/search-observation.mjs';

/** 实际 Opener／原只读编辑器／历史；源文件只是产品读取的输入，不预造业务状态。 */
export async function verifySourceDocuments({ page, command, api, result, owner, open }) {
  const reads = [], queries = [], writes = [];
  result.sourceDocuments = { reads, queries, writes };
  const observe = request => {
    const path = new URL(request.url()).pathname;
    const body = request.method() === 'POST' && path === '/api/v1/resources/navigation' ? request.postDataJSON() : undefined;
    if (body?.operation === 'read') reads.push({ ...body, route: request.headers()['x-boxteam-workspace-id'] });
    const observed = searchRequest(request);
    if (observed?.path === '/api/gateway/search' || body?.operation === 'search'
      || ['/api/v1/session-catalog/search', '/api/v1/session-threads/search'].includes(path)) queries.push(observed);
    // 原工作区观察流用 POST 建立只读订阅；其余非 GET 请求仍逐一检查。
    if (!['GET', 'HEAD'].includes(request.method()) && !['/api/gateway/search', '/api/gateway/resource-addresses/parse', '/api/v1/resources/navigation',
      '/api/v1/workspace/observations/stream'].includes(path)) {
      writes.push({ path, method: request.method() });
    }
  };
  page.on('request', observe);
  const editor = () => page.locator('.editor-group-container.active .editor-instance .monaco-editor').filter({ visible: true });
  const expectSource = async text => {
    await expect(editor().locator('.view-lines')).toContainText(text);
    // 固定上游 native/screenReaderSupport.ts 用此属性表达只读；实际键入与无写请求另行验证。
    await expect(editor().getByRole('textbox')).toHaveAttribute('aria-autocomplete', 'none');
  };
  try {
    result.phase = 'canonical-readonly-source-documents';
    const other = await api('/api/v1/workspace', owner.routeB);
    const prefix = workspaceId => `boxteam://${owner.gatewayId}/workspace/${workspaceId}/resources/`;
    const agentsA = prefix(owner.workspaceId) + 'agent-spec/root/AGENTS.md';
    const agentsB = prefix(other.workspace_id) + 'agent-spec/root/AGENTS.md';
    const skillB = prefix(other.workspace_id) + 'skills/quick-source/SKILL.md';
    const addressA = await open(agentsA);
    expect(addressA.gateway_id).toBe(owner.gatewayId);
    expect(addressA.scope_id).toBe(owner.workspaceId);
    await expectSource('source AGENTS A');
    const addressB = await open(agentsB);
    expect(addressB.gateway_id).toBe(owner.gatewayId);
    expect(addressB.scope_id).toBe(other.workspace_id);
    await expectSource('source AGENTS B');
    const before = await editor().locator('.view-lines').innerText();
    await editor().getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText('unexpected source mutation');
    await expectSource('source AGENTS B');
    await expect(editor().locator('.view-lines')).not.toContainText('unexpected source mutation');
    expect(await editor().locator('.view-lines').innerText()).toBe(before);

    result.phase = 'source-owner-search-and-history';
    const quick = page.locator('.quick-input-widget');
    const completed = page.waitForRequest(request => {
      const observed = searchRequest(request);
      return observed?.path === '/api/gateway/search' && observed.query === 'AGENTS' && observed.categories.includes('catalog')
        && observed.owners.length === 1 && observed.owners[0] === other.workspace_id;
    });
    await command('workbench.action.quickOpen', 'AGENTS');
    const receipt = await searchReceipt(await completed);
    expect(receipt.status).toBe(200);
    expect(receipt.summary.state).toBe('complete');
    const history = quick.locator('[data-quick-input-id^="boxteam-source:"]');
    await expect(history).toHaveCount(1);
    await history.click();
    await expect(quick).not.toBeVisible();
    await expectSource('source AGENTS B');
    expect(queries.length).toBeGreaterThan(0);
    expect(queries.every(query => query.path === '/api/gateway/search' && query.owners.length === 1 && query.owners[0] === other.workspace_id)).toBe(true);

    result.phase = 'skill-readonly-and-source-reopen';
    const skill = await open(skillB);
    await expectSource('source Skill B');
    await command('workbench.action.closeActiveEditor');
    await open(skillB);
    await expectSource('source Skill B');
    expect(reads.filter(read => read.uri === addressA.uri && read.route === owner.routeA)).toHaveLength(1);
    expect(reads.filter(read => read.uri === addressB.uri && read.route === owner.routeB)).toHaveLength(1);
    expect(reads.filter(read => read.uri === skill.uri && read.route === owner.routeB)).toHaveLength(2);
    expect(reads.every(read => read.read_format === 'auto')).toBe(true);
    expect(writes).toEqual([]);
    expect(await readFile(join(process.env.BOXTEAM_CATALOG_WORKSPACE, 'other/AGENTS.md'), 'utf8')).toContain('source AGENTS B');
    result.sourceDocuments = { readonly: true, exactOwners: true, historyIsolated: true, reopenReadsOwner: true,
      noMutationOrExecution: true, reads, queries, writes };
  } finally { page.off('request', observe); }
}
