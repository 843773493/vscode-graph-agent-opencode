/** 保留既有 UTF-16 长度上限，但不在代理对中间截断 emoji／扩展汉字。 */
export function truncateDisplayText(value: string, maxCodeUnits: number): string {
  let end = Math.min(value.length, maxCodeUnits);
  const last = value.charCodeAt(end - 1);
  const next = value.charCodeAt(end);
  if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) end -= 1;
  return value.slice(0, end);
}

function unwrapQuotedText(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length < 2 || !trimmed.startsWith("\"") || !trimmed.endsWith("\"")) {
    return value;
  }
  return trimmed.slice(1, -1);
}

export function normalizeDisplayText(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    return value;
  }

  if (
    trimmed.length >= 4 &&
    trimmed.startsWith("\\\"") &&
    trimmed.endsWith("\\\"")
  ) {
    return trimmed.slice(2, -2);
  }

  if (!trimmed.startsWith("\"") || !trimmed.endsWith("\"")) {
    return value;
  }

  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (typeof parsed !== "string") {
      return value;
    }
    return unwrapQuotedText(parsed);
  } catch (error) {
    if (error instanceof SyntaxError) {
      return value;
    }
    throw error;
  }
}
