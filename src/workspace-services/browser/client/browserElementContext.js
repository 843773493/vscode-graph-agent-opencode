function formatElementPath(ancestors) {
  if (!Array.isArray(ancestors) || ancestors.length === 0) {
    return undefined;
  }

  return ancestors
    .map((ancestor) => {
      const classes = Array.isArray(ancestor.classNames) && ancestor.classNames.length > 0
        ? `.${ancestor.classNames.join(".")}`
        : "";
      const id = ancestor.id ? `#${ancestor.id}` : "";
      return `${ancestor.tagName}${id}${classes}`;
    })
    .join(" > ");
}

// 与 smartpagetranslator 的普通元素选择保持一致：只复制元素本身的 HTML。
export function formatBrowserElementBasicClipboard(element) {
  return element.outerHTML || JSON.stringify(element, null, 2);
}

function classNamesFromSelection(element) {
  if (typeof element.classes !== "string") {
    return [];
  }
  return element.classes.trim().split(/\s+/).filter(Boolean);
}

// 上下文中的CSS形状显示标签，不是可执行selector或稳定元素身份。
function elementContextLabel(element) {
  const tagName = typeof element.tag === "string" ? element.tag.toLowerCase() : "element";
  if (Array.isArray(element.ancestors) && element.ancestors.length > 0) {
    let contextElement = element.ancestors[element.ancestors.length - 1];
    let pseudoElementSuffix = "";
    if (typeof contextElement.tagName === "string" && contextElement.tagName.startsWith("::") && element.ancestors.length > 1) {
      pseudoElementSuffix = contextElement.tagName;
      contextElement = element.ancestors[element.ancestors.length - 2];
    }
    const contextTagName = typeof contextElement.tagName === "string" ? contextElement.tagName.toLowerCase() : tagName;
    const contextId = contextElement.id ? `#${contextElement.id}` : "";
    const contextClasses = Array.isArray(contextElement.classNames) && contextElement.classNames.length > 0
      ? `.${contextElement.classNames.join(".")}`
      : "";
    return `${contextTagName}${contextId}${contextClasses}${pseudoElementSuffix}`;
  }
  const id = element.id ? `#${element.id}` : "";
  const classes = classNamesFromSelection(element);
  return `${tagName}${id}${classes.length > 0 ? `.${classes.join(".")}` : ""}`;
}

/**
 * 与 VS Code Integrated Browser 的 createElementContextValue 保持一致。
 * 注意：这里故意不加入 ref、selector、属性表或 innerText，它们不是 VS Code
 * 的元素 Markdown 上下文内容。
 */
export function createBrowserElementContextValue(element) {
  const sections = [];
  sections.push("Attached Element Context from Integrated Browser");
  sections.push(`Element: ${elementContextLabel(element)}`);

  if (element.url) {
    sections.push(`URL: ${element.url}`);
  }

  const htmlPath = formatElementPath(element.ancestors);
  if (htmlPath) {
    sections.push(`HTML Path: ${htmlPath}`);
  }

  sections.push(`Outer HTML:\n\`\`\`html\n${element.outerHTML || ""}\n\`\`\``);

  const dimensions = element.dimensions || (element.bounds
    ? {
        top: element.bounds.y,
        left: element.bounds.x,
        width: element.bounds.width,
        height: element.bounds.height,
      }
    : null);
  if (dimensions) {
    sections.push(
      `Dimensions:\n- top: ${Math.round(dimensions.top)}px\n- left: ${Math.round(dimensions.left)}px\n- width: ${Math.round(dimensions.width)}px\n- height: ${Math.round(dimensions.height)}px`,
    );
  }

  sections.push(`CSS:\n\`\`\`css\n${element.computedStyle || ""}\n\`\`\``);
  return sections.join("\n\n");
}

export function formatBrowserElementClipboard(elements) {
  return elements.map(createBrowserElementContextValue).join("\n\n");
}
