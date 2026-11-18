import { expect } from 'playwright/test';

/** 通过会话树展开 Thread 导航，不打开资源标签或改变当前编辑器组。 */
export async function openSessionThreads(page, { gatewayId, workspaceId, sessionId }) {
  const row = page.getByTestId(`catalog-node-${JSON.stringify([gatewayId, workspaceId])}-${sessionId}`);
  await expect(row).toBeVisible();
  const node = row.locator('..');
  if (await node.getAttribute('aria-expanded') !== 'true') {
    await row.locator('button.session-resource-chevron').click();
  }
  const threads = node.locator(':scope > .session-thread-navigation .child-thread-panel');
  await expect(threads).toBeVisible();
  return threads;
}
