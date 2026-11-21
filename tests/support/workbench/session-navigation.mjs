import { expect } from 'playwright/test';

/** 通过发起编辑器组的会话资源标签打开 Thread 索引，不切换会话树或编辑器组。 */
export async function openSessionThreads(page, { workspaceId, sessionId }) {
  const thread = page.locator('.editor-group-container.active')
    .locator(`section.boxteam-thread[data-workspace-id="${workspaceId}"][data-session-id="${sessionId}"]`);
  await expect(thread).toBeVisible();
  const toggle = thread.getByRole('button', { name: '切换会话资源', exact: true });
  if (await toggle.getAttribute('aria-expanded') !== 'true') {
    await toggle.click();
  }
  await thread.getByRole('tab', { name: 'Thread', exact: true }).click();
  const threads = thread.getByRole('tabpanel', { name: 'Thread', exact: true }).locator('.child-thread-panel');
  await expect(threads).toBeVisible();
  return threads;
}
