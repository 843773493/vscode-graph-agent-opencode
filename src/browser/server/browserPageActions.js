import { TOOL_TIMEOUT_MS, normalizeToolResult, withTimeout } from "./browserRuntime.js";

export function selectorFor(refSelectors, { ref, selector, fieldPrefix = "" }) {
  if (selector) {
    return selector;
  }
  if (ref) {
    const mapped = refSelectors.get(ref);
    if (!mapped) {
      throw new Error(`未知元素 ref: ${fieldPrefix}${ref}。请先调用 readPage 获取最新 ref。`);
    }
    return mapped;
  }
  throw new Error(`${fieldPrefix}ref 或 ${fieldPrefix}selector 必须提供一个`);
}

export async function readBrowserSummary(page, refSelectors) {
  const result = await page.evaluate(() => {
    const refAttribute = "data-boxteam-ref";
    let counter = 0;
    const maxElements = 80;

    function visible(element) {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        style.visibility !== "hidden"
        && style.display !== "none"
        && rect.width > 0
        && rect.height > 0
      );
    }

    function textOf(element) {
      const aria = element.getAttribute("aria-label");
      const alt = element.getAttribute("alt");
      const title = element.getAttribute("title");
      const value = element.value;
      const text = element.innerText || element.textContent || "";
      return String(aria || alt || title || value || text || "")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 160);
    }

    const interactiveSelector = [
      "a",
      "button",
      "input",
      "textarea",
      "select",
      "summary",
      "[role=button]",
      "[role=link]",
      "[role=textbox]",
      "[contenteditable=true]",
      "[onclick]",
      "[draggable=true]",
    ].join(",");
    const elements = Array.from(document.querySelectorAll(interactiveSelector))
      .filter(visible)
      .slice(0, maxElements);
    const refs = [];
    for (const element of elements) {
      let ref = element.getAttribute(refAttribute);
      if (!ref) {
        counter += 1;
        ref = `e${Date.now().toString(36)}_${counter}`;
        element.setAttribute(refAttribute, ref);
      }
      refs.push({
        ref,
        tag: element.tagName.toLowerCase(),
        role: element.getAttribute("role") || "",
        type: element.getAttribute("type") || "",
        text: textOf(element),
        selector: `[${refAttribute}="${CSS.escape(ref)}"]`,
      });
    }

    const bodyText = (document.body?.innerText || "")
      .replace(/\s+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim()
      .slice(0, 6000);
    return {
      title: document.title || "",
      url: location.href,
      text: bodyText,
      refs,
    };
  });

  refSelectors.clear();
  for (const item of result.refs) {
    refSelectors.set(item.ref, item.selector);
  }

  const elementLines = result.refs.map((item) => {
    const role = item.role ? ` role=${item.role}` : "";
    const type = item.type ? ` type=${item.type}` : "";
    const text = item.text ? ` "${item.text}"` : "";
    return `- [ref=${item.ref}] <${item.tag}${role}${type}>${text}`;
  });
  const summary = [
    `页面标题: ${result.title || "(无标题)"}`,
    `URL: ${result.url}`,
    "",
    "可交互元素:",
    ...(elementLines.length > 0 ? elementLines : ["- (无)"]),
    "",
    "页面文本:",
    result.text || "(无可见文本)",
  ].join("\n");
  return { summary, refs: result.refs, title: result.title, url: result.url };
}

export async function clickElement(page, refSelectors, {
  ref = null,
  selector = null,
  dblClick = false,
  button = "left",
}) {
  const targetSelector = selectorFor(refSelectors, { ref, selector });
  const locator = page.locator(targetSelector).first();
  if (dblClick) {
    await locator.dblclick({ button, timeout: TOOL_TIMEOUT_MS });
  } else {
    await locator.click({ button, timeout: TOOL_TIMEOUT_MS });
  }
}

export async function hoverElement(page, refSelectors, { ref = null, selector = null }) {
  const targetSelector = selectorFor(refSelectors, { ref, selector });
  await page.locator(targetSelector).first().hover({ timeout: TOOL_TIMEOUT_MS });
}

export async function typeInPage(page, refSelectors, {
  ref = null,
  selector = null,
  text = null,
  key = null,
  submit = false,
}) {
  const hasTarget = Boolean(ref || selector);
  if (!text && !key) {
    throw new Error("text 或 key 必须提供一个");
  }
  const targetSelector = hasTarget ? selectorFor(refSelectors, { ref, selector }) : null;
  if (key) {
    if (targetSelector) {
      await page.locator(targetSelector).first().press(key, { timeout: TOOL_TIMEOUT_MS });
    } else {
      await page.keyboard.press(key);
    }
    return;
  }
  if (targetSelector) {
    const locator = page.locator(targetSelector).first();
    await locator.fill(text, { timeout: TOOL_TIMEOUT_MS });
    if (submit) {
      await locator.press("Enter", { timeout: TOOL_TIMEOUT_MS });
    }
  } else {
    await page.keyboard.type(text);
    if (submit) {
      await page.keyboard.press("Enter");
    }
  }
}

export async function dragElement(page, refSelectors, {
  fromRef = null,
  fromSelector = null,
  toRef = null,
  toSelector = null,
}) {
  const sourceSelector = selectorFor(refSelectors, {
    ref: fromRef,
    selector: fromSelector,
    fieldPrefix: "from",
  });
  const targetSelector = selectorFor(refSelectors, {
    ref: toRef,
    selector: toSelector,
    fieldPrefix: "to",
  });
  await page.dragAndDrop(sourceSelector, targetSelector, { timeout: TOOL_TIMEOUT_MS });
}

export async function handleDialog(session, {
  acceptModal = null,
  promptText = undefined,
  selectFiles = undefined,
}) {
  if (selectFiles !== undefined && selectFiles !== null) {
    if (!Array.isArray(selectFiles)) {
      throw new Error("selectFiles 必须是文件路径数组");
    }
    if (!session.pendingFileChooser) {
      throw new Error("当前页面没有待处理的文件选择对话框");
    }
    await session.pendingFileChooser.setFiles(selectFiles);
    session.pendingFileChooser = null;
    return { summary: `已选择 ${selectFiles.length} 个文件` };
  }
  if (!session.pendingDialog) {
    throw new Error("当前页面没有待处理的浏览器对话框");
  }
  const dialog = session.pendingDialog.dialog;
  const dialogMessage = session.pendingDialog.message;
  if (acceptModal === false) {
    await dialog.dismiss();
    session.pendingDialog = null;
    return { summary: `已取消对话框: ${dialogMessage}` };
  }
  if (typeof promptText === "string") {
    await dialog.accept(promptText);
  } else {
    await dialog.accept();
  }
  session.pendingDialog = null;
  return { summary: `已接受对话框: ${dialogMessage}` };
}

export async function screenshotPage(page, refSelectors, {
  ref = null,
  selector = null,
  scrollIntoViewIfNeeded = false,
}) {
  const targetSelector = ref || selector ? selectorFor(refSelectors, { ref, selector }) : null;
  if (!targetSelector) {
    return await page.screenshot({ type: "png" });
  }
  const locator = page.locator(targetSelector).first();
  if (scrollIntoViewIfNeeded) {
    await locator.scrollIntoViewIfNeeded();
  }
  return await locator.screenshot({ type: "png" });
}

export async function runPlaywrightCode({ page, context, browser }, {
  code,
  timeoutMs = TOOL_TIMEOUT_MS,
}) {
  if (typeof code !== "string" || !code.trim()) {
    throw new Error("runPlaywrightCode 需要 code");
  }
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const fn = new AsyncFunction("page", "context", "browser", code);
  const result = await withTimeout(
    Promise.resolve(fn(page, context, browser)),
    timeoutMs,
    "Playwright 代码执行",
  );
  return {
    result: normalizeToolResult(result),
    summary: "Playwright 代码执行完成",
  };
}
