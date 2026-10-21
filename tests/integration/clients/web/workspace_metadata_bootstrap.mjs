// Integration：实际产品页面通过 Gateway 初始化；没有请求拦截或元数据替身。
import { writeFile } from "node:fs/promises";
import { chromium } from "playwright";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
function check(value, message) {
  if (!value) throw new Error(message);
}
const fixture = JSON.parse(required("BOXTEAM_BROWSER_FIXTURE"));
const result = { classification: "Integration", provider_boundary: "model_stream 精确回放，无模型调用",
  route_stubs: [], page_errors: [], network: [], metadata: null, workspace_navigation_visible: false };
const browser = await chromium.launch({
  executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true,
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on("pageerror", (error) => result.page_errors.push(error.message));
page.on("requestfailed", (request) => result.network.push({
  url: request.url(), method: request.method(), failure: request.failure(), timing: request.timing(),
}));
page.on("response", (response) => {
  if (new URL(response.url()).pathname.startsWith("/api/")) result.network.push({
    url: response.url(), status: response.status(), timing: response.request().timing(),
  });
});
try {
  const metadataResponse = page.waitForResponse((response) => response.request().method() === "GET"
    && new URL(response.url()).pathname === "/api/v1/workspace", { timeout: 30_000 });
  await page.goto(required("BOXTEAM_BROWSER_BASE_URL"), { waitUntil: "domcontentloaded", timeout: 30_000 });
  const response = await metadataResponse;
  const payload = await response.json();
  check(response.status() === 200, `产品初始化元数据失败 ${response.status()}: ${JSON.stringify(payload)}`);
  check(response.headers()["x-request-id"] === payload.request_id, "产品初始化元数据 request_id 不一致");
  check(payload.data.workspace_id === fixture.backendWorkspaceId, "页面初始化取得了其他工作区的元数据");
  check(!Object.hasOwn(payload.data, "root_path") && !Object.hasOwn(payload.data.git, "root"), "产品元数据仍包含重复路径");
  result.metadata = payload;
  await page.getByRole("button", { name: "工作台视图", exact: true }).waitFor({ state: "visible", timeout: 30_000 });
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /会话工作台/ }).click();
  const workspace = page.getByTestId(`workspace-node-${fixture.workspaceId}`);
  await workspace.waitFor({ state: "visible", timeout: 30_000 });
  result.workspace_navigation_visible = true;
  const native = fixture.nativeDirectory;
  check(native, "缺少未注册目录验收输入");
  const openNativeFile = async () => {
    await page.getByRole("button", { name: "工作台视图", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /会话工作台/ }).click();
    const workspaceRow = page.getByTestId(`workspace-node-${fixture.workspaceId}`);
    const expand = workspaceRow.getByRole("button", { name: /^展开工作区 / });
    if (await expand.count()) await expand.click();
    await page.locator(`button[data-session-id="${native.sessionId}"]`).click();
    await page.getByRole("tab", { name: "文件", exact: true }).click();
    const tree = page.getByRole("tree", { name: "工作区文件树", exact: true });
    const shortcut = tree.locator("button.files-tree-quick-main").filter({ hasText: "未注册目录" });
    await shortcut.waitFor({ state: "visible", timeout: 30_000 });
    check(await shortcut.getAttribute("title") === native.rootUri, "快捷目录未保存URI");
    if (await shortcut.getAttribute("aria-expanded") !== "true") await shortcut.click();
    await tree.getByText(native.fileName, { exact: true }).click();
    const editor = page.locator("textarea.workspace-preview-text-editor");
    await editor.waitFor({ state: "visible", timeout: 30_000 });
    return editor;
  };
  let editor = await openNativeFile();
  check(await editor.inputValue() === native.content, "未注册目录正文读取错误");
  await editor.click();
  await editor.fill(native.edited);
  const saved = page.waitForResponse((response) => response.request().method() === "PUT"
    && new URL(response.url()).pathname === "/api/v1/workspace/files/content");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  const savedResponse = await saved;
  check(savedResponse.status() === 200, `原生目录编辑失败: ${await savedResponse.text()}`);
  const savedBody = await savedResponse.json();
  check(savedBody.data.path.startsWith(native.rootUri + "/"), "编辑响应丢失原生目录URI");
  await page.reload({ waitUntil: "domcontentloaded" });
  editor = await openNativeFile();
  check(await editor.inputValue() === native.edited, "刷新后未从目标目录重开已编辑文件");
  result.native_directory_reopened = true;
  check(result.page_errors.length === 0, `页面异常: ${result.page_errors.join("; ")}`);
} finally {
  result.page_text = await page.locator("body").innerText();
  await page.screenshot({ path: required("BOXTEAM_BROWSER_SCREENSHOT_PATH"), fullPage: true });
  await writeFile(required("BOXTEAM_BROWSER_RESULT_PATH"), JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
