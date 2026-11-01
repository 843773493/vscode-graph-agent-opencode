import assert from "node:assert/strict";
import { expect } from "playwright/test";

export async function openWorkbench(page, baseUrl) {
  const credential = await page.context().request.get(`${baseUrl}/api/gateway/auth/local-credential`);
  assert.equal(credential.status(), 200);
  const access = await page.context().request.post(`${baseUrl}/api/gateway/users/guest`, {
    headers: { "X-Local-Token": (await credential.json()).data.token },
    data: { tracking: { source: "persistent-tasks-workbench" } },
  });
  assert.equal(access.status(), 200, await access.text());
  const target = new URL(baseUrl);
  await page.goto(target.href, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench .monaco-workbench")).toBeVisible();
}

export async function selectThread(page, owner, groupIndex) {
  assert([0, 1].includes(groupIndex), "任务场景必须明确选择第一或第二编辑器组");
  const group = page.locator(".editor-group-container").nth(groupIndex);
  await expect(group).toBeVisible();
  await page.evaluate(command => globalThis.workbench.commands.executeCommand(command),
    groupIndex === 0 ? "workbench.action.focusFirstEditorGroup" : "workbench.action.focusSecondEditorGroup");
  const workspace = page.getByTestId(`workspace-node-${owner.routeId}`).locator("..");
  await workspace.locator(`button[data-session-id="${owner.sessionId}"]`).dblclick();
  const thread = group.locator(`section.boxteam-thread[data-workspace-id="${owner.workspaceId}"][data-session-id="${owner.sessionId}"][data-thread-id="${owner.threadId}"]`);
  await expect(thread).toBeVisible();
  await expect(group).toHaveClass(/\bactive\b/);
  return thread;
}

export async function sessionTasks(page, owner, groupIndex) {
  const thread = await selectThread(page, owner, groupIndex);
  const toggle = thread.getByRole("button", { name: "切换会话资源", exact: true });
  if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
  await thread.getByRole("tab", { name: "任务", exact: true }).click();
  return thread.getByRole("region", { name: "当前会话关联任务", exact: true });
}

export async function automation(page) {
  await page.evaluate(() => globalThis.workbench.commands.executeCommand("boxteam.automation.focus"));
  const panel = page.locator('.native-workspace-panel-content:not([hidden])').getByRole("region", { name: "工作区持久任务", exact: true });
  await expect(panel).toBeVisible();
  return panel;
}
