const DEFAULT_INITIAL_URL = "about:blank";

function hasSupportedExplicitScheme(value) {
  return /^(https?|file|data|about):/i.test(value);
}

function shouldUseHttpByDefault(value) {
  return /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[[0-9a-f:]+\])(?::|\/|$)/i.test(value);
}

function withDefaultScheme(value) {
  if (hasSupportedExplicitScheme(value)) {
    return value;
  }
  return `${shouldUseHttpByDefault(value) ? "http" : "https"}://${value}`;
}

export function normalizeBrowserUrl(rawUrl = DEFAULT_INITIAL_URL) {
  const trimmed = String(rawUrl || "").trim() || DEFAULT_INITIAL_URL;
  if (trimmed === "about:blank") {
    return trimmed;
  }
  const parsed = new URL(withDefaultScheme(trimmed));
  if (!["http:", "https:", "file:", "data:", "about:"].includes(parsed.protocol)) {
    throw new Error(`浏览器工具不支持的 URL 协议: ${parsed.protocol}`);
  }
  return parsed.toString();
}

export function nowIso() {
  return new Date().toISOString();
}
