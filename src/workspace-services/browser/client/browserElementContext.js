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

function classNamesFromSelection(element) {
  if (typeof element.classes !== "string") {
    return [];
  }
  return element.classes.trim().split(/\s+/).filter(Boolean);
}

function elementDisplayNames(element) {
  const tagName = typeof element.tag === "string" ? element.tag.toLowerCase() : "element";
  const id = element.id ? `#${element.id}` : "";
  const classes = classNamesFromSelection(element);
  let shortName = `${tagName}${id}`;
  let fullName = `${shortName}${classes.length > 0 ? `.${classes.join(".")}` : ""}`;

  if (Array.isArray(element.ancestors) && element.ancestors.length > 0) {
    let last = element.ancestors[element.ancestors.length - 1];
    let pseudo = "";
    if (typeof last.tagName === "string" && last.tagName.startsWith("::") && element.ancestors.length > 1) {
      pseudo = last.tagName;
      last = element.ancestors[element.ancestors.length - 2];
    }
    const ancestorTagName = typeof last.tagName === "string" ? last.tagName.toLowerCase() : tagName;
    const ancestorId = last.id ? `#${last.id}` : "";
    const ancestorClasses = Array.isArray(last.classNames) && last.classNames.length > 0
      ? `.${last.classNames.join(".")}`
      : "";
    shortName = `${ancestorTagName}${ancestorId}${pseudo}`;
    fullName = `${ancestorTagName}${ancestorId}${ancestorClasses}${pseudo}`;
  }

  return { fullName };
}

/**
 * 与 VS Code Integrated Browser 的 createElementContextValue 保持一致。
 * 注意：这里故意不加入 ref、selector、属性表或 innerText，它们不是 VS Code
 * 的元素 Markdown 上下文内容。
 */
export function createBrowserElementContextValue(element) {
  const sections = [];
  const { fullName } = elementDisplayNames(element);
  sections.push("Attached Element Context from Integrated Browser");
  sections.push(`Element: ${fullName}`);

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
