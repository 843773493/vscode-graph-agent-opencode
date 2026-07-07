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
