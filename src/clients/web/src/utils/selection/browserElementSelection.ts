export interface BrowserElementAncestor {
  tagName: string;
  id?: string;
  classNames?: string[];
}

export interface BrowserElementSelection {
  browserId: string;
  workspaceId: string;
  ref: string;
  selector: string;
  tag: string;
  role: string;
  type: string;
  text: string;
  title: string;
  url: string;
  outerHTML: string;
  computedStyle: string;
  bounds: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
  ancestors?: BrowserElementAncestor[];
  attributes?: Record<string, string>;
  computedStyles?: Record<string, string>;
  dimensions?: {
    top: number;
    left: number;
    width: number;
    height: number;
  };
  innerText?: string;
  id?: string;
  classes?: string;
  document_revision?: number;
}

interface BrowserElementSelectedMessage {
  type: "boxteam:browser-element-selected";
  browserId: string;
  workspaceId: string;
  element: Omit<BrowserElementSelection, "browserId" | "workspaceId">;
  elements?: Array<Omit<BrowserElementSelection, "browserId" | "workspaceId">>;
  mode?: "basic" | "rich";
}

type BrowserElementRequiredStringField =
  | "ref"
  | "selector"
  | "tag"
  | "role"
  | "type"
  | "text"
  | "title"
  | "url"
  | "outerHTML"
  | "computedStyle";

type BrowserElementWithRequiredStrings = Record<string, unknown>
  & Record<BrowserElementRequiredStringField, string>;

export interface BrowserElementSelectionBundle {
  browserId: string;
  workspaceId: string;
  elements: BrowserElementSelection[];
  mode: "basic" | "rich";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return isRecord(value) && Object.values(value).every((item) => typeof item === "string");
}

function isBounds(value: unknown): value is BrowserElementSelection["bounds"] {
  return isRecord(value)
    && ["x", "y", "width", "height"].every((field) => typeof value[field] === "number");
}

function isDimensions(value: unknown): value is NonNullable<BrowserElementSelection["dimensions"]> {
  return isRecord(value)
    && ["top", "left", "width", "height"].every((field) => typeof value[field] === "number");
}

function isAncestors(value: unknown): value is BrowserElementAncestor[] {
  return Array.isArray(value) && value.every((ancestor) => isRecord(ancestor)
    && typeof ancestor.tagName === "string"
    && (ancestor.id === undefined || typeof ancestor.id === "string")
    && (ancestor.classNames === undefined
      || (Array.isArray(ancestor.classNames)
        && ancestor.classNames.every((className) => typeof className === "string"))));
}

function hasRequiredStringFields(
  value: Record<string, unknown>,
): value is BrowserElementWithRequiredStrings {
  const requiredStringFields: BrowserElementRequiredStringField[] = [
    "ref",
    "selector",
    "tag",
    "role",
    "type",
    "text",
    "title",
    "url",
    "outerHTML",
    "computedStyle",
  ];
  return requiredStringFields.every((field) => typeof value[field] === "string");
}

function parseElement(
  rawElement: unknown,
  browserId: string,
  workspaceId: string,
): BrowserElementSelection | null {
  if (!isRecord(rawElement)) {
    return null;
  }
  if (!hasRequiredStringFields(rawElement) || !isBounds(rawElement.bounds)) {
    return null;
  }
  if (rawElement.ancestors !== undefined && !isAncestors(rawElement.ancestors)) {
    return null;
  }
  if (rawElement.attributes !== undefined && !isStringRecord(rawElement.attributes)) {
    return null;
  }
  if (rawElement.computedStyles !== undefined && !isStringRecord(rawElement.computedStyles)) {
    return null;
  }
  if (rawElement.dimensions !== undefined && !isDimensions(rawElement.dimensions)) {
    return null;
  }
  if (rawElement.id !== undefined && typeof rawElement.id !== "string") {
    return null;
  }
  if (rawElement.classes !== undefined && typeof rawElement.classes !== "string") {
    return null;
  }
  if (rawElement.document_revision !== undefined && typeof rawElement.document_revision !== "number") {
    return null;
  }
  if (rawElement.innerText !== undefined && typeof rawElement.innerText !== "string") {
    return null;
  }

  return {
    browserId,
    workspaceId,
    ref: rawElement.ref,
    selector: rawElement.selector,
    tag: rawElement.tag,
    role: rawElement.role,
    type: rawElement.type,
    text: rawElement.text,
    title: rawElement.title,
    url: rawElement.url,
    outerHTML: rawElement.outerHTML,
    computedStyle: rawElement.computedStyle,
    bounds: rawElement.bounds,
    ...(isAncestors(rawElement.ancestors) ? { ancestors: rawElement.ancestors } : {}),
    ...(isStringRecord(rawElement.attributes) ? { attributes: rawElement.attributes } : {}),
    ...(isStringRecord(rawElement.computedStyles) ? { computedStyles: rawElement.computedStyles } : {}),
    ...(isDimensions(rawElement.dimensions) ? { dimensions: rawElement.dimensions } : {}),
    ...(typeof rawElement.innerText === "string" ? { innerText: rawElement.innerText } : {}),
    ...(typeof rawElement.id === "string" ? { id: rawElement.id } : {}),
    ...(typeof rawElement.classes === "string" ? { classes: rawElement.classes } : {}),
    ...(typeof rawElement.document_revision === "number"
      ? { document_revision: rawElement.document_revision }
      : {}),
  };
}

export function parseBrowserElementSelectedMessage(
  value: unknown,
): BrowserElementSelection | null {
  return parseBrowserElementSelectionBundle(value)?.elements[0] || null;
}

export function parseBrowserElementSelectionBundle(
  value: unknown,
): BrowserElementSelectionBundle | null {
  if (!isRecord(value) || value.type !== "boxteam:browser-element-selected"
    || typeof value.browserId !== "string"
    || typeof value.workspaceId !== "string"
    || !isRecord(value.element)) {
    return null;
  }

  const rawElements = Array.isArray(value.elements) ? value.elements : [value.element];
  const elements = rawElements.map((element) =>
    parseElement(element, value.browserId as string, value.workspaceId as string));
  if (elements.some((element) => element === null)) {
    return null;
  }

  const message = value as unknown as BrowserElementSelectedMessage;
  return {
    browserId: message.browserId,
    workspaceId: message.workspaceId,
    elements: elements as BrowserElementSelection[],
    mode: message.mode === "rich" ? "rich" : "basic",
  };
}

function formatElementPath(ancestors: readonly BrowserElementAncestor[] | undefined): string | undefined {
  if (!ancestors || ancestors.length === 0) {
    return undefined;
  }

  return ancestors.map((ancestor) => {
    const classes = ancestor.classNames?.length ? `.${ancestor.classNames.join(".")}` : "";
    const id = ancestor.id ? `#${ancestor.id}` : "";
    return `${ancestor.tagName}${id}${classes}`;
  }).join(" > ");
}

function formatElementDisplayName(element: BrowserElementSelection): string {
  const tagName = element.tag.toLowerCase();
  const classes = element.classes?.trim().split(/\s+/).filter(Boolean) || [];
  let displayName = `${tagName}${element.id ? `#${element.id}` : ""}${classes.length > 0 ? `.${classes.join(".")}` : ""}`;
  const ancestors = element.ancestors;
  if (!ancestors || ancestors.length === 0) {
    return displayName;
  }

  let last = ancestors[ancestors.length - 1];
  let pseudo = "";
  if (last.tagName.startsWith("::") && ancestors.length > 1) {
    pseudo = last.tagName;
    last = ancestors[ancestors.length - 2];
  }
  displayName = `${last.tagName.toLowerCase()}${last.id ? `#${last.id}` : ""}`
    + `${last.classNames?.length ? `.${last.classNames.join(".")}` : ""}${pseudo}`;
  return displayName;
}

/**
 * 按 VS Code Integrated Browser 的 createElementContextValue 生成完整 Markdown。
 * 这里不额外加入 ref、selector、属性表或 innerText，避免复制内容偏多。
 */
export function formatBrowserElementSelections(
  selections: BrowserElementSelection[],
): string {
  return selections.map((selection) => {
    const sections: string[] = [
      "Attached Element Context from Integrated Browser",
      `Element: ${formatElementDisplayName(selection)}`,
    ];
    if (selection.url) {
      sections.push(`URL: ${selection.url}`);
    }
    const htmlPath = formatElementPath(selection.ancestors);
    if (htmlPath) {
      sections.push(`HTML Path: ${htmlPath}`);
    }
    sections.push(`Outer HTML:\n\`\`\`html\n${selection.outerHTML}\n\`\`\``);
    const dimensions = selection.dimensions || {
      top: selection.bounds.y,
      left: selection.bounds.x,
      width: selection.bounds.width,
      height: selection.bounds.height,
    };
    sections.push(
      `Dimensions:\n- top: ${Math.round(dimensions.top)}px\n- left: ${Math.round(dimensions.left)}px\n- width: ${Math.round(dimensions.width)}px\n- height: ${Math.round(dimensions.height)}px`,
    );
    sections.push(`CSS:\n\`\`\`css\n${selection.computedStyle}\n\`\`\``);
    return sections.join("\n\n");
  }).join("\n\n");
}
