import assert from "node:assert/strict";
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
let forkThreadId = null;
let forkThreadUri = null;
let nestedWorkspaceFolders = false;
let childWorkspaceMoved = false;
let workspaceFolderReturnedToRoot = false;
let workspaceExpansionPreserved = false;
let phantomSessionEmptyStatePresent = true;
let workspaceRootUsesContextMenu = false;
let workspaceFolderRecursiveDelete = false;
let warmDialogBackground = "";
let workspaceMenuCreatedSessionFolder = false;
let sessionMainActionOpenedCatalogSession = false;
let folderMainActionToggledExpansion = false;

async function expandRow(row) {
  const chevron = row.locator(".session-resource-chevron");
  await chevron.waitFor({ state: "visible", timeout: 10_000 });
  const label = await chevron.getAttribute("aria-label");
  if (label?.startsWith("展开")) await chevron.click();
}

function matchingCatalogIntent(request, expected) {
  if (request.method() !== "POST"
    || !request.url().endsWith("/session-catalog/operations:enqueue")
    || request.headers()["x-boxteam-workspace-id"] !== fixture.parentWorkspaceId) {
    return undefined;
  }
  return request.postDataJSON().intents.find(
    (intent) => Object.entries(expected).every(([key, value]) => intent[key] === value),
  );
}

function waitForCatalogAcceptance(expected) {
  return page.waitForResponse(
    (response) => Boolean(matchingCatalogIntent(response.request(), expected)),
  );
}

async function requireCatalogCommit(response, expected) {
  if (response.status() !== 202) {
    throw new Error(`目录操作未被接受: ${await response.text()}`);
  }
  const request = response.request();
  const intent = matchingCatalogIntent(request, expected);
  if (!intent) throw new Error("目录响应与目标 intent 不匹配");
  const operationId = intent.client_operation_id;
  const accepted = await response.json();
  if (!accepted.data.receipts.some((receipt) => receipt.operation_id === operationId)) {
    throw new Error(`目录 acceptance 缺少 operation ${operationId}`);
  }
  const statusUrl = new URL(response.url());
  statusUrl.pathname = statusUrl.pathname.replace(/operations:enqueue$/, "operations");
  statusUrl.search = new URLSearchParams({ operation_id: operationId }).toString();
  const headers = Object.fromEntries(Object.entries(request.headers()).filter(
    ([name]) => ["x-local-token", "x-boxteam-workspace-id", "x-boxteam-backend-workspace-id"].includes(name),
  ));
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const status = await context.request.get(statusUrl.toString(), { headers, timeout: 5_000 });
    if (!status.ok()) throw new Error(`目录终态查询失败: ${await status.text()}`);
    const payload = await status.json();
    const receipt = payload.data.items.find((item) => item.operation_id === operationId);
    if (receipt?.state === "committed") return receipt;
    if (["rejected", "cancelled", "dependency_failed"].includes(receipt?.state)) {
      throw new Error(`目录操作未提交 ${operationId}: ${JSON.stringify(receipt)}`);
    }
    await page.waitForTimeout(50);
  }
  throw new Error(`目录操作未到终态: ${operationId}`);
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
  const workspaceSessionFolderIntent = { kind: "create_folder", name: "菜单工作区文件夹", parent_node_id: null };
  const workspaceSessionFolderResponsePromise = waitForCatalogAcceptance(workspaceSessionFolderIntent);
  await workspaceSessionFolderDialog.getByRole("button", { name: "创建", exact: true }).click();
  const workspaceSessionFolderResponse = await workspaceSessionFolderResponsePromise;
  await requireCatalogCommit(workspaceSessionFolderResponse, workspaceSessionFolderIntent);
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

  await parentRow.click({ button: "right" });
  const bloodlineTextPresent = await page.getByText(/血缘/).count() > 0;
  await page.getByRole("menuitem", { name: "新建子文件夹", exact: true }).waitFor({ state: "visible" });
  await page.keyboard.press("Escape");

  const parentMainAction = parentRow.locator("button.session-resource-main-action");
  const childMainAction = childRow.locator("button.session-resource-main-action");
  assert.equal(await parentMainAction.count(), 1);
  assert.equal(await parentMainAction.isVisible(), true);
  assert.equal(await parentMainAction.isEnabled(), true);
  assert.equal(await parentMainAction.getAttribute("data-session-id"), fixture.parentSessionId);
  assert.equal(await childMainAction.count(), 1);
  assert.equal(await childMainAction.isVisible(), true);
  assert.equal(await childMainAction.isEnabled(), true);
  assert.equal(await childMainAction.getAttribute("data-session-id"), fixture.childSessionId);
  await parentMainAction.click();
  await page.locator(
    `[data-testid$="-${fixture.parentSessionId}"][aria-current="true"]`,
  ).waitFor({ state: "visible", timeout: 10_000 });
  assert.equal(await childRow.getAttribute("aria-current"), null);
  await childMainAction.click();
  await page.locator(
    `[data-testid$="-${fixture.childSessionId}"][aria-current="true"]`,
  ).waitFor({ state: "visible", timeout: 10_000 });
  assert.equal(await parentRow.getAttribute("aria-current"), null);
  sessionMainActionOpenedCatalogSession = true;

  const folderMainAction = folderRow.locator("button.session-resource-main-action");
  const folderTreeItem = folderRow.locator("xpath=..");
  assert.equal(await folderMainAction.count(), 1);
  assert.equal(await folderMainAction.isVisible(), true);
  assert.equal(await folderMainAction.isEnabled(), true);
  assert.equal(await folderMainAction.getAttribute("data-session-id"), null);
  assert.equal(await folderTreeItem.getAttribute("aria-expanded"), "false");
  const folderTreeItemHandle = await folderTreeItem.elementHandle();
  assert.ok(folderTreeItemHandle);
  await folderMainAction.click();
  await page.waitForFunction(
    (treeItem) => treeItem.getAttribute("aria-expanded") === "true",
    folderTreeItemHandle,
  );
  await folderMainAction.click();
  await page.waitForFunction(
    (treeItem) => treeItem.getAttribute("aria-expanded") === "false",
    folderTreeItemHandle,
  );
  folderMainActionToggledExpansion = true;

  const bindIntent = { kind: "move_node", target_node_id: fixture.childSessionId, parent_node_id: fixture.parentSessionId };
  const bindResponsePromise = waitForCatalogAcceptance(bindIntent);
  await childRow.dragTo(parentRow);
  const bindResponse = await bindResponsePromise;
  await requireCatalogCommit(bindResponse, bindIntent);

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
  const folderRenameIntent = { kind: "rename_node", target_node_id: fixture.folderId, name: "归档文件夹已重命名" };
  const folderRenameResponsePromise = waitForCatalogAcceptance(folderRenameIntent);
  await folderRenameDialog.getByRole("button", { name: "保存", exact: true }).click();
  const folderRenameResponse = await folderRenameResponsePromise;
  await requireCatalogCommit(folderRenameResponse, folderRenameIntent);
  const moveIntent = { kind: "move_node", target_node_id: fixture.childSessionId, parent_node_id: fixture.folderId };
  const moveResponsePromise = waitForCatalogAcceptance(moveIntent);
  await childRow.dragTo(folderRow);
  const moveResponse = await moveResponsePromise;
  await requireCatalogCommit(moveResponse, moveIntent);
  await parentRow.locator(".session-resource-chevron").waitFor({ state: "detached" });
  phantomSessionEmptyStatePresent = await parentRow
    .locator("xpath=..")
    .getByText("空文件夹", { exact: true })
    .count() > 0;
  if (phantomSessionEmptyStatePresent) {
    throw new Error("叶子会话下错误显示了“空文件夹”");
  }

  const moveFolderUnderSessionIntent = { kind: "move_node", target_node_id: fixture.folderId, parent_node_id: fixture.parentSessionId };
  const moveFolderUnderSessionPromise = waitForCatalogAcceptance(moveFolderUnderSessionIntent);
  await folderRow.dragTo(parentRow);
  const moveFolderUnderSession = await moveFolderUnderSessionPromise;
  await requireCatalogCommit(moveFolderUnderSession, moveFolderUnderSessionIntent);
  await expandRow(parentRow);
  await folderRow.waitFor({ state: "visible" });

  const workspaceRow = page.locator('[data-testid^="workspace-node-"]').first();
  const moveFolderToRootIntent = { kind: "move_node", target_node_id: fixture.folderId, parent_node_id: null };
  const moveFolderToRootPromise = waitForCatalogAcceptance(moveFolderToRootIntent);
  await folderRow.dragTo(workspaceRow);
  const moveFolderToRoot = await moveFolderToRootPromise;
  await requireCatalogCommit(moveFolderToRoot, moveFolderToRootIntent);

  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  await parentRow.click();
  await page.locator(`[data-testid$="-${fixture.parentSessionId}"][aria-current="true"]`).waitFor({ state: "visible" });
  await parentRow.click({ button: "right" });
  const pointResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "GET"
      && new URL(response.url()).pathname === `/api/v1/sessions/${fixture.parentSessionId}/fork-source-point`,
  );
  await page.getByRole("menuitem", { name: "从上下文创建子会话", exact: true }).click();
  const pointResponse = await pointResponsePromise;
  assert.equal(pointResponse.status(), 200, await pointResponse.text());
  const point = (await pointResponse.json()).data;
  const forkDialog = page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await forkDialog.waitFor({ state: "visible" });
  assert.equal(await forkDialog.getByLabel("创建位置", { exact: true }).inputValue(), "new_child_thread");
  assert.equal(await forkDialog.getByLabel("目标容器", { exact: true }).inputValue(), point.child_container_uri);
  const fullPointPromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === `/api/v1/sessions/${fixture.parentSessionId}/fork-source-point`
      && url.searchParams.get("mode") === "full_rollout_copy";
  });
  await forkDialog.getByLabel("复制范围", { exact: true }).selectOption("full_rollout_copy");
  const fullPointResponse = await fullPointPromise;
  assert.equal(fullPointResponse.status(), 200, await fullPointResponse.text());
  const fullPoint = (await fullPointResponse.json()).data;
  assert.equal(fullPoint.anchor_item_id, null);
  assert.deepEqual([fullPoint.source_uri, fullPoint.source_view_id, fullPoint.source_view_revision],
    [point.source_uri, point.source_view_id, point.source_view_revision]);
  const prefixPointPromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET"
      && url.pathname === `/api/v1/sessions/${fixture.parentSessionId}/fork-source-point`
      && url.searchParams.get("mode") === "view_prefix_fork";
  });
  await forkDialog.getByLabel("复制范围", { exact: true }).selectOption("view_prefix_fork");
  const prefixPointResponse = await prefixPointPromise;
  assert.equal(prefixPointResponse.status(), 200, await prefixPointResponse.text());
  const prefixPoint = (await prefixPointResponse.json()).data;
  assert.deepEqual([prefixPoint.source_uri, prefixPoint.source_view_id, prefixPoint.source_view_revision, prefixPoint.anchor_item_id],
    [point.source_uri, point.source_view_id, point.source_view_revision, point.anchor_item_id]);
  await forkDialog.getByLabel("目标名称", { exact: true }).fill("目录入口的上下文分支");
  const forkResponsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/v1/sessions/${fixture.parentSessionId}/fork-context`,
  );
  await forkDialog.getByRole("button", { name: "创建分支", exact: true }).click();
  const forkResponse = await forkResponsePromise;
  if (!forkResponse.ok()) throw new Error(`创建上下文子会话失败: ${await forkResponse.text()}`);
  const forkRequest = forkResponse.request().postDataJSON();
  assert.deepEqual([forkRequest.source_uri, forkRequest.source_view_id, forkRequest.source_view_revision, forkRequest.anchor_item_id],
    [point.source_uri, point.source_view_id, point.source_view_revision, point.anchor_item_id]);
  assert.deepEqual([forkRequest.creation_kind, forkRequest.target_container_uri, forkRequest.resources],
    ["new_child_thread", point.child_container_uri, []]);
  const fork = (await forkResponse.json()).data;
  assert.equal(fork.session.session_id, fixture.parentSessionId);
  assert.equal(fork.creation_kind, "new_child_thread");
  assert.notEqual(fork.thread_id, fork.session.thread_id);
  assert.notEqual(fork.thread_uri, point.child_container_uri);
  forkThreadId = fork.thread_id;
  forkThreadUri = fork.thread_uri;
  await page.locator(`.child-thread-conversation[data-thread-id="${forkThreadId}"]`).waitFor({ state: "visible", timeout: 10_000 });
  await parentRow.waitFor({ state: "visible", timeout: 15_000 });
  assert.equal(await parentRow.getAttribute("aria-current"), "true");

  await parentRow.click({ button: "right" });
  const deleteIntent = { kind: "delete_session", target_node_id: fixture.parentSessionId };
  const deleteResponsePromise = waitForCatalogAcceptance(deleteIntent);
  await page.getByRole("menuitem", { name: "删除会话", exact: true }).click();
  const deleteSessionDialog = page.getByRole("dialog", { name: "永久删除会话" });
  await deleteSessionDialog.getByRole("button", { name: "删除", exact: true }).click();
  const deleteResponse = await deleteResponsePromise;
  await requireCatalogCommit(deleteResponse, deleteIntent);

  await writeFile(resultPath, `${JSON.stringify({
    bloodlineTextPresent,
    sessionMainActionOpenedCatalogSession,
    folderMainActionToggledExpansion,
    forkThreadId,
    forkThreadUri,
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
