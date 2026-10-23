import { TOOL_TIMEOUT_MS, browserRuntimeFailure, normalizeToolResult, withTimeout } from "../browserRuntime.js";

const PLAYWRIGHT_ACTION_TIMEOUT_RESERVE_MS = 250;
const STALE_SELECTOR_WAIT_TIMEOUT_MS = 1_500;
const MAX_SUMMARY_ELEMENTS = 80;

// 标签页的 ref 索引只保存当前可定位项；清空索引不能让已发出的编号重新指向其他元素。
// 在 await 前预留本次最多需要的连续编号，摘要与元素检查共用同一分配入口。
const nextElementRefNumbers = new WeakMap();

function reserveElementRefNumbers(refSelectors, count) {
  const firstElementRefNumber = nextElementRefNumbers.get(refSelectors) ?? 1;
  nextElementRefNumbers.set(refSelectors, firstElementRefNumber + count);
  return firstElementRefNumber;
}

function configurePlaywrightTimeoutBudget(page, timeoutMs) {
  const actionTimeoutMs = Math.max(1, timeoutMs - PLAYWRIGHT_ACTION_TIMEOUT_RESERVE_MS);
  if (typeof page.setDefaultTimeout === "function") {
    page.setDefaultTimeout(actionTimeoutMs);
  }
  if (typeof page.setDefaultNavigationTimeout === "function") {
    page.setDefaultNavigationTimeout(actionTimeoutMs);
  }
}

function isPlaywrightStaleElementError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return /detached from the DOM|not attached to the DOM|element is not attached|frame was detached/i.test(message);
}

function staleElementError(label, cause) {
  const error = new Error(`页面已变化或目标元素已移除: ${label}。请重新调用 readPage。`);
  error.code = "browser_stale_element_ref";
  error.cause = cause;
  return error;
}

async function withLocatorAction(label, action) {
  try {
    return await action();
  } catch (error) {
    if (isPlaywrightStaleElementError(error)) {
      throw staleElementError(label, error);
    }
    throw error;
  }
}

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
    // 导航完成后，应用可能还在用脚本挂载画布或替换根节点。对显式
    // selector 等待一次短暂的 DOM 稳定窗口，避免把可恢复的导航竞态
    // 误报成永久失效的元素引用。ref 不在这里自动复用，防止旧 ref
    // 被重新分配给另一个按钮。
    if (target.selector && typeof page.waitForSelector === "function") {
      try {
        await page.waitForSelector(targetSelector, {
          state: "attached",
          timeout: STALE_SELECTOR_WAIT_TIMEOUT_MS,
        });
        if (await locator.count() > 0) {
          return locator;
        }
      } catch {
        // 下方统一返回可诊断的 stale 错误；不吞掉真实失败。
      }
    }
    if (target.ref) {
      refSelectors.delete(target.ref);
    }
    const label = target.ref || target.selector;
    throw staleElementError(label);
  }
  return locator;
}

function isStaleElementError(error) {
  return error?.code === "browser_stale_element_ref";
}

export async function runWithStaleSelectorRecovery(
  page,
  refSelectors,
  target,
  action,
  refreshPage,
) {
  try {
    return await action();
  } catch (error) {
    if (!isStaleElementError(error) || !target?.selector || !refreshPage) {
      throw error;
    }
    // selector 是稳定的调用意图；readPage 只刷新 ref 索引和文档状态，
    // 不重放用户动作。动作本身最多执行两次，避免点击等副作用无限重试。
    await refreshPage();
    try {
      return await action();
    } catch (recoveredError) {
      if (isStaleElementError(recoveredError)) {
        recoveredError.message = `${recoveredError.message} 页面已重新读取但 selector 仍不存在，请使用最新页面摘要中的实际元素，不要继续重放旧 selector。`;
        recoveredError.recovery = "read_page";
      }
      throw recoveredError;
    }
  }
}

export async function readBrowserSummary(page, refSelectors, documentRevision = 0) {
  const firstElementRefNumber = reserveElementRefNumbers(refSelectors, MAX_SUMMARY_ELEMENTS);
  const result = await page.evaluate(({ revision, firstRefNumber, maxElements }) => {
    const refAttribute = "data-boxteam-ref";
    let nextElementRefNumber = firstRefNumber;

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

    function summaryLabelOf(element) {
      // 优先级按原始值判定；已有标签或表单值时不读取无关的完整正文。
      return String(
        element.getAttribute("aria-label")
        || element.getAttribute("alt")
        || element.getAttribute("title")
        || element.value
        || element.innerText
        || element.textContent
        || "",
      )
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
    const elements = [];
    for (const element of document.querySelectorAll(interactiveSelector)) {
      if (visible(element)) elements.push(element);
      if (elements.length >= maxElements) break;
    }
    const refs = [];
    for (const element of elements) {
      let ref = element.getAttribute(refAttribute);
      if (!ref || !ref.startsWith(`r${revision}_`)) {
        ref = `r${revision}_e${nextElementRefNumber++}`;
        element.setAttribute(refAttribute, ref);
      }
      refs.push({
        ref,
        tag: element.tagName.toLowerCase(),
        role: element.getAttribute("role") || "",
        type: element.getAttribute("type") || "",
        text: summaryLabelOf(element),
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
  }, {
    revision: documentRevision,
    firstRefNumber: firstElementRefNumber,
    maxElements: MAX_SUMMARY_ELEMENTS,
  });

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
  const newElementRef = `r${documentRevision}_e${reserveElementRefNumbers(refSelectors, 1)}`;
  const result = await page.evaluate(({ pointX, pointY, revision, newRef }) => {
    const element = document.elementFromPoint(pointX, pointY);
    if (!(element instanceof Element)) {
      return null;
    }

    const refAttribute = "data-boxteam-ref";
    let ref = element.getAttribute(refAttribute);
    if (!ref || !ref.startsWith(`r${revision}_`)) {
      ref = newRef;
      element.setAttribute(refAttribute, ref);
    }
    const rect = element.getBoundingClientRect();
    const shorten = (value, maxLength) => {
      const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
      return normalized.length > maxLength
        ? `${normalized.slice(0, maxLength - 1)}…`
        : normalized;
    };
    const attributes = Object.fromEntries(
      [...element.attributes]
        .filter((attribute) => attribute.name !== refAttribute)
        .map((attribute) => [attribute.name, attribute.value]),
    );
    const text = String(
      element.getAttribute("aria-label")
      || element.getAttribute("alt")
      || element.getAttribute("title")
      || ("value" in element ? element.value : "")
      || element.textContent
      || "",
    ).replace(/\s+/g, " ").trim().slice(0, 240);
    const ancestors = [];
    let current = element;
    while (current instanceof Element) {
      ancestors.unshift({
        tagName: current.tagName.toLowerCase(),
        ...(current.id ? { id: current.id } : {}),
        classNames: [...current.classList],
      });
      current = current.parentElement;
    }

    const keyComputedProperties = new Set([
      "display",
      "position",
      "margin",
      "margin-top",
      "margin-right",
      "margin-bottom",
      "margin-left",
      "padding",
      "padding-top",
      "padding-right",
      "padding-bottom",
      "padding-left",
      "font-size",
      "font-family",
      "color",
      "background-color",
    ]);
    const inheritableProperties = new Set([
      "color",
      "cursor",
      "direction",
      "font",
      "font-family",
      "font-size",
      "font-style",
      "font-weight",
      "letter-spacing",
      "line-height",
      "list-style",
      "text-align",
      "text-indent",
      "text-transform",
      "visibility",
      "white-space",
      "word-break",
      "word-spacing",
      "writing-mode",
    ]);
    const referencedVars = new Set();
    const authorPropertyNames = new Set(["display", "height", "width"]);
    const normalRuleLines = [];
    const pseudoRuleLines = [];
    const inheritedRuleLines = [];
    const seenRuleLines = new Set();

    function collectDeclarations(style, propertyNames, onlyInheritable = false) {
      for (const property of style) {
        const value = style.getPropertyValue(property);
        if (!value || property.startsWith("--")
          || (onlyInheritable && !inheritableProperties.has(property))) {
          continue;
        }
        propertyNames.add(property);
        for (const match of value.matchAll(/var\(\s*(--[a-zA-Z0-9_-]+)/g)) {
          referencedVars.add(match[1]);
        }
      }
    }

    function selectorMatches(target, selector) {
      const normalized = selector.trim();
      const pseudoMatch = normalized.match(/^(.*?)(::[a-zA-Z-]+)(?:\(.*\))?$/);
      const targetSelector = pseudoMatch ? pseudoMatch[1].trim() : normalized;
      try {
        return target.matches(targetSelector || "*");
      } catch {
        return false;
      }
    }

    // 一次同步检查内只读取和拆分一次CSSOM；不跨调用缓存，后续检查仍读取最新规则。
    const styleRules = [];
    function collectStyleRules(rules) {
      for (const rule of rules || []) {
        if (rule.type === 1) {
          const selectors = String(rule.selectorText || "")
            .split(",")
            .map((selector) => selector.trim())
            .filter(Boolean);
          const cssText = String(rule.style?.cssText || "").trim();
          if (selectors.length > 0 && cssText) {
            styleRules.push({ selectors, cssText, style: rule.style });
          }
          continue;
        }
        if (rule.cssRules) collectStyleRules(rule.cssRules);
      }
    }
    for (const styleSheet of document.styleSheets) {
      try {
        collectStyleRules(styleSheet.cssRules);
      } catch {
        // 跨域样式表无法读取CSSOM，保留可访问规则；祖先不重复读取同一失败来源。
      }
    }

    function collectMatchingRules(target, kind) {
      for (const { selectors, cssText, style } of styleRules) {
        const matchingSelectors = selectors.filter((selector) => selectorMatches(target, selector));
        if (matchingSelectors.length === 0) continue;
        const hasPseudoSelector = matchingSelectors.some((selector) => selector.includes("::"));
        const line = `${matchingSelectors.join(", ")} { ${cssText} }`;
        if (seenRuleLines.has(`${kind}:${line}`)) continue;
        seenRuleLines.add(`${kind}:${line}`);
        if (kind === "inherited") {
          collectDeclarations(style, authorPropertyNames, true);
          inheritedRuleLines.push(line);
        } else if (hasPseudoSelector) {
          collectDeclarations(style, authorPropertyNames);
          pseudoRuleLines.push(line);
        } else {
          collectDeclarations(style, authorPropertyNames);
          normalRuleLines.push(line);
        }
      }
    }

    const inlineStyle = String(element.style?.cssText || "").trim();
    if (inlineStyle) {
      collectDeclarations(element.style, authorPropertyNames);
      normalRuleLines.push(`element { ${inlineStyle} }`);
    }
    collectMatchingRules(element, "direct");
    for (let ancestorElement = element.parentElement;
      ancestorElement instanceof Element;
      ancestorElement = ancestorElement.parentElement) {
      collectMatchingRules(ancestorElement, "inherited");
    }

    const computedStyleDeclaration = window.getComputedStyle(element);
    const computedStyles = {};
    for (const property of keyComputedProperties) {
      const value = computedStyleDeclaration.getPropertyValue(property);
      if (value) {
        computedStyles[property] = value;
      }
    }
    for (const variable of referencedVars) {
      const value = computedStyleDeclaration.getPropertyValue(variable);
      if (value) {
        computedStyles[variable] = value;
      }
    }
    const computedStyleLines = [...normalRuleLines];
    if (pseudoRuleLines.length > 0) {
      computedStyleLines.push("", "/* Pseudo-elements */", ...pseudoRuleLines);
    }
    if (inheritedRuleLines.length > 0) {
      computedStyleLines.push("", "/* Inherited */", ...inheritedRuleLines);
    }
    const resolvedLines = [...authorPropertyNames]
      .map((property) => `${property}: ${computedStyleDeclaration.getPropertyValue(property)};`)
      .filter((line) => !line.endsWith(": ;"));
    if (resolvedLines.length > 0) {
      computedStyleLines.push("", "/* Resolved values */", ...resolvedLines);
    }
    const variableLines = [...referencedVars]
      .map((variable) => `${variable}: ${computedStyleDeclaration.getPropertyValue(variable)};`)
      .filter((line) => !line.endsWith(": ;"));
    if (variableLines.length > 0) {
      computedStyleLines.push("", "/* CSS variables */", ...variableLines);
    }
    const computedStyle = computedStyleLines.join("\n");

    const cleanClone = element.cloneNode(true);
    if (cleanClone instanceof Element) {
      cleanClone.removeAttribute(refAttribute);
      for (const descendant of cleanClone.querySelectorAll(`[${refAttribute}]`)) {
        descendant.removeAttribute(refAttribute);
      }
    }

    return {
      ref,
      selector: `[${refAttribute}="${CSS.escape(ref)}"]`,
      tag: element.tagName.toLowerCase(),
      id: element.id || "",
      classes: shorten(typeof element.className === "string" ? element.className : "", 240),
      role: element.getAttribute("role") || "",
      type: element.getAttribute("type") || "",
      text,
      attributes,
      outerHTML: cleanClone instanceof Element ? cleanClone.outerHTML : element.outerHTML,
      computedStyle,
      ancestors,
      computedStyles,
      title: document.title || "",
      url: location.href,
      document_revision: revision,
      bounds: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      },
      dimensions: {
        top: rect.y,
        left: rect.x,
        width: rect.width,
        height: rect.height,
      },
      innerText: element.textContent || "",
    };
  }, { pointX: x, pointY: y, revision: documentRevision, newRef: newElementRef });

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
  const label = ref || selector;
  if (dblClick) {
    await withLocatorAction(label, () => locator.dblclick({ button, timeout: TOOL_TIMEOUT_MS }));
  } else {
    await withLocatorAction(label, () => locator.click({ button, timeout: TOOL_TIMEOUT_MS }));
  }
}

export async function hoverElement(page, refSelectors, { ref = null, selector = null }) {
  const locator = await locatorFor(page, refSelectors, { ref, selector });
  await withLocatorAction(ref || selector, () => locator.hover({ timeout: TOOL_TIMEOUT_MS }));
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
      await withLocatorAction(ref || selector, () => locator.press(key, { timeout: TOOL_TIMEOUT_MS }));
    } else {
      await page.keyboard.press(key);
    }
    return;
  }
  if (locator) {
    await withLocatorAction(ref || selector, () => locator.fill(text, { timeout: TOOL_TIMEOUT_MS }));
    if (submit) {
      await withLocatorAction(ref || selector, () => locator.press("Enter", { timeout: TOOL_TIMEOUT_MS }));
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
  await withLocatorAction(fromRef || fromSelector, () => sourceLocator.dragTo(targetLocator, { timeout: TOOL_TIMEOUT_MS }));
}

export async function handleDialog(session, {
  acceptModal = null,
  promptText = undefined,
  selectFiles = undefined,
  filePayloads = undefined,
}) {
  if (filePayloads !== undefined || (selectFiles !== undefined && selectFiles !== null)) {
    const fromClient = filePayloads !== undefined;
    const selected = fromClient ? filePayloads : selectFiles;
    if (!Array.isArray(selected)) {
      throw new Error(fromClient ? "filePayloads 必须是文件数组" : "selectFiles 必须是文件路径数组");
    }
    const chooser = session.pendingFileChooser;
    if (!chooser) throw new Error("当前页面没有待处理的文件选择对话框");
    const chooserPage = chooser.page();
    const pageId = [...session.pageEntries.values()].find((entry) => entry.page === chooserPage)?.pageId ?? null;
    const files = fromClient ? selected.map((file) => ({
      name: file.name,
      mimeType: file.mimeType || "application/octet-stream",
      buffer: Buffer.from(file.data, "base64"),
    })) : selected;
    try {
      await chooser.setFiles(files);
    } catch (cause) {
      throw browserRuntimeFailure({ browserId: session.id, pageId, stage: "select_files", cause });
    }
    session.pendingFileChooser = null;
    return { summary: fromClient ? `已从用户设备选择 ${selected.length} 个文件` : `已选择 ${selected.length} 个文件` };
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
  return await withLocatorAction(ref || selector, async () => {
    if (scrollIntoViewIfNeeded) {
      await locator.scrollIntoViewIfNeeded();
    }
    return await locator.screenshot({ type: "png" });
  });
}

export async function runPlaywrightCode({ page, context, browser }, {
  code,
  timeoutMs = TOOL_TIMEOUT_MS,
}, { onTimeout } = {}) {
  if (typeof code !== "string" || !code.trim()) {
    throw new Error("runPlaywrightCode 需要 code");
  }
  // 用户脚本中的 page.click/page.goto 默认可能等待 30 秒；必须给外层工具
  // 超时留出收尾余量，避免无效 selector 被误报成 page reset。
  configurePlaywrightTimeoutBudget(page, timeoutMs);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const cancellationController = new AbortController();
  const fn = new AsyncFunction("page", "context", "browser", "signal", code);
  const result = await withTimeout(
    Promise.resolve(fn(page, context, browser, cancellationController.signal)),
    timeoutMs,
    "Playwright 代码执行",
    {
      onTimeout: async (error) => {
        cancellationController.abort(error);
        await onTimeout?.(error);
      },
    },
  );
  return {
    result: normalizeToolResult(result),
    summary: "Playwright 代码执行完成",
  };
}
