import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const baseUrl = requiredEnvironment("BOXTEAM_E2E_BASE_URL");
const fixture = JSON.parse(requiredEnvironment("BOXTEAM_E2E_FIXTURE"));
const resultPath = requiredEnvironment("BOXTEAM_E2E_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseUrl });
const page = await context.newPage();

const parentRow = page.locator(`[data-testid$="-${fixture.parentSessionId}"]`);
const childRow = page.locator(`[data-testid$="-${fixture.childSessionId}"]`);
const folderRow = page.locator(`[data-testid$="-${fixture.folderId}"]`);
let forkSessionId = null;
let nestedWorkspaceFolders = false;
let childWorkspaceMoved = false;
let workspaceFolderReturnedToRoot = false;
let workspaceExpansionPreserved = false;
let phantomSessionEmptyStatePresent = true;
let workspaceRootUsesContextMenu = false;
let workspaceFolderRecursiveDelete = false;
let warmDialogBackground = "";
let workspaceMenuCreatedSessionFolder = false;

async function expandRow(row) {
  const chevron = row.locator(".session-resource-chevron");
  await chevron.waitFor({ state: "visible", timeout: 10_000 });
  const label = await chevron.getAttribute("aria-label");
  if (label?.startsWith("展开")) await chevron.click();
}

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  await childRow.waitFor({ state: "visible", timeout: 15_000 });
  await folderRow.waitFor({ state: "visible", timeout: 15_000 });
  const parentWorkspaceRow = page.locator(
    `[data-testid="workspace-node-${fixture.parentWorkspaceId}"]`,
  );
  const childWorkspaceRow = page.locator(
    `[data-testid="workspace-node-${fixture.childWorkspaceId}"]`,
  );
  await parentWorkspaceRow.waitFor({ state: "visible" });
  await childWorkspaceRow.waitFor({ state: "visible" });
  const parentWorkspaceChevron = parentWorkspaceRow.locator(".session-resource-chevron");
  if ((await parentWorkspaceChevron.getAttribute("aria-label"))?.startsWith("折叠")) {
    await parentWorkspaceChevron.click();
  }
  await parentRow.waitFor({ state: "hidden" });
  const navigationRootDropTarget = page.locator(
    '[data-testid="workspace-navigation-root-drop-target"]',
  );
  workspaceRootUsesContextMenu = await navigationRootDropTarget.evaluate(
    (node) => node.tagName !== "BUTTON" && node.getAttribute("role") === "heading",
  );

  async function createWorkspaceFolder(name) {
    await navigationRootDropTarget.click({ button: "right" });
    await page.getByRole("menuitem", { name: "新建工作区文件夹", exact: true }).click();
    const input = page.getByRole("textbox", { name: "新工作区文件夹名称", exact: true });
    await input.fill(name);
    const responsePromise = page.waitForResponse(
      (response) => response.request().method() === "POST"
        && response.url().includes("/workspace-navigation/folders"),
    );
    await input.press("Enter");
    const response = await responsePromise;
    if (!response.ok()) throw new Error(`创建工作区文件夹失败: ${await response.text()}`);
    return (await response.json()).data.nodes.find(
      (node) => node.kind === "workspace_folder" && node.name === name,
    );
  }

  const workspaceFolderOne = await createWorkspaceFolder("虚拟一级");
  const workspaceFolderTwo = await createWorkspaceFolder("虚拟二级");
  if (!workspaceFolderOne || !workspaceFolderTwo) throw new Error("创建响应缺少工作区文件夹节点");
  const workspaceFolderOneRow = page.locator(
    `[data-testid="workspace-folder-node-${workspaceFolderOne.node_id}"]`,
  );
  const workspaceFolderTwoRow = page.locator(
    `[data-testid="workspace-folder-node-${workspaceFolderTwo.node_id}"]`,
  );
  await workspaceFolderOneRow.waitFor({ state: "visible" });
  await workspaceFolderTwoRow.waitFor({ state: "visible" });
  const nestWorkspaceFolderResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PUT"
      && response.url().includes("/workspace-navigation/placement"),
  );
  await workspaceFolderTwoRow.dragTo(workspaceFolderOneRow);
  const nestWorkspaceFolderResponse = await nestWorkspaceFolderResponsePromise;
  if (!nestWorkspaceFolderResponse.ok()) {
    throw new Error(`嵌套工作区文件夹失败: ${await nestWorkspaceFolderResponse.text()}`);
  }
  await expandRow(workspaceFolderOneRow);
  await workspaceFolderTwoRow.waitFor({ state: "visible" });
  const workspaceFolderOnePadding = Number.parseFloat(
    await workspaceFolderOneRow.evaluate((node) => getComputedStyle(node).paddingLeft),
  );
  const workspaceFolderTwoPadding = Number.parseFloat(
    await workspaceFolderTwoRow.evaluate((node) => getComputedStyle(node).paddingLeft),
  );
  nestedWorkspaceFolders = workspaceFolderTwoPadding > workspaceFolderOnePadding;

  const returnWorkspaceFolderToRootResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PUT"
      && response.url().includes("/workspace-navigation/placement"),
  );
  await workspaceFolderTwoRow.dragTo(navigationRootDropTarget);
  const returnWorkspaceFolderToRootResponse = await returnWorkspaceFolderToRootResponsePromise;
  if (!returnWorkspaceFolderToRootResponse.ok()) {
    throw new Error(`工作区文件夹移回根级失败: ${await returnWorkspaceFolderToRootResponse.text()}`);
  }
  await workspaceFolderTwoRow.waitFor({ state: "visible" });
  const returnedWorkspaceFolderPadding = Number.parseFloat(
    await workspaceFolderTwoRow.evaluate((node) => getComputedStyle(node).paddingLeft),
  );
  workspaceFolderReturnedToRoot = returnedWorkspaceFolderPadding === workspaceFolderOnePadding;

  await workspaceFolderOneRow.click({ button: "right" });
  for (const label of ["新建子文件夹", "重命名", "复制文件夹 ID", "删除文件夹"]) {
    await page.getByRole("menuitem", { name: label, exact: true }).waitFor({ state: "visible" });
  }
  await page.getByRole("menuitem", { name: "重命名", exact: true }).click();
  const renameInput = page.getByRole("textbox", { name: "工作区文件夹新名称", exact: true });
  await renameInput.fill("虚拟一级已重命名");
  const renameResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/workspace-navigation/nodes/${workspaceFolderOne.node_id}`),
  );
  await renameInput.press("Enter");
  const renameResponse = await renameResponsePromise;
  if (!renameResponse.ok()) throw new Error(`树内重命名失败: ${await renameResponse.text()}`);
  workspaceExpansionPreserved = await parentRow.isHidden()
    && (await parentWorkspaceChevron.getAttribute("aria-label"))?.startsWith("展开") === true;
  if (!workspaceExpansionPreserved) {
    throw new Error("工作区导航变更后，用户折叠的当前工作区被自动展开");
  }
  await expandRow(parentWorkspaceRow);
  await parentRow.waitFor({ state: "visible" });
  await childRow.waitFor({ state: "visible" });
  await folderRow.waitFor({ state: "visible" });
  await parentWorkspaceRow.click({ button: "right" });
  for (const label of ["新建会话", "新建会话文件夹"]) {
    await page.getByRole("menuitem", { name: label, exact: true }).waitFor({ state: "visible" });
  }
  await page.getByRole("menuitem", { name: "新建会话文件夹", exact: true }).click();
  const workspaceSessionFolderDialog = page.getByRole("dialog", { name: "新建会话文件夹" });
  await workspaceSessionFolderDialog.getByRole("textbox", { name: "文件夹名称" }).fill("菜单工作区文件夹");
  const workspaceSessionFolderResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST"
      && response.url().includes("/session-catalog/folders"),
  );
  await workspaceSessionFolderDialog.getByRole("button", { name: "创建", exact: true }).click();
  const workspaceSessionFolderResponse = await workspaceSessionFolderResponsePromise;
  if (!workspaceSessionFolderResponse.ok()) {
    throw new Error(`工作区右键菜单创建会话文件夹失败: ${await workspaceSessionFolderResponse.text()}`);
  }
  await page.getByText("菜单工作区文件夹", { exact: true }).waitFor({ state: "visible" });
  workspaceMenuCreatedSessionFolder = true;
  const bindWorkspaceResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/workspaces/${fixture.childWorkspaceId}`),
  );
  await childWorkspaceRow.dragTo(parentWorkspaceRow);
  const bindWorkspaceResponse = await bindWorkspaceResponsePromise;
  if (!bindWorkspaceResponse.ok()) {
    throw new Error(`设置子工作区失败: ${await bindWorkspaceResponse.text()}`);
  }
  await expandRow(parentWorkspaceRow);
  await childWorkspaceRow.waitFor({ state: "visible" });
  const parentWorkspacePadding = Number.parseFloat(
    await parentWorkspaceRow.evaluate((node) => getComputedStyle(node).paddingLeft),
  );
  const childWorkspacePadding = Number.parseFloat(
    await childWorkspaceRow.evaluate((node) => getComputedStyle(node).paddingLeft),
  );
  if (!(childWorkspacePadding > parentWorkspacePadding)) {
    throw new Error("子工作区没有显示在父工作区下方");
  }
  const unbindWorkspaceResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/workspaces/${fixture.childWorkspaceId}`),
  );
  await childWorkspaceRow.dragTo(workspaceFolderOneRow);
  const unbindWorkspaceResponse = await unbindWorkspaceResponsePromise;
  if (!unbindWorkspaceResponse.ok()) {
    throw new Error(`工作区移入虚拟文件夹失败: ${await unbindWorkspaceResponse.text()}`);
  }
  await expandRow(workspaceFolderOneRow);
  await childWorkspaceRow.waitFor({ state: "visible" });
  childWorkspaceMoved = true;

  await workspaceFolderOneRow.click({ button: "right" });
  await page.getByRole("menuitem", { name: "删除文件夹", exact: true }).click();
  const workspaceDeleteDialog = page.getByRole("dialog", { name: "删除工作区文件夹" });
  await workspaceDeleteDialog.waitFor({ state: "visible" });
  warmDialogBackground = await workspaceDeleteDialog.evaluate(
    (node) => getComputedStyle(node).backgroundColor,
  );
  const recursiveWorkspaceDeleteResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "DELETE"
      && response.url().includes(`/workspace-navigation/folders/${workspaceFolderOne.node_id}?recursive=true`),
  );
  await workspaceDeleteDialog.getByRole("button", { name: "删除", exact: true }).click();
  const recursiveWorkspaceDeleteResponse = await recursiveWorkspaceDeleteResponsePromise;
  if (!recursiveWorkspaceDeleteResponse.ok()) {
    throw new Error(`递归删除工作区文件夹失败: ${await recursiveWorkspaceDeleteResponse.text()}`);
  }
  await workspaceFolderOneRow.waitFor({ state: "detached" });
  await childWorkspaceRow.waitFor({ state: "visible" });
  workspaceFolderRecursiveDelete = true;

  const sessionRowActionCount = await childRow.locator(".session-resource-actions").count();
  const folderRowActionCount = await folderRow.locator(".session-resource-actions").count();

  await parentRow.click({ button: "right" });
  const bloodlineTextPresent = await page.getByText(/血缘/).count() > 0;
  await page.getByRole("menuitem", { name: "新建子文件夹", exact: true }).waitFor({ state: "visible" });
  const bindResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/session-catalog/nodes/${fixture.childSessionId}/parent`),
  );
  await page.keyboard.press("Escape");
  await childRow.dragTo(parentRow);
  const bindResponse = await bindResponsePromise;
  if (!bindResponse.ok()) throw new Error(`绑定子会话失败: ${await bindResponse.text()}`);

  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  await expandRow(parentRow);
  await childRow.waitFor({ state: "visible", timeout: 10_000 });
  const parentPadding = Number.parseFloat(await parentRow.evaluate((node) => getComputedStyle(node).paddingLeft));
  const childPadding = Number.parseFloat(await childRow.evaluate((node) => getComputedStyle(node).paddingLeft));
  if (!(childPadding > parentPadding)) throw new Error("绑定后子会话没有显示在父会话下方");

  await folderRow.click({ button: "right" });
  for (const label of [
    "新建会话",
    "新建子文件夹",
    "复制文件夹 ID",
    "将剪贴板会话移动到此处",
    "移动到剪贴板文件夹",
    "重命名",
    "删除文件夹及内容",
  ]) {
    await page.getByRole("menuitem", { name: label, exact: true }).waitFor({ state: "visible" });
  }
  await page.getByRole("menuitem", { name: "重命名", exact: true }).click();
  const folderRenameDialog = page.getByRole("dialog", { name: "重命名会话文件夹" });
  const folderRenameInput = folderRenameDialog.getByRole("textbox", { name: "文件夹名称" });
  await folderRenameInput.fill("归档文件夹已重命名");
  const folderRenameResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/session-catalog/folders/${fixture.folderId}`),
  );
  await folderRenameDialog.getByRole("button", { name: "保存", exact: true }).click();
  const folderRenameResponse = await folderRenameResponsePromise;
  if (!folderRenameResponse.ok()) {
    throw new Error(`暖色窗口重命名会话文件夹失败: ${await folderRenameResponse.text()}`);
  }
  const moveResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/session-catalog/nodes/${fixture.childSessionId}/parent`),
  );
  await childRow.dragTo(folderRow);
  const moveResponse = await moveResponsePromise;
  if (!moveResponse.ok()) throw new Error(`拖动会话到文件夹失败: ${await moveResponse.text()}`);
  await parentRow.locator(".session-resource-chevron").waitFor({ state: "detached" });
  phantomSessionEmptyStatePresent = await parentRow
    .locator("xpath=..")
    .getByText("空文件夹", { exact: true })
    .count() > 0;
  if (phantomSessionEmptyStatePresent) {
    throw new Error("叶子会话下错误显示了“空文件夹”");
  }

  const moveFolderUnderSessionPromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/session-catalog/nodes/${fixture.folderId}/parent`),
  );
  await folderRow.dragTo(parentRow);
  const moveFolderUnderSession = await moveFolderUnderSessionPromise;
  if (!moveFolderUnderSession.ok()) {
    throw new Error(`拖动文件夹到会话失败: ${await moveFolderUnderSession.text()}`);
  }
  await expandRow(parentRow);
  await folderRow.waitFor({ state: "visible" });

  const workspaceRow = page.locator('[data-testid^="workspace-node-"]').first();
  const moveFolderToRootPromise = page.waitForResponse(
    (response) => response.request().method() === "PATCH"
      && response.url().includes(`/session-catalog/nodes/${fixture.folderId}/parent`),
  );
  await folderRow.dragTo(workspaceRow);
  const moveFolderToRoot = await moveFolderToRootPromise;
  if (!moveFolderToRoot.ok()) {
    throw new Error(`拖动文件夹到工作区根失败: ${await moveFolderToRoot.text()}`);
  }

  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  await parentRow.click({ button: "right" });
  const forkResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST"
      && response.url().includes(`/sessions/${fixture.parentSessionId}/fork-context`),
  );
  await page.getByRole("menuitem", { name: "从上下文创建子会话", exact: true }).click();
  const forkResponse = await forkResponsePromise;
  if (!forkResponse.ok()) throw new Error(`创建上下文子会话失败: ${await forkResponse.text()}`);
  forkSessionId = (await forkResponse.json()).data.session_id;
  const forkRow = page.locator(`[data-testid$="-${forkSessionId}"]`);
  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  await expandRow(parentRow);
  await forkRow.waitFor({ state: "visible", timeout: 10_000 });

  await parentRow.click({ button: "right" });
  const deleteResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "DELETE"
      && response.url().includes(`/sessions/${fixture.parentSessionId}?cascade=true`),
  );
  await page.getByRole("menuitem", { name: "删除会话", exact: true }).click();
  const deleteSessionDialog = page.getByRole("dialog", { name: "永久删除会话" });
  await deleteSessionDialog.getByRole("button", { name: "删除", exact: true }).click();
  const deleteResponse = await deleteResponsePromise;
  if (!deleteResponse.ok()) throw new Error(`级联删除父会话失败: ${await deleteResponse.text()}`);

  await writeFile(resultPath, `${JSON.stringify({
    bloodlineTextPresent,
    sessionRowActionCount,
    folderRowActionCount,
    forkSessionId,
    nestedWorkspaceFolders,
    childWorkspaceMoved,
    workspaceFolderReturnedToRoot,
    workspaceExpansionPreserved,
    phantomSessionEmptyStatePresent,
    workspaceRootUsesContextMenu,
    workspaceFolderRecursiveDelete,
    warmDialogBackground,
    workspaceMenuCreatedSessionFolder,
  }, null, 2)}\n`, "utf8");
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => undefined);
  throw error;
} finally {
  await browser.close();
}
