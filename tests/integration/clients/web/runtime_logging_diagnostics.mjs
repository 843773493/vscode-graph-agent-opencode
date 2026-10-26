import { writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const baseUrl = process.env.BOXTEAM_BROWSER_BASE_URL;
const artifacts = process.env.BOXTEAM_BROWSER_ARTIFACTS;
if (!baseUrl || !artifacts) throw new Error("日志诊断验收缺少目标或产物目录");
const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH, headless: true });
const errors = [];
let executionError;
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  const initialization = await page.evaluate(async () => {
    const token = (await (await fetch("/api/gateway/auth/local-credential")).json()).data.token;
    const observed = [];
    for (const route of ["/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"]) {
      const response = await fetch(route, { headers: { "X-Local-Token": token } });
      const payload = await response.json();
      if (!response.ok || !payload.request_id || response.headers.get("X-Request-ID") !== payload.request_id) {
        throw new Error(`页面初始化失败 ${route}: ${JSON.stringify(payload)}`);
      }
      observed.push({ route, request_id: payload.request_id, data: payload.data });
    }
    return observed;
  });
  await page.getByRole("button", { name: "工作台视图", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /Gateway 控制台/ }).click();
  const pending = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/gateway/diagnostics");
  await page.getByRole("button", { name: "日志与诊断", exact: true }).click();
  const response = await pending;
  const diagnostics = (await response.json()).data;
  if (response.status() !== 200 || diagnostics.processes.length !== 5) throw new Error("真实五类进程诊断缺失");
  const panel = page.locator(".gateway-diagnostics-runtime");
  await panel.waitFor({ state: "visible" });
  const text = await panel.innerText();
  for (const process of diagnostics.processes) {
    if (process.error || !process.state || !text.includes(process.state.runtime_instance_id)
      || !text.includes(process.state.applied_revision) || !text.includes(`PID ${process.state.pid}`)) {
      throw new Error(`进程真实设置未展示: ${JSON.stringify(process)}`);
    }
  }
  if (!text.includes("stdout-only") || !text.includes("Owner：") || !text.includes("文件身份：")) {
    throw new Error("文件与 stdout-only 入口未区分");
  }
  await page.screenshot({ path: path.join(artifacts, "runtime-logging-diagnostics.png"), fullPage: true });
  await writeFile(path.join(artifacts, "runtime-logging-diagnostics-web.json"), JSON.stringify({ initialization, diagnostics, text, errors }, null, 2));
  if (errors.length) throw new Error(`页面错误：${errors.join("\n")}`);
} catch (error) {
  executionError = error;
  throw error;
} finally {
  try {
    await browser.close();
  } catch (cleanupError) {
    if (executionError) throw new AggregateError([executionError, cleanupError], "日志诊断验收与浏览器清理均失败");
    throw cleanupError;
  }
}
