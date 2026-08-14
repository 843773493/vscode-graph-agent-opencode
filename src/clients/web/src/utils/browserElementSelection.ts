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
}

interface BrowserElementSelectedMessage {
  type: "boxteam:browser-element-selected";
  browserId: string;
  workspaceId: string;
  element: Omit<BrowserElementSelection, "browserId" | "workspaceId">;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function parseBrowserElementSelectedMessage(
  value: unknown,
): BrowserElementSelection | null {
  if (!isRecord(value) || value.type !== "boxteam:browser-element-selected") {
    return null;
  }
  const element = value.element;
  if (
    typeof value.browserId !== "string"
    || typeof value.workspaceId !== "string"
    || !isRecord(element)
  ) {
    return null;
  }
  const requiredFields = ["ref", "selector", "tag", "role", "type", "text", "title", "url"];
  if (requiredFields.some((field) => typeof element[field] !== "string")) {
    return null;
  }
  const message = value as unknown as BrowserElementSelectedMessage;
  return {
    browserId: message.browserId,
    workspaceId: message.workspaceId,
    ...message.element,
  };
}

export function formatBrowserElementSelections(
  selections: BrowserElementSelection[],
): string {
  if (selections.length === 0) {
    return "";
  }
  return selections.map((selection) => {
    const attributes = [
      selection.role ? `role=${JSON.stringify(selection.role)}` : "",
      selection.type ? `type=${JSON.stringify(selection.type)}` : "",
    ].filter(Boolean).join(" ");
    return [
      `[浏览器元素 browser_id=${selection.browserId} ref=${selection.ref}]`,
      `页面: ${selection.title || "(无标题)"} (${selection.url})`,
      `元素: <${selection.tag}${attributes ? ` ${attributes}` : ""}>${selection.text}</${selection.tag}>`,
      `选择器: ${selection.selector}`,
    ].join("\n");
  }).join("\n\n");
}
