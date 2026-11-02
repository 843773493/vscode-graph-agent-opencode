import { expect } from 'playwright/test';

/** 检查原列表的实际展示，不以 Provider 返回字段代替渲染证据。 */
export async function verifyResourcePresentation({ quick, sessionId }) {
  const session = quick.locator('[data-quick-input-id]').filter({ hasText: `会话 · ${sessionId}` });
  const thread = quick.locator('[data-quick-input-id]').filter({ hasText: `main Thread · ${sessionId} /` });
  const file = quick.locator('[data-quick-input-id]').filter({ hasText: 'quickdiscover-file.txt' });
  const rows = [session, thread, file];
  for (const [index, row] of rows.entries()) {
    await expect(row).toHaveCount(1);
    await expect(row.locator('.quick-input-list-row').first().locator('.label-name')).toContainText(index === 2 ? 'quickdiscover-file.txt' : 'quickdiscover A 会话');
    await expect(row.locator('.label-description')).toContainText('home');
    await expect(row.locator('.quick-input-list-label-meta')).toContainText(index === 2 ? 'quickdiscover-file.txt' : sessionId);
    await expect(row.locator(`.quick-input-list-icon.codicon-${index === 2 ? 'file' : 'comment-discussion'}`)).toBeVisible();
    await expect.poll(async () => (await row.locator('.quick-input-list-row').first().locator('.label-name .highlight').allTextContents()).join('')).toBe('quickdiscover');
    await row.hover();
    await expect(row.getByRole('button', { name: '在侧边打开', exact: true })).toHaveCount(1);
  }
  return Promise.all(rows.map(row => row.evaluate(element => ({
    name: element.querySelector('.quick-input-list-row .label-name')?.textContent,
    description: element.querySelector('.label-description')?.textContent,
    detail: element.querySelector('.quick-input-list-label-meta')?.textContent,
    icon: element.querySelector('.quick-input-list-icon')?.className,
    highlights: [...element.querySelectorAll('.quick-input-list-row:first-child .label-name .highlight')].map(span => span.textContent),
  }))));
}

/** 侧边按钮经键盘触发；后台接受继续输入，移除历史不关闭标签或删除业务对象。 */
export async function verifyResourceOpenActions({ page, command, api, result, owner, session }) {
  result.phase = 'default-quick-access-open-actions';
  const quick = page.locator('.quick-input-widget'), input = quick.locator('input').first();
  const groups = page.locator('.editor-group-container');
  await expect(groups).toHaveCount(1);
  const original = groups.first();
  const originalThread = original.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${session.session_id}"]`);
  await expect(originalThread).toBeVisible();

  await command('workbench.action.quickOpen', 'quickdiscover-file.txt');
  const file = quick.getByRole('option', { name: /^quickdiscover-file\.txt / });
  await expect(file).toHaveCount(1);
  await expect(file).toHaveClass(/focused/);
  const side = file.getByRole('button', { name: '在侧边打开', exact: true });
  await side.hover();
  await expect(page.getByRole('tooltip')).toContainText('在侧边打开');
  await input.click();
  for (let step = 0; step < 6 && !await side.evaluate(element => element === document.activeElement); step++) await page.keyboard.press('Tab');
  await expect(side).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(quick).not.toBeVisible();
  await expect(groups).toHaveCount(2);
  await expect(originalThread).toBeVisible();
  const opened = groups.nth(1);
  await expect(opened).toHaveClass(/\bactive\b/);
  await expect(opened.locator('.editor-instance .view-lines')).toContainText('discover A line one');
  await expect(opened.getByRole('tab', { name: /quickdiscover-file\.txt/ }).locator('.monaco-icon-label')).toHaveClass(/italic/);

  await command('workbench.action.quickOpen', 'quickdiscover-dir/deep-file');
  const nested = quick.getByRole('option', { name: /^deep-file\.txt / });
  await expect(nested).toHaveClass(/focused/);
  await input.press('End');
  await input.press('ArrowRight');
  await expect(quick).toBeVisible();
  await expect(input).toBeFocused();
  await expect(opened.locator('.editor-instance .view-lines')).toContainText('nested discover A');
  await expect(opened.getByRole('tab', { name: /deep-file\.txt/ }).locator('.monaco-icon-label')).not.toHaveClass(/italic/);
  await expect(originalThread).toBeVisible();
  // 第二次接受验证浮层仍可继续操作，目标组保持发起组。
  await input.fill('quick-scope.txt');
  await expect(quick.getByRole('option', { name: /^quick-scope\.txt / })).toHaveClass(/focused/);
  await input.press('End');
  await input.press('ArrowRight');
  await expect(input).toBeFocused();
  await expect(opened.locator('.editor-instance .view-lines')).toContainText('diskword quickscope');
  await expect(opened.getByRole('tab', { name: /deep-file\.txt/ })).toBeVisible();
  await page.keyboard.press('Escape');
  await command('workbench.action.focusFirstEditorGroup');
  await command('workbench.action.closeEditorsInOtherGroups');
  await expect(groups).toHaveCount(1);
  await expect(originalThread).toBeVisible();

  await command('workbench.action.quickOpen', '');
  const history = quick.getByRole('option', { name: /^quickdiscover A 会话 / });
  await expect(history).toHaveCount(1);
  await history.hover();
  await history.getByRole('button', { name: '从最近打开中移除', exact: true }).click();
  await expect(history).toHaveCount(0);
  await expect(quick).toBeVisible();
  await expect(original.getByRole('tab', { name: /quickdiscover A 会话/ })).toBeVisible();
  const retained = (await api(`/api/v1/sessions/${session.session_id}`, { headers: { 'X-BoxTeam-Workspace-Id': owner.route } })).data;
  expect(retained.session_id).toBe(session.session_id);
  await page.keyboard.press('Escape');
  result.defaultOpenActions = { keyboardSideButton: true, sidePreview: true, continuousBackground: true,
    backgroundPinned: true, sourceGroupPreserved: true, historyRemovalKeepsTabAndSession: true };
}
