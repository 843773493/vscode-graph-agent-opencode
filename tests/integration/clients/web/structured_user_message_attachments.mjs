import { writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

import { TestRunContext } from "../../../harness/js/run-context.mjs";

const testFile = fileURLToPath(import.meta.url);
const runContext = await TestRunContext.fromTestFile(
  testFile,
  process.env.BOXTEAM_PROJECT_ROOT ?? process.cwd(),
).prepare();
const projectRoot = runContext.projectRoot;
const resultPath = join(runContext.artifactsDir, "structured-user-message-attachments-result.json");
const screenshotPath = join(runContext.artifactsDir, "structured-user-message-attachments-failure.png");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
const harnessPath = dirname(testFile);
const imageBytes = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);
const pdfBytes = Buffer.from(
  "%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n",
  "utf8",
);
const attachmentRequests = [];
let brokenThumbnailAttempts = 0;
let failNotesOriginal = false;

const { createServer } = await import(
  pathToFileURL(
    `${projectRoot}/src/clients/web/node_modules/vite/dist/node/index.js`,
  ).href,
);

const vite = await createServer({
  root: harnessPath,
  logLevel: "error",
  server: {
    host: "127.0.0.1",
    port: 0,
    strictPort: false,
    fs: { allow: [projectRoot] },
  },
  resolve: {
    alias: {
      react: resolve(projectRoot, "src/clients/web/node_modules/react"),
      "react-dom": resolve(projectRoot, "src/clients/web/node_modules/react-dom"),
      "@floating-ui/react": resolve(
        projectRoot,
        "src/clients/web/node_modules/@floating-ui/react",
      ),
    },
  },
});
await vite.listen();
runContext.addCleanup("Structured attachment Vite", () => vite.close());

const address = vite.httpServer?.address();
if (!address || typeof address === "string") {
  throw new Error("无法读取结构化附件 Integration 的 Vite 端口");
}

const browser = await chromium.launch({ executablePath, headless: true });
runContext.addCleanup("Structured attachment Chromium", () => browser.close());
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await context.newPage();
const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(String(error)));

await page.route("**/api/gateway/auth/local-credential", async (route) => {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data: { token: "integration-local-token" } }),
  });
});
await page.route("**/api/v1/sessions/*/attachments/content?*", async (route) => {
  const url = new URL(route.request().url());
  const fileId = url.searchParams.get("file_id") || "";
  const variant = url.searchParams.get("variant") || "";
  const maxEdge = url.searchParams.get("max_edge");
  attachmentRequests.push({ fileId, variant, maxEdge });

  if (fileId.endsWith("broken.png") && variant === "thumbnail" && brokenThumbnailAttempts++ === 0) {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "模拟缩略图暂时不可用" }),
    });
    return;
  }
  if (fileId.endsWith(".png")) {
    await route.fulfill({ status: 200, contentType: "image/png", body: imageBytes });
    return;
  }
  if (fileId.endsWith("report.pdf")) {
    await route.fulfill({ status: 200, contentType: "application/pdf", body: pdfBytes });
    return;
  }
  if (fileId.endsWith("notes.txt")) {
    if (variant === "original" && failNotesOriginal) {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "模拟原件已被清理" }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "text/plain; charset=utf-8",
      body: "附件正文\n这段文本来自会话附件原件。",
    });
    return;
  }
  await route.fulfill({
    status: 404,
    contentType: "application/json",
    body: JSON.stringify({ detail: "测试附件不存在" }),
  });
});

const result = {
  thumbnailRequest: null,
  retryRecovered: false,
  noRawPayloadVisible: false,
  imageOpenedInResourcePanel: false,
  pdfOpenedInResourcePanel: false,
  textOpenedInResourcePanel: false,
  failedOriginalVisible: false,
  noPageErrors: false,
};

try {
  await page.goto(
    `http://127.0.0.1:${address.port}/structured_user_message_attachments_harness.html`,
    { waitUntil: "networkidle", timeout: 30_000 },
  );
  const messageSurface = page.getByTestId("message-surface");
  await messageSurface.getByRole("heading", { name: "请检查这些附件" }).waitFor();
  await page.getByText("重新加载附件预览").waitFor({ state: "visible", timeout: 10_000 });
  result.thumbnailRequest = attachmentRequests.find(
    (request) => request.fileId.endsWith("valid.png") && request.variant === "thumbnail",
  ) || null;
  if (!result.thumbnailRequest || result.thumbnailRequest.maxEdge !== "512") {
    throw new Error(`缩略图请求没有使用 512 边界: ${JSON.stringify(attachmentRequests)}`);
  }

  const initialText = await messageSurface.innerText();
  result.noRawPayloadVisible = !initialText.includes("image_url")
    && !initialText.includes("data:image")
    && !initialText.includes("base64");
  if (!result.noRawPayloadVisible) {
    throw new Error(`用户消息界面泄漏了 raw block/base64: ${initialText}`);
  }

  await page.getByText("重新加载附件预览").click();
  await messageSurface.locator('img[alt="broken.png"]').waitFor({ state: "visible", timeout: 10_000 });
  result.retryRecovered = true;

  await messageSurface.locator('button[aria-label="在右侧打开附件：valid.png"]').click();
  const resourcePanel = page.locator(".auxiliary-files-body");
  await resourcePanel.getByRole("region", { name: "附件预览：valid.png" }).waitFor({ state: "visible", timeout: 10_000 });
  await resourcePanel.locator('img[alt="valid.png"]').waitFor({ state: "visible", timeout: 10_000 });
  result.imageOpenedInResourcePanel = await page.locator('[role="dialog"]').count() === 0
    && attachmentRequests.some(
      (request) => request.fileId.endsWith("valid.png") && request.variant === "original",
    );

  await messageSurface.locator('button[aria-label="在右侧打开附件：report.pdf"]').click();
  await resourcePanel.getByRole("region", { name: "附件预览：report.pdf" }).waitFor({ state: "visible", timeout: 10_000 });
  await resourcePanel.locator('iframe[title="PDF 附件：report.pdf"]').waitFor({ state: "visible", timeout: 10_000 });
  result.pdfOpenedInResourcePanel = attachmentRequests.some(
    (request) => request.fileId.endsWith("report.pdf") && request.variant === "original",
  );

  await messageSurface.locator('button[aria-label="在右侧打开附件：notes.txt"]').click();
  await resourcePanel.getByRole("region", { name: "附件预览：notes.txt" }).waitFor({ state: "visible", timeout: 10_000 });
  await resourcePanel.getByText("这段文本来自会话附件原件。").waitFor({ state: "visible", timeout: 10_000 });
  result.textOpenedInResourcePanel = attachmentRequests.some(
    (request) => request.fileId.endsWith("notes.txt") && request.variant === "original",
  );

  failNotesOriginal = true;
  await messageSurface.locator('button[aria-label="在右侧打开附件：report.pdf"]').click();
  await resourcePanel.getByRole("region", { name: "附件预览：report.pdf" }).waitFor({ state: "visible", timeout: 10_000 });
  await messageSurface.locator('button[aria-label="在右侧打开附件：notes.txt"]').click();
  await resourcePanel.getByRole("alert").waitFor({ state: "visible", timeout: 10_000 });
  result.failedOriginalVisible = true;

  result.noPageErrors = pageErrors.length === 0;
  if (!result.retryRecovered || !result.imageOpenedInResourcePanel || !result.pdfOpenedInResourcePanel || !result.textOpenedInResourcePanel || !result.failedOriginalVisible || !result.noPageErrors) {
    throw new Error(`结构化附件边界断言失败: ${JSON.stringify(result)}`);
  }
  await writeFile(resultPath, `${JSON.stringify({ ...result, attachmentRequests }, null, 2)}\n`, "utf8");
  await runContext.writeResult("passed", { ...result, attachmentRequests });
} catch (error) {
  result.noPageErrors = pageErrors.length === 0;
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await runContext.writeResult("failed", {
    message: error instanceof Error ? error.message : String(error),
    screenshotPath,
    pageErrors,
    result,
  });
  throw error;
} finally {
  await runContext.close();
}
