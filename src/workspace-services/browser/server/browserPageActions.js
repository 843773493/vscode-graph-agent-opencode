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

    function walkRules(rules, target, kind) {
      for (const rule of rules || []) {
        if (rule.type === 1) {
          const selectors = String(rule.selectorText || "")
            .split(",")
            .map((selector) => selector.trim())
            .filter(Boolean);
          const matchingSelectors = selectors.filter((selector) => selectorMatches(target, selector));
          if (matchingSelectors.length === 0) {
            continue;
          }
          const cssText = String(rule.style?.cssText || "").trim();
          if (!cssText) {
            continue;
          }
          const hasPseudoSelector = matchingSelectors.some((selector) => selector.includes("::"));
          const line = `${matchingSelectors.join(", ")} { ${cssText} }`;
          if (seenRuleLines.has(`${kind}:${line}`)) {
            continue;
          }
          seenRuleLines.add(`${kind}:${line}`);
          if (kind === "inherited") {
            collectDeclarations(rule.style, authorPropertyNames, true);
            inheritedRuleLines.push(line);
          } else if (hasPseudoSelector) {
            collectDeclarations(rule.style, authorPropertyNames);
            pseudoRuleLines.push(line);
          } else {
            collectDeclarations(rule.style, authorPropertyNames);
            normalRuleLines.push(line);
          }
          continue;
        }
        if (rule.cssRules) {
          walkRules(rule.cssRules, target, kind);
        }
      }
    }

    const inlineStyle = String(element.style?.cssText || "").trim();
    if (inlineStyle) {
      collectDeclarations(element.style, authorPropertyNames);
      normalRuleLines.push(`element { ${inlineStyle} }`);
    }
    for (const styleSheet of document.styleSheets) {
      try {
        walkRules(styleSheet.cssRules, element, "direct");
      } catch {
        // 跨域样式表无法读取 CSSOM，保留可访问样式表的结果。
      }
    }
    for (let ancestorElement = element.parentElement;
      ancestorElement instanceof Element;
      ancestorElement = ancestorElement.parentElement) {
      for (const styleSheet of document.styleSheets) {
        try {
          walkRules(styleSheet.cssRules, ancestorElement, "inherited");
        } catch {
          // 跨域样式表无法读取 CSSOM，保留可访问样式表的结果。
        }
      }
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
