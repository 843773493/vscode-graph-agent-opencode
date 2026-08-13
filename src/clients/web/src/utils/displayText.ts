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
