import { TOOL_TIMEOUT_MS, normalizeToolResult, withTimeout } from "./browserRuntime.js";

export function selectorFor(refSelectors, { ref, selector, fieldPrefix = "" }) {
  if (selector) {
    return selector;
  }
  if (ref) {
    const mapped = refSelectors.get(ref);
    if (!mapped) {
      const error = new Error(`页面已变化或元素 ref 已失效: ${fieldPrefix}${ref}。请重新调用 readPage 获取最新 ref。`);
      error.code = "browser_stale_element_ref";
      throw error;
    }
    return typeof mapped === "string" ? mapped : mapped.selector;
  }
  throw new Error(`${fieldPrefix}ref 或 ${fieldPrefix}selector 必须提供一个`);
}

async function locatorFor(page, refSelectors, target) {
  const targetSelector = selectorFor(refSelectors, target);
  const locator = page.locator(targetSelector).first();
  if (await locator.count() === 0) {
    if (target.ref) {
      refSelectors.delete(target.ref);
    }
    const label = target.ref || target.selector;
    const error = new Error(`页面已变化或目标元素已移除: ${label}。请重新调用 readPage。`);
    error.code = "browser_stale_element_ref";
    throw error;
  }
  return locator;
}

export async function readBrowserSummary(page, refSelectors, documentRevision = 0) {
  const result = await page.evaluate((revision) => {
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
      if (!ref || !ref.startsWith(`r${revision}_`)) {
        counter += 1;
        ref = `r${revision}_e${counter}`;
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
      documentRevision: revision,
      text: bodyText,
      refs,
    };
  }, documentRevision);

  refSelectors.clear();
  for (const item of result.refs) {
    refSelectors.set(item.ref, { selector: item.selector, documentRevision });
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
  return {
    summary,
    refs: result.refs,
    title: result.title,
    url: result.url,
    document_revision: result.documentRevision,
  };
}

export async function inspectPageElement(page, refSelectors, { x, y }, documentRevision = 0) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw new Error(`元素命中坐标必须是有限数字: x=${x}, y=${y}`);
  }
  const result = await page.evaluate(({ pointX, pointY, revision }) => {
    const element = document.elementFromPoint(pointX, pointY);
    if (!(element instanceof Element)) {
      return null;
    }

    const refAttribute = "data-boxteam-ref";
    let ref = element.getAttribute(refAttribute);
    if (!ref || !ref.startsWith(`r${revision}_`)) {
      ref = `r${revision}_e${Math.random().toString(36).slice(2, 8)}`;
      element.setAttribute(refAttribute, ref);
    }
    const rect = element.getBoundingClientRect();
    const text = String(
      element.getAttribute("aria-label")
      || element.getAttribute("alt")
      || element.getAttribute("title")
      || ("value" in element ? element.value : "")
      || element.textContent
      || "",
    ).replace(/\s+/g, " ").trim().slice(0, 240);
    return {
      ref,
      selector: `[${refAttribute}="${CSS.escape(ref)}"]`,
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute("role") || "",
      type: element.getAttribute("type") || "",
      text,
      title: document.title || "",
      url: location.href,
      document_revision: revision,
      bounds: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      },
    };
  }, { pointX: x, pointY: y, revision: documentRevision });

  if (result) {
    refSelectors.set(result.ref, { selector: result.selector, documentRevision });
  }
  return result;
}

export async function clickElement(page, refSelectors, {
  ref = null,
  selector = null,
  dblClick = false,
  button = "left",
}) {
  const locator = await locatorFor(page, refSelectors, { ref, selector });
  if (dblClick) {
    await locator.dblclick({ button, timeout: TOOL_TIMEOUT_MS });
  } else {
    await locator.click({ button, timeout: TOOL_TIMEOUT_MS });
  }
}

export async function hoverElement(page, refSelectors, { ref = null, selector = null }) {
  const locator = await locatorFor(page, refSelectors, { ref, selector });
  await locator.hover({ timeout: TOOL_TIMEOUT_MS });
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
  const locator = hasTarget ? await locatorFor(page, refSelectors, { ref, selector }) : null;
  if (key) {
    if (locator) {
      await locator.press(key, { timeout: TOOL_TIMEOUT_MS });
    } else {
      await page.keyboard.press(key);
    }
    return;
  }
  if (locator) {
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
  const sourceLocator = await locatorFor(page, refSelectors, {
    ref: fromRef,
    selector: fromSelector,
    fieldPrefix: "from",
  });
  const targetLocator = await locatorFor(page, refSelectors, {
    ref: toRef,
    selector: toSelector,
    fieldPrefix: "to",
  });
  await sourceLocator.dragTo(targetLocator, { timeout: TOOL_TIMEOUT_MS });
}

export async function handleDialog(session, {
  acceptModal = null,
  promptText = undefined,
  selectFiles = undefined,
  filePayloads = undefined,
}) {
  if (filePayloads !== undefined) {
    if (!Array.isArray(filePayloads)) {
      throw new Error("filePayloads 必须是文件数组");
    }
    if (!session.pendingFileChooser) {
      throw new Error("当前页面没有待处理的文件选择对话框");
    }
    await session.pendingFileChooser.setFiles(filePayloads.map((file) => ({
      name: file.name,
      mimeType: file.mimeType || "application/octet-stream",
      buffer: Buffer.from(file.data, "base64"),
    })));
    session.pendingFileChooser = null;
    return { summary: `已从用户设备选择 ${filePayloads.length} 个文件` };
  }
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
  if (!ref && !selector) {
    return await page.screenshot({ type: "png" });
  }
  const locator = await locatorFor(page, refSelectors, { ref, selector });
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
