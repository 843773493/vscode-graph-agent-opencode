/** 通用对象判定：非数组的 object。全前端共享的 isRecord 权威实现。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function redactLargeData(value: unknown): unknown {
  if (typeof value === "string") {
    if (
      value.startsWith("data:image/") ||
      value.startsWith("data:video/") ||
      value.startsWith("data:audio/")
    ) {
      const commaIndex = value.indexOf(",");
      const header = commaIndex >= 0 ? value.slice(0, commaIndex) : "data:<media>";
      const payloadLength = commaIndex >= 0 ? value.length - commaIndex - 1 : value.length;
      return `${header},<base64 ${payloadLength} chars redacted>`;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(redactLargeData);
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, redactLargeData(item)]),
    );
  }
  return value;
}

export function prettyJson(value: unknown): string {
  return JSON.stringify(redactLargeData(value), null, 2) ?? "";
}

/**
 * 从剪贴板文本里提取可解析的 JSON 候选：原文优先，其次逐个取出 ```json 围栏内容，
 * 去重后按顺序返回。会话信息与工作区信息的粘贴导入共用这一实现。
 */
export function jsonCandidates(text: string): string[] {
  const candidates = [text];
  const fencePattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  for (const match of text.matchAll(fencePattern)) {
    const fencedJson = match[1]?.trim();
    if (fencedJson) {
      candidates.push(fencedJson);
    }
  }
  return [...new Set(candidates)];
}
