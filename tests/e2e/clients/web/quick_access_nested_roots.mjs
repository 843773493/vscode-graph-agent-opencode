import { expect } from 'playwright/test';
import { searchReceipt } from '../../../support/workbench/search-observation.mjs';

/** 原 Search Editor 通过实际 QueryBuilder 取得父子分支及各自选项，不手工提供查询结果。 */
export async function verifyNestedSearchRoots({ page, command, target, result }) {
  const resource = name => page.evaluate(({ target, name }) => globalThis.workbench.URI.from({ scheme: 'boxteam-file', authority: target.gatewayId,
    path: `/${target.workspaceId}/${encodeURIComponent(target.rootUri)}${name ? '/' + name : ''}` }).toString(), { target, name });
  result.phase = 'create-real-dirty-models';
  for (const [name, body] of [['nested/drop.txt', 'partitionword EXCLUDED DRAFT'], ['nested/keep.md', 'partitionword ACCEPTED DRAFT']]) {
    await command('_workbench.open', await resource(name), [undefined, { pinned: true }]);
    const editor = page.locator('.editor-instance .monaco-editor').filter({ visible: true });
    await expect(editor.locator('.view-lines')).toContainText('partitionword');
    await editor.getByRole('textbox').focus();
    await page.keyboard.press('ControlOrMeta+A'); await page.keyboard.insertText(body);
    await expect(editor.locator('.view-lines')).toContainText(body);
  }
  // 原 QueryBuilder 支持明确的目录绝对路径；路径来自同一实际编辑器投影，不创建第二文件根。
  const rootPath = await page.evaluate(resource => globalThis.workbench.URI.parse(resource).path, await resource(''));
  const request = onlyOpenEditors => command('search.action.openEditor', { query: 'partitionword', location: 'new', triggerSearch: true,
    scope: { version: 1, kind: 'workspace', workspaces: [target] }, contextLines: 0, onlyOpenEditors,
    filesToInclude: `${rootPath}/*.txt, ${rootPath}/nested/*.md`, useExcludeSettingsAndIgnoreFiles: false });
  result.phase = 'search-independent-roots';
  const pending = page.waitForRequest(request => new URL(request.url()).pathname === '/api/gateway/search'
    && request.postDataJSON().queries.some(query => query.query === 'partitionword'));
  await request(false);
  const sent = await pending, wire = sent.postDataJSON(), receipt = await searchReceipt(sent);
  expect(receipt.status).toBe(200); expect(receipt.summary.state).toBe('complete');
  expect(receipt.scope.owners).toEqual([{ gateway_id: target.gatewayId, workspace_id: target.workspaceId }]);
  const queries = wire.queries[0].file_targets.map(item => item.target);
  expect(queries).toHaveLength(2);
  expect(queries[0].uri).toBe(target.rootUri);
  expect(queries[0].search_options.excluded_subtree_uris).toEqual([target.rootUri + '/nested']);
  expect(queries[1].uri).toBe(target.rootUri + '/nested');
  expect(queries[1].search_options.excluded_resource_uris).toEqual([target.rootUri + '/nested/keep.md']);
  const hits = receipt.pages.flatMap(event => event.page.files.matches);
  expect(hits.map(hit => hit.uri)).toEqual([target.rootUri + '/parent.txt']);
  const search = page.locator('.search-editor').filter({ visible: true }).locator('.view-lines');
  await expect(search).toContainText('partitionword parent disk');
  await expect(search).toContainText('partitionword ACCEPTED DRAFT');
  await expect(search).not.toContainText('EXCLUDED');
  await expect(search).not.toContainText('The result set only contains a subset of all matches');
  result.partitioned = true; result.filteredModel = true; result.request = wire; result.nestedReceipt = receipt;
  result.phase = 'only-open-editors-uses-the-same-partition';
  const searches = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/gateway/search') searches.push(request.postDataJSON()); });
  await request(true);
  const local = page.locator('.search-editor').filter({ visible: true }).locator('.view-lines');
  await expect(local).toContainText('partitionword ACCEPTED DRAFT');
  await expect(local).not.toContainText('EXCLUDED'); await expect(local).not.toContainText('parent disk');
  expect(searches).toEqual([]);
  result.onlyOpenEditors = true;
}
