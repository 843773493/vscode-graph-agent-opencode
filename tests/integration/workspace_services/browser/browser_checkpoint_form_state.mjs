import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdir } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BrowserSession } from "../../../../src/workspace-services/browser/server/browserSession.js";
import {
  captureBrowserCheckpoint,
  restoreBrowserCheckpoint,
} from "../../../../src/workspace-services/browser/server/resources/browserCheckpoint.js";
import { BrowserRuntimePool } from "../../../../src/workspace-services/browser/server/resources/browserRuntimePool.js";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

const run = await TestRunContext.fromTestFile(fileURLToPath(import.meta.url)).prepare();
const runtimeDirectory = path.join(run.outputRoot, "runtime");
await mkdir(runtimeDirectory, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtimeDirectory;
const results = [];
const server = createServer((request, response) => {
  const rows = Number(new URL(request.url, "http://localhost").searchParams.get("rows"));
  response.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(`<!doctype html>
    <html><head><title>检查点表单</title></head><body><section><fieldset>
      ${Array.from({ length: rows }, (_, index) => `<label>字段${index}</label><input value="初始${index}">`).join("")}
    </fieldset><fieldset><textarea>多行正文</textarea><span>非控件兄弟</span>
      <select multiple><option value="a">a</option><option value="b">b</option><option value="c">c</option></select>
      <input type="checkbox"><input value="同类兄弟">
    </fieldset></section><section><input id="中文 空格" value="转义ID"><textarea id="field:with.dot">转义符号</textarea></section>
    <div style="height:2000px"></div></body></html>`);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
run.addCleanup("表单HTTP服务", () => new Promise((resolve, reject) =>
  server.close((error) => error ? reject(error) : resolve())));
const pool = new BrowserRuntimePool();
run.addCleanup("Chromium运行时池", () => pool.shutdown());
const manager = { runtimePool: pool, persist: async () => undefined, schedulePersist: () => undefined };
const scenarios = [
  ["混合控件定位、状态与存储经释放和恢复保持", 8, async (session) => {
    await session.page.evaluate(() => {
      for (const [index, control] of [...document.querySelectorAll("input, textarea, select")].entries()) {
        if (control instanceof HTMLSelectElement) {
          for (const option of control.options) option.selected = option.value !== "a";
        } else if (control instanceof HTMLInputElement && control.type === "checkbox") {
          control.checked = true;
        } else control.value = `已编辑 ${index}`;
      }
      localStorage.setItem("checkpoint-local", "本地冻结正文");
      sessionStorage.setItem("checkpoint-session", "会话冻结正文");
      window.scrollTo(0, 300);
    });
    const checkpoint = await captureBrowserCheckpoint(session);
    await assertControlsResolve(session.page, checkpoint.pages[0].controls);
    const originalPageId = session.activePageId;
    await session.releaseRuntime();
    await restoreBrowserCheckpoint(session, checkpoint);
    const restored = await captureBrowserCheckpoint(session);
    assert.equal(session.activePageId, originalPageId);
    assert.deepEqual(restored.pages[0].controls, checkpoint.pages[0].controls);
    assert.deepEqual(restored.pages[0].session_storage, checkpoint.pages[0].session_storage);
    assert.equal(restored.pages[0].scroll_y, checkpoint.pages[0].scroll_y);
    assert.equal(await session.page.evaluate(() => localStorage.getItem("checkpoint-local")), "本地冻结正文");
    await assertControlsResolve(session.page, restored.pages[0].controls);
    return { controls: restored.pages[0].controls.length, restoredPageId: originalPageId };
  }],
  ["后一次采集重新计算插入、删除和重排后的定位", 4, async (session) => {
    const original = await captureBrowserCheckpoint(session);
    await session.page.evaluate(() => {
      const fieldset = document.querySelector("fieldset");
      fieldset.querySelectorAll("input")[1].remove();
      const inserted = document.createElement("input");
      inserted.value = "新插入";
      fieldset.prepend(inserted);
      const section = document.querySelector("section");
      section.prepend(section.lastElementChild);
    });
    const changed = await captureBrowserCheckpoint(session);
    assert.equal(changed.pages[0].controls.length, original.pages[0].controls.length);
    assert(changed.pages[0].controls.some((control) => control.value === "新插入"));
    await assertControlsResolve(session.page, changed.pages[0].controls);
    return { controls: changed.pages[0].controls.length };
  }],
  ["大型匿名表单的兄弟元素扫描随DOM规模有界增长", 2000, async (session) => {
    await session.page.evaluate(() => {
      const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "children");
      window.checkpointSiblingScan = { reads: 0, children: 0 };
      Object.defineProperty(Element.prototype, "children", {
        configurable: true,
        get() {
          const children = descriptor.get.call(this);
          window.checkpointSiblingScan.reads += 1;
          window.checkpointSiblingScan.children += children.length;
          return children;
        },
      });
      window.restoreCheckpointChildrenGetter = () => Object.defineProperty(Element.prototype, "children", descriptor);
    });
    let checkpoint;
    try {
      checkpoint = await captureBrowserCheckpoint(session);
    } finally {
      await session.page.evaluate(() => window.restoreCheckpointChildrenGetter());
    }
    const scan = await session.page.evaluate(() => ({ ...window.checkpointSiblingScan, elements: document.querySelectorAll("*").length }));
    assert.equal(checkpoint.pages[0].controls.length, 2006);
    await assertControlsResolve(session.page, checkpoint.pages[0].controls);
    assert(scan.children <= scan.elements * 5,
      `DOM有${scan.elements}个元素，生成定位符却累计扫描${scan.children}个兄弟元素`);
    return scan;
  }],
];

async function assertControlsResolve(page, controls) {
  assert.equal(await page.evaluate((saved) => {
    const current = [...document.querySelectorAll("input, textarea, select")];
    return saved.length === current.length && saved.every((control, index) =>
      document.querySelector(control.selector) === current[index]);
  }, controls), true, "保存的定位符没有逐项指向原控件");
}

try {
  for (const [name, rows, scenario] of scenarios) {
    const session = new BrowserSession({ manager, record: {
      browser_id: `browser_${randomUUID().replaceAll("-", "")}`,
      workspace_id: "00000000-0000-4000-8000-000000000001", transport_owner_revision: 1,
      reconciliation_status: "verified", url: `http://127.0.0.1:${server.address().port}/?rows=${rows}`,
    } });
    try {
      await session.start();
      results.push({ name, status: "passed", measurements: await scenario(session) });
    } catch (error) {
      results.push({ name, status: "failed", error: error.stack ?? String(error) });
    } finally {
      session.closingRequested = true;
      await session.releaseRuntime();
    }
  }
  const failed = results.filter((result) => result.status === "failed");
  await run.writeResult(failed.length ? "failed" : "passed", {
    scenarios: results,
    scope: "真实Chromium、HTTP表单、BrowserSession与检查点捕获/恢复；Manager持久化边界为空操作，不含Gateway或主Web",
  });
  console.log(JSON.stringify({ scenarios: results, artifacts: run.artifactsDir }, null, 2));
  assert.equal(failed.length, 0, "检查点表单集成场景失败");
} finally {
  await run.close();
}
