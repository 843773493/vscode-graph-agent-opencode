import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";
import { chromium } from "playwright";

const { createServer } = await import(
  pathToFileURL(`${process.cwd()}/src/web/node_modules/vite/dist/node/index.js`).href,
);

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const projectRoot = requiredEnvironment("BOXTEAM_PROJECT_ROOT");
const resultPath = requiredEnvironment("BOXTEAM_E2E_RESULT_PATH");
const screenshotPath = requiredEnvironment("BOXTEAM_E2E_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
const harnessPath = `${projectRoot}/tests/e2e/clients/web`;

const vite = await createServer({
  root: harnessPath,
  logLevel: "error",
  server: {
    host: "127.0.0.1",
    port: 0,
    strictPort: false,
    fs: {
      allow: [projectRoot],
    },
  },
  resolve: {
    alias: {
      react: resolve(projectRoot, "src/web/node_modules/react"),
      "react-dom": resolve(projectRoot, "src/web/node_modules/react-dom"),
      "@floating-ui/react": resolve(
        projectRoot,
        "src/web/node_modules/@floating-ui/react",
      ),
    },
  },
});
await vite.listen();

const address = vite.httpServer?.address();
if (!address || typeof address === "string") {
  throw new Error("无法读取 AnchoredOverlay 回归测试的 Vite 端口");
}

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
const result = {
  anchor: null,
  overlay: null,
  viewport: { width: 1280, height: 800 },
};

try {
  await page.goto(
    `http://127.0.0.1:${address.port}/anchored_overlay_positioning_harness.html`,
    { waitUntil: "networkidle", timeout: 30_000 },
  );
  const anchor = page.getByTestId("anchor");
  await anchor.click();
  const overlay = page.getByTestId("overlay");
  await overlay.waitFor({ state: "visible", timeout: 10_000 });
  await page.waitForFunction(() => {
    const element = document.querySelector('[data-testid="overlay"]');
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0
      && rect.height > 0
      && rect.left > 0
      && rect.top > 0;
  }, undefined, { timeout: 10_000 });

  const measurements = await page.evaluate(() => {
    const anchorElement = document.querySelector('[data-testid="anchor"]');
    const overlayElement = document.querySelector('[data-testid="overlay"]');
    const positioner = overlayElement?.parentElement;
    if (!anchorElement || !overlayElement || !positioner) {
      throw new Error("定位回归测试缺少锚点或浮层元素");
    }
    const anchorRect = anchorElement.getBoundingClientRect();
    const overlayRect = overlayElement.getBoundingClientRect();
    return {
      anchor: {
        left: anchorRect.left,
        top: anchorRect.top,
        right: anchorRect.right,
        bottom: anchorRect.bottom,
      },
      overlay: {
        left: overlayRect.left,
        top: overlayRect.top,
        right: overlayRect.right,
        bottom: overlayRect.bottom,
        width: overlayRect.width,
        height: overlayRect.height,
      },
      visibility: getComputedStyle(positioner).visibility,
    };
  });
  result.anchor = measurements.anchor;
  result.overlay = measurements.overlay;

  if (measurements.visibility !== "visible") {
    throw new Error("浮层定位完成后仍不可见");
  }
  if (measurements.overlay.left <= 0 || measurements.overlay.top <= 0) {
    throw new Error(
      `浮层出现在左上角: ${JSON.stringify(measurements.overlay)}`,
    );
  }
  if (measurements.overlay.right > result.viewport.width
      || measurements.overlay.bottom > result.viewport.height) {
    throw new Error(
      `浮层超出视口: ${JSON.stringify(measurements.overlay)}`,
    );
  }
  const alignsWithAnchor = Math.abs(
    measurements.overlay.left - measurements.anchor.left,
  ) <= 2 || Math.abs(
    measurements.overlay.right - measurements.anchor.right,
  ) <= 2;
  if (!alignsWithAnchor) {
    throw new Error(
      `浮层没有与锚点水平对齐或边界对齐: ${JSON.stringify(measurements)}`,
    );
  }
  const verticalGap = Math.min(
    Math.abs(measurements.overlay.bottom - measurements.anchor.top),
    Math.abs(measurements.overlay.top - measurements.anchor.bottom),
  );
  if (verticalGap > 8) {
    throw new Error(
      `浮层没有贴近锚点: ${JSON.stringify(measurements)}`,
    );
  }

  await writeFile(resultPath, JSON.stringify(result, null, 2), "utf8");
} catch (error) {
  await page.screenshot({ path: screenshotPath, fullPage: true });
  throw error;
} finally {
  await browser.close();
  await vite.close();
}
