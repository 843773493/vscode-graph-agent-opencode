import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import {
  clickElement,
  inspectPageElement,
  readBrowserSummary,
} from "../../../../src/workspace-services/browser/server/operations/browserPageActions.js";
import { browserLaunchOptions } from "../../../../src/workspace-services/browser/server/browserRuntime.js";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

const run = await TestRunContext.fromTestFile(fileURLToPath(import.meta.url)).prepare();
const runtimeDirectory = path.join(run.outputRoot, "runtime");
await mkdir(runtimeDirectory, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtimeDirectory;
const results = [];
const scenarios = [
  ["新增元素的引用唯一，点击落在新元素", async (page, refs) => {
    const original = await readBrowserSummary(page, refs);
    await page.evaluate(() => {
      document.body.insertAdjacentHTML("beforeend", '<button id="second">second</button>');
    });
    const updated = await readBrowserSummary(page, refs);
    assert.equal(updated.refs[0].ref, original.refs[0].ref);
    const added = updated.refs.find((item) => item.text === "second");
    await clickElement(page, refs, { ref: added.ref });
    assert.equal(await page.evaluate(() => document.body.dataset.clicked), "second");
    assert.equal(new Set(updated.refs.map((item) => item.ref)).size, 2);
  }],
  ["移除元素的引用不重新分配给后来的元素", async (page, refs) => {
    const original = await readBrowserSummary(page, refs);
    const removedRef = original.refs[0].ref;
    await page.evaluate(() => {
      document.querySelector("#first").remove();
      document.body.insertAdjacentHTML("beforeend", '<button id="second">second</button>');
    });
    const updated = await readBrowserSummary(page, refs);
    assert.notEqual(updated.refs[0].ref, removedRef);
    await assert.rejects(clickElement(page, refs, { ref: removedRef }), {
      code: "browser_stale_element_ref",
    });
    assert.equal(await page.evaluate(() => document.body.dataset.clicked), undefined);
  }],
  ["元素检查与摘要共用稳定引用，重排后仍指向同一元素", async (page, refs) => {
    await readBrowserSummary(page, refs);
    await page.evaluate(() => {
      document.body.insertAdjacentHTML("beforeend", '<button id="second">second</button>');
    });
    const bounds = await page.locator("#second").boundingBox();
    const inspected = await inspectPageElement(page, refs, {
      x: bounds.x + bounds.width / 2,
      y: bounds.y + bounds.height / 2,
    });
    const summary = await readBrowserSummary(page, refs);
    assert.equal(summary.refs.find((item) => item.text === "second").ref, inspected.ref);
    await page.evaluate(() => document.body.prepend(document.querySelector("#second")));
    const reordered = await readBrowserSummary(page, refs);
    assert.equal(reordered.refs[0].ref, inspected.ref);
    await clickElement(page, refs, { ref: inspected.ref });
    assert.equal(await page.evaluate(() => document.body.dataset.clicked), "second");
  }],
  ["文档版本变化后旧引用失效", async (page, refs) => {
    const original = await readBrowserSummary(page, refs, 0);
    const updated = await readBrowserSummary(page, refs, 1);
    assert.notEqual(updated.refs[0].ref, original.refs[0].ref);
    await assert.rejects(clickElement(page, refs, { ref: original.refs[0].ref }), {
      code: "browser_stale_element_ref",
    });
    await clickElement(page, refs, { ref: updated.refs[0].ref });
    assert.equal(await page.evaluate(() => document.body.dataset.clicked), "first");
  }],
  ["摘要达到元素上限后停止尾部可见性检查", async (page, refs) => {
    await page.evaluate(() => {
      const hidden = [
        'display:none',
        'visibility:hidden',
        'width:0;height:0;padding:0;border:0',
        'width:0;height:10px;padding:0;border:0',
      ].map((style) => `<button style="${style}">hidden</button>`).join("");
      document.body.innerHTML = hidden + Array.from({ length: 5_000 }, (_, index) =>
        `<button id="visible_${index}">visible_${index}</button>`).join("");
      const original = window.getComputedStyle.bind(window);
      window.summaryVisibilityChecks = 0;
      window.getComputedStyle = (...args) => {
        window.summaryVisibilityChecks += 1;
        return original(...args);
      };
    });
    const summary = await readBrowserSummary(page, refs);
    assert.equal(summary.refs.length, 80);
    assert.deepEqual(summary.refs.map((item) => item.text),
      Array.from({ length: 80 }, (_, index) => `visible_${index}`));
    const checks = await page.evaluate(() => window.summaryVisibilityChecks);
    assert(checks <= 100, `只需前80个可见元素，却检查了${checks}个候选元素`);
    return { candidateElements: 5_004, returnedElements: summary.refs.length, visibilityChecks: checks };
  }],
  ["摘要标签保持来源优先级，已有标签和值时不读取无关正文", async (page, refs) => {
    const expected = [
      "首选 标签", "替代 标签", "标题 标签", "输入 值", "多行 值", "", "0",
      "普通 正文", "隐藏备用正文", "a".repeat(159) + "\ud83d",
    ];
    await page.evaluate(() => {
      document.body.replaceChildren();
      const rows = [
        { tag: "button", attributes: { "aria-label": " \t首选\n标签 ", alt: "备用alt", title: "备用title", value: "备用value" } },
        { tag: "button", attributes: { "aria-label": "", alt: "替代  标签", title: "备用title", value: "备用value" } },
        { tag: "button", attributes: { title: "标题\t标签", value: "备用value" } },
        { tag: "input", value: "输入  值" },
        { tag: "textarea", value: "多行\n值" },
        { tag: "button", attributes: { "aria-label": " \t\n ", title: "不能回退到标题" } },
        { tag: "input", value: "0" },
        { tag: "button", text: "普通 \n 正文" },
        { tag: "button", hiddenText: "隐藏备用正文" },
        { tag: "button", attributes: { "aria-label": "a".repeat(159) + "😀尾部" } },
      ];
      const innerText = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
      const textContent = Object.getOwnPropertyDescriptor(Node.prototype, "textContent");
      window.summaryLabelReads = [];
      for (const [index, row] of rows.entries()) {
        const element = document.createElement(row.tag);
        element.id = `label_${index}`;
        element.style.cssText = "display:inline-block;width:120px;height:32px;overflow:hidden";
        for (const [name, value] of Object.entries(row.attributes || {})) element.setAttribute(name, value);
        if (row.value !== undefined) element.value = row.value;
        if (row.hiddenText) {
          const span = document.createElement("span");
          span.hidden = true;
          span.textContent = row.hiddenText;
          element.append(span);
        } else element.textContent = row.text ?? "无关正文".repeat(2000);
        const counts = { innerText: 0, textContent: 0 };
        window.summaryLabelReads.push(counts);
        Object.defineProperty(element, "innerText", {
          get() { counts.innerText++; return innerText.get.call(this); },
        });
        Object.defineProperty(element, "textContent", {
          get() { counts.textContent++; return textContent.get.call(this); },
        });
        document.body.append(element);
      }
    });
    const summary = await readBrowserSummary(page, refs);
    assert.deepEqual(summary.refs.map(item => item.text), expected);
    assert.equal(new Set(summary.refs.map(item => item.ref)).size, expected.length);
    const reads = await page.evaluate(() => window.summaryLabelReads);
    assert.deepEqual(reads, expected.map((_, index) => ({
      innerText: index === 7 || index === 8 ? 1 : 0,
      textContent: index === 8 ? 1 : 0,
    })), `摘要标签不应预读更低优先级正文: ${JSON.stringify(reads)}`);
    return { elements: expected.length, reads };
  }],
  ["元素检查只整理一次CSSOM，下一次检查仍读取规则更新", async (page, refs) => {
    const depth = 24;
    const unrelatedRuleCount = 200;
    await page.setContent(`<!doctype html><html><head><style id="rules">
      body { --accent: rgb(10, 20, 30); color: var(--accent); }
      .level { font-size: 14px; }
      @media screen { @supports (display: block) { #inspected, .other { width: 120px; } } }
      #inspected::before { content: "标记"; color: var(--accent); }
      ${Array.from({ length: unrelatedRuleCount }, (_, index) => `.absent-${index} { padding-left: ${index}px; }`).join("\n")}
    </style><style>.level { color: var(--accent); }</style></head>
    <body>${'<div class="level">'.repeat(depth)}
      <button id="inspected" style="height: 40px;">正文🙂</button>
    ${'</div>'.repeat(depth)}</body></html>`);
    await page.evaluate(() => {
      const selectorDescriptor = Object.getOwnPropertyDescriptor(CSSStyleRule.prototype, "selectorText");
      const sheetDescriptor = Object.getOwnPropertyDescriptor(CSSStyleSheet.prototype, "cssRules");
      window.inspectionCounts = { selectorReads: 0, sheetReads: 0 };
      Object.defineProperty(CSSStyleRule.prototype, "selectorText", {
        ...selectorDescriptor,
        get() { window.inspectionCounts.selectorReads += 1; return selectorDescriptor.get.call(this); },
      });
      Object.defineProperty(CSSStyleSheet.prototype, "cssRules", {
        ...sheetDescriptor,
        get() { window.inspectionCounts.sheetReads += 1; return sheetDescriptor.get.call(this); },
      });
    });
    const bounds = await page.locator("#inspected").boundingBox();
    const point = { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
    const inspected = await inspectPageElement(page, refs, point);
    assert.equal(inspected.id, "inspected");
    assert.equal(inspected.ancestors.length, depth + 3);
    assert.match(inspected.computedStyle, /^element \{ height: 40px; \}/);
    assert.match(inspected.computedStyle, /#inspected \{ width: 120px; \}/);
    assert.doesNotMatch(inspected.computedStyle, /\.other/);
    assert.match(inspected.computedStyle, /Pseudo-elements/);
    assert.match(inspected.computedStyle, /Inherited/);
    assert.equal(inspected.computedStyles["--accent"], "rgb(10, 20, 30)");
    const firstCounts = await page.evaluate(() => window.inspectionCounts);
    assert.deepEqual(firstCounts, { selectorReads: unrelatedRuleCount + 5, sheetReads: 2 });

    await page.evaluate(() => {
      const sheet = document.querySelector("#rules").sheet;
      sheet.insertRule("#inspected { background-color: rgb(40, 50, 60); }", sheet.cssRules.length);
      window.inspectionCounts = { selectorReads: 0, sheetReads: 0 };
    });
    const updated = await inspectPageElement(page, refs, point);
    assert.equal(updated.ref, inspected.ref);
    assert.equal(updated.computedStyles["background-color"], "rgb(40, 50, 60)");
    assert.match(updated.computedStyle, /#inspected \{ background-color: rgb\(40, 50, 60\); \}/);
    assert.deepEqual({ ...updated, computedStyle: inspected.computedStyle, computedStyles: inspected.computedStyles }, inspected);
    const secondCounts = await page.evaluate(() => window.inspectionCounts);
    assert.deepEqual(secondCounts, { selectorReads: unrelatedRuleCount + 6, sheetReads: 2 });
    return { depth, firstCounts, secondCounts, updatedRuleObserved: true };
  }],
  ["跨域CSSOM拒绝只读取一次，可读规则与最终样式仍返回", async (page, refs) => {
    await page.route("http://inspection.test/", (route) => route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html><head>
        <style>body { font-size: 17px; } #inspected { width: 120px; height: 40px; }</style>
        <link rel="stylesheet" href="http://inspection-styles.test/style.css">
      </head><body><main><section><button id="inspected">跨域样式</button></section></main></body></html>`,
    }));
    await page.route("http://inspection-styles.test/style.css", (route) => route.fulfill({
      contentType: "text/css",
      body: "#inspected { color: rgb(70, 80, 90); }",
    }));
    await page.goto("http://inspection.test/");
    await page.evaluate(() => {
      const descriptor = Object.getOwnPropertyDescriptor(CSSStyleSheet.prototype, "cssRules");
      window.inspectionCounts = { sheetReads: 0, deniedReads: 0 };
      Object.defineProperty(CSSStyleSheet.prototype, "cssRules", {
        ...descriptor,
        get() {
          window.inspectionCounts.sheetReads += 1;
          try {
            return descriptor.get.call(this);
          } catch (error) {
            if (error.name === "SecurityError") window.inspectionCounts.deniedReads += 1;
            throw error;
          }
        },
      });
    });
    const bounds = await page.locator("#inspected").boundingBox();
    const inspected = await inspectPageElement(page, refs, {
      x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2,
    });
    assert.equal(inspected.id, "inspected");
    assert.equal(inspected.computedStyles.color, "rgb(70, 80, 90)");
    assert.match(inspected.computedStyle, /#inspected \{ width: 120px; height: 40px; \}/);
    assert.match(inspected.computedStyle, /body \{ font-size: 17px; \}/);
    assert.doesNotMatch(inspected.computedStyle, /#inspected \{ color:/);
    const counts = await page.evaluate(() => window.inspectionCounts);
    assert.deepEqual(counts, { sheetReads: 2, deniedReads: 1 });
    return counts;
  }],
];

try {
  const browser = await chromium.launch(browserLaunchOptions());
  run.addCleanup("Chromium", () => browser.close());
  for (const [name, scenario] of scenarios) {
    const page = await browser.newPage();
    try {
      await page.setContent(`<!doctype html><html><body>
        <button id="first">first</button>
        <script>document.addEventListener("click", (event) => {
          document.body.dataset.clicked = event.target.id;
        });</script>
      </body></html>`);
      const measurements = await scenario(page, new Map());
      results.push({ name, status: "passed", measurements });
    } catch (error) {
      results.push({ name, status: "failed", error: error.stack ?? String(error) });
    } finally {
      await page.close();
    }
  }
  const failed = results.filter((result) => result.status === "failed");
  await run.writeResult(failed.length ? "failed" : "passed", {
    scenarios: results,
    scope: "真实 Chromium DOM、页面摘要、元素检查与按 ref 点击；不含 Gateway 或主 Web",
  });
  console.log(JSON.stringify({ scenarios: results, artifacts: run.artifactsDir }, null, 2));
  assert.equal(failed.length, 0, "元素引用集成场景失败");
} finally {
  await run.close();
}
