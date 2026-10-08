import { writeFile } from "node:fs/promises";
import process from "node:process";
import { chromium } from "playwright";

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}
function check(value, message) {
  if (!value) throw new Error(message);
}
const baseUrl = required("BOXTEAM_BROWSER_BASE_URL");
const fixture = JSON.parse(required("BOXTEAM_BROWSER_FIXTURE"));
const resultPath = required("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = required("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true,
});
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const result = { classification: "Integration", provider_boundary: "OpenAI HTTP stub", route_stubs: [],
  requests: [], errors: [], cancel: null, joined: null, blocked: null, forced: null, stale: null, removed: null, empty_exit: null, deleted: null };
let phase = "initial";
page.on("pageerror", (error) => result.errors.push(error.message));
page.on("response", async (response) => {
  const request = response.request();
  if (request.method() === "POST" && /\/runtime\/(start|stop|restart-safe|restart-force)$/.test(new URL(response.url()).pathname)) {
    result.requests.push({ phase, path: new URL(response.url()).pathname, status: response.status(), body: request.postDataJSON() });
  }
});
async function api(path, { method = "GET", body, expected = 200 } = {}) {
  const output = await page.evaluate(async ({ path, method, body }) => {
    const credential = await fetch("/api/gateway/auth/local-credential");
    const token = (await credential.json()).data.token;
    const response = await fetch(path, {
      method, credentials: "include",
      headers: { "X-Local-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    return { status: response.status, payload: await response.json(), requestId: response.headers.get("X-Request-ID") };
  }, { path, method, body });
  check(output.status === expected, `真实 Gateway HTTP ${output.status}: ${JSON.stringify(output.payload)}`);
  check(output.requestId === output.payload.request_id, "Gateway 请求 ID 不一致");
  return output.payload.data;
}
const panel = page.locator('section[aria-label="Backend 运行时"]');
const dialog = page.getByRole("dialog");
const primaryArticle = () => panel.locator("article").filter({ has: page.locator("h2").filter({ hasText: fixture.primaryName }) }).first();
async function confirm(action) {
  await dialog.waitFor({ state: "visible" });
  const text = await dialog.innerText();
  check(text.includes(fixture.primaryId), "完整组确认缺少默认成员 ID");
  if (action !== "cancel-singleton") check(text.includes(fixture.secondaryId), "完整组确认缺少另一成员 ID");
  if (action === "force") check(text.includes("interrupted"), "强制确认缺少持久化中断说明");
  if (action.startsWith("cancel")) await dialog.getByRole("button", { name: "取消", exact: true }).click();
  else await dialog.getByRole("button", { name: action === "force" ? "强制重启" : "安全重启", exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
  return text;
}
async function lifecycleResponse(action, trigger, expected = 200) {
  const pending = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/gateway/workspaces/${fixture.primaryId}/runtime/${action}`,
  { timeout: 120_000 });
  await trigger();
  const response = await pending;
  const payload = await response.json();
  check(response.status() === expected, `生命周期请求 ${response.status()}: ${JSON.stringify(payload)}`);
  return payload.data ?? payload;
}
function groupRequest(snapshot) {
  return { expected_backend_generation: snapshot.backend_generation, expected_workspace_ids: snapshot.affected_workspace_ids };
}
try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.getByRole("button", { name: "工作台视图", exact: true }).waitFor({ state: "visible", timeout: 30_000 });
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Gateway 控制台/ }).click();
  await page.getByRole("button", { name: "服务运行时", exact: true }).click();
  await panel.waitFor({ state: "visible" });
  await primaryArticle().getByRole("button", { name: "安全重启", exact: true }).waitFor({ state: "visible" });

  phase = "cancel";
  const before = (await api("/api/gateway/workspaces")).items.find((item) => item.workspace_id === fixture.primaryId);
  await primaryArticle().getByRole("button", { name: "安全重启", exact: true }).click();
  const cancelText = await confirm("cancel-singleton");
  const afterCancel = (await api("/api/gateway/workspaces")).items.find((item) => item.workspace_id === fixture.primaryId);
  check(before.backend_generation === afterCancel.backend_generation, "取消确认改变了 Backend generation");
  check(result.requests.length === 0, "取消确认发送了生命周期 POST");
  result.cancel = { text: cancelText, generation: afterCancel.backend_generation, lifecycle_posts: 0 };

  phase = "desired-join";
  await primaryArticle().getByRole("button", { name: "调整成员", exact: true }).click();
  const defaultCheckbox = primaryArticle().getByRole("checkbox", { name: new RegExp(fixture.primaryName) });
  check(await defaultCheckbox.isDisabled(), "默认成员可被移出");
  await primaryArticle().getByRole("checkbox", { name: fixture.secondaryName, exact: true }).check();
  result.joined = await lifecycleResponse("restart-safe", async () => {
    await primaryArticle().getByRole("button", { name: "确认成员并安全重启", exact: true }).click();
    await confirm("safe");
  });
  check(result.joined.status === "restarted", "成员合并未成功");
  check(result.joined.affected_workspace_ids.length === 2, "合并没有返回完整 affected 集合");
  await primaryArticle().getByText("运行中 · 2 个工作区共用一个 Backend", { exact: true }).waitFor({ state: "visible", timeout: 30_000 });

  // 保持真实 Gateway SSE 引用；安全重启必须 blocked，force 在 durable 中断后撤销旧租约。
  await page.evaluate(async ({ workspaceId }) => {
    const token = (await (await fetch("/api/gateway/auth/local-credential")).json()).data.token;
    const controller = new AbortController();
    const response = await fetch("/api/v1/session-catalog/events/stream", {
      headers: { "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId }, signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(`SSE 连接失败 ${response.status}`);
    const state = { controller, chunks: [], closed: false };
    window.__managedBackendStream = state;
    state.reader = response.body.getReader();
    state.consume = (async () => {
      try {
        while (true) {
          const { done, value } = await state.reader.read();
          if (done) break;
          state.chunks.push(new TextDecoder().decode(value));
        }
      } finally { state.closed = true; }
    })();
  }, { workspaceId: fixture.secondaryId });
  phase = "blocked";
  const blockedGeneration = result.joined.backend_generation;
  result.blocked = await lifecycleResponse("restart-safe", async () => {
    await primaryArticle().getByRole("button", { name: "安全重启", exact: true }).click();
    await confirm("safe");
  });
  check(result.blocked.status === "blocked", "活动 SSE 未阻止安全重启");
  check(result.blocked.backend_generation === blockedGeneration, "blocked 改变了 generation");
  check(result.blocked.blockers.some((item) => item.workspace_id === fixture.secondaryId && item.kind === "proxy_stream"),
    "blocker 丢失另一成员的工作区 ID");
  await panel.getByRole("alert").filter({ hasText: fixture.secondaryId }).waitFor({ state: "visible" });

  phase = "force";
  result.forced = await lifecycleResponse("restart-force", async () => {
    await primaryArticle().getByRole("button", { name: "强制重启", exact: true }).click();
    result.force_confirmation = await confirm("force");
  });
  check(result.forced.status === "restarted" && result.forced.forced, "强制重启没有成功回执");
  check(result.forced.backend_generation !== blockedGeneration, "强制重启未切换 generation");
  result.old_stream = await page.evaluate(() => ({
    closed: window.__managedBackendStream.closed,
    chunks: window.__managedBackendStream.chunks,
  }));
  check(result.old_stream.closed && result.old_stream.chunks.join("").includes("gateway-route-invalidated"),
    "旧 SSE 租约未通过真实代理失效");
  await primaryArticle().getByText("运行中 · 2 个工作区共用一个 Backend", { exact: true }).waitFor({ state: "visible" });

  // 用户确认期间另一调用方提交新 generation：真实 CAS 必须拒绝旧选择，并刷新镜像。
  phase = "stale-confirmation";
  await primaryArticle().getByRole("button", { name: "调整成员", exact: true }).click();
  await primaryArticle().getByRole("checkbox", { name: fixture.secondaryName, exact: true }).uncheck();
  await primaryArticle().getByRole("button", { name: "确认成员并安全重启", exact: true }).click();
  await dialog.waitFor({ state: "visible" });
  const oldRequest = groupRequest(result.forced);
  phase = "external-generation-change";
  const advanced = await api(`/api/gateway/workspaces/${fixture.primaryId}/runtime/restart-force`, { method: "POST", body: oldRequest });
  check(advanced.status === "restarted" && advanced.backend_generation !== result.forced.backend_generation,
    "另一调用方未真正推进 generation");
  phase = "stale-confirmation";
  result.stale = await lifecycleResponse("restart-safe", () => confirm("safe"), 409);
  check(JSON.stringify(result.stale).includes("generation"), "陈旧合同错误没有标明 generation");
  await panel.getByRole("alert").waitFor({ state: "visible" });
  const fresh = (await api("/api/gateway/workspaces")).items.find((item) => item.workspace_id === fixture.primaryId);
  check(fresh.backend_generation === advanced.backend_generation && fresh.affected_workspace_ids.length === 2,
    "陈旧提交改变了已公布完整组");

  phase = "desired-remove";
  await primaryArticle().getByRole("button", { name: "调整成员", exact: true }).click();
  await primaryArticle().getByRole("checkbox", { name: fixture.secondaryName, exact: true }).uncheck();
  result.removed = await lifecycleResponse("restart-safe", async () => {
    await primaryArticle().getByRole("button", { name: "确认成员并安全重启", exact: true }).click();
    await confirm("safe");
  });
  check(result.removed.status === "restarted", "完整 desired 成员移出失败");
  const primary = result.removed.workspaces.items.find((item) => item.workspace_id === fixture.primaryId);
  const secondary = result.removed.workspaces.items.find((item) => item.workspace_id === fixture.secondaryId);
  check(primary.status === "ready" && primary.affected_workspace_ids.length === 1, "默认成员未保持就绪 singleton");
  check(secondary.status === "offline", "移出成员的辅助服务没有关闭");
  check(result.removed.affected_workspace_ids.includes(fixture.secondaryId), "移出成员没有纳入 affected 回执");
  // 非默认 singleton 的发起成员可以全部退出；页面消费 stopped 并保持注册项离线。
  phase = "empty-exit";
  await api(`/api/gateway/workspaces/${fixture.secondaryId}/runtime/start`, { method: "POST" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "工作台视图", exact: true }).waitFor({ state: "visible", timeout: 30_000 });
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Gateway 控制台/ }).click();
  await page.getByRole("button", { name: "服务运行时", exact: true }).click();
  const secondaryArticle = () => panel.locator("article").filter({ has: page.locator("h2").filter({ hasText: fixture.secondaryName }) }).first();
  await secondaryArticle().getByText("运行中 · 1 个工作区共用一个 Backend", { exact: true }).waitFor({ state: "visible", timeout: 30_000 });
  await secondaryArticle().getByRole("button", { name: "调整成员", exact: true }).click();
  const initiatingCheckbox = secondaryArticle().getByRole("checkbox", { name: fixture.secondaryName, exact: true });
  check(!(await initiatingCheckbox.isDisabled()), "非默认发起成员仍被禁止退出");
  await initiatingCheckbox.uncheck();
  const emptyPending = page.waitForResponse((response) => response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/gateway/workspaces/${fixture.secondaryId}/runtime/restart-safe`, { timeout: 120_000 });
  await secondaryArticle().getByRole("button", { name: "排空并退出全部成员", exact: true }).click();
  await dialog.waitFor({ state: "visible" });
  check((await dialog.innerText()).includes(fixture.secondaryId), "空目标确认缺少发起成员");
  await dialog.getByRole("button", { name: "关闭并退出", exact: true }).click();
  const emptyResponse = await emptyPending;
  check(emptyResponse.status() === 200, `空目标退出 HTTP ${emptyResponse.status()}`);
  result.empty_exit = (await emptyResponse.json()).data;
  check(result.empty_exit.status === "stopped", "空目标退出没有 stopped 回执");
  check((await api("/api/gateway/workspaces")).items.find((item) => item.workspace_id === fixture.secondaryId).backend_generation === null,
    "空目标退出仍保有 owner");
  await dialog.waitFor({ state: "hidden" });

  // 真实主窗口删除入口提交最后成员的完整合同，成功后从权威列表收敛。
  phase = "delete-last-member";
  await api(`/api/gateway/workspaces/${fixture.secondaryId}/runtime/start`, { method: "POST" });
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /会话工作台/ }).click();
  const row = page.getByTestId(`workspace-node-${fixture.secondaryId}`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await row.click({ button: "right" });
  await page.getByRole("menuitem", { name: "删除工作区", exact: true }).click();
  await dialog.waitFor({ state: "visible" });
  const deleteText = await dialog.innerText();
  check(deleteText.includes(fixture.secondaryId) && deleteText.includes("最后成员退出"), "删除确认未展示最后成员完整影响");
  const deletePending = page.waitForResponse((response) => response.request().method() === "DELETE"
    && new URL(response.url()).pathname === `/api/gateway/workspaces/${fixture.secondaryId}`, { timeout: 120_000 });
  await dialog.getByRole("button", { name: "删除", exact: true }).click();
  const deleteResponse = await deletePending;
  check(deleteResponse.status() === 200, `删除最后成员 HTTP ${deleteResponse.status()}: ${await deleteResponse.text()}`);
  const deleteBody = deleteResponse.request().postDataJSON();
  check(deleteBody.expected_workspace_ids.length === 1 && deleteBody.expected_workspace_ids[0] === fixture.secondaryId
    && deleteBody.desired_workspace_ids.length === 0, "Web DELETE 丢失完整 singleton/显式空目标");
  result.deleted = { workspaces: (await deleteResponse.json()).data, request: deleteBody, confirmation: deleteText };
  check(!result.deleted.workspaces.items.some((item) => item.workspace_id === fixture.secondaryId), "删除成功仍保留目标登记");
  await row.waitFor({ state: "hidden", timeout: 30_000 });
  check(result.errors.length === 0, `页面异常: ${result.errors.join("; ")}`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
} catch (error) {
  result.failure = { phase, message: error.message, page: await page.locator("body").innerText().catch(() => "") };
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => {});
  throw error;
} finally {
  await writeFile(resultPath, JSON.stringify(result, null, 2) + "\n");
  await context.close();
  await browser.close();
}
