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
const result = { classification: "Integration", provider_boundary: "本地 OpenAI HTTP 协议桩，无模型调用",
  route_stubs: [], page_errors: [], metadata: null, workspace_navigation_visible: false };
const browser = await chromium.launch({
  executablePath: required("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless: true,
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on("pageerror", (error) => result.page_errors.push(error.message));
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
  const workspace = page.getByTestId(`workspace-node-${fixture.workspaceId}`);
  await workspace.waitFor({ state: "visible", timeout: 30_000 });
  result.workspace_navigation_visible = true;
  // 用户可打开实际工作台视图菜单，证明元数据到达后页面完成 React 初始化。
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Gateway 控制台/ }).waitFor({ state: "visible" });
  await page.keyboard.press("Escape");
  await page.screenshot({ path: required("BOXTEAM_BROWSER_SCREENSHOT_PATH"), fullPage: true });
  check(result.page_errors.length === 0, `页面异常: ${result.page_errors.join("; ")}`);
} finally {
  await writeFile(required("BOXTEAM_BROWSER_RESULT_PATH"), JSON.stringify(result, null, 2) + "\n");
  await browser.close();
}
