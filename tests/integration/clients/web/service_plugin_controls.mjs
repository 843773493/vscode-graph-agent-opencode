import { expect } from 'playwright/test';

// 操作正式侧栏与原版菜单；菜单点击保留150ms按压，遵守原版打开后100ms的鼠标防误触保护。
export async function selectPluginScope(page, scope) {
  const view = page.locator('.service-plugin-packages-view');
  await view.getByRole('button', { name: '插件管理范围及安装', exact: true }).click();
  await page.getByRole('menuitem', { name: { workspace: '当前工作区', gateway: '当前 Gateway', defaults: '以后新建工作区' }[scope], exact: true }).click({ delay: 150 });
}

export async function pluginScopeAction(page, label) {
  await page.locator('.service-plugin-packages-view').getByRole('button', { name: '插件管理范围及安装', exact: true }).click();
  return page.getByRole('menuitem', { name: label, exact: true });
}

export async function pluginPackageAction(page, row, label) {
  await row.locator('h3').getByRole('button').click();
  return page.getByRole('menuitem', { name: label, exact: true });
}

export async function selectPluginWorkspace(page, name) {
  const selector = page.locator('.service-plugin-packages-view').getByRole('combobox', { name: '插件管理工作区', exact: true });
  await selector.selectOption({ label: name });
  await expect(page.locator('.quick-input-widget')).not.toBeVisible();
}
