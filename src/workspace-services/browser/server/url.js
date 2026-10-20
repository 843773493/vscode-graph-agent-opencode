const DEFAULT_INITIAL_URL = "about:blank";
const SUPPORTED_PROTOCOLS = new Set(["http:", "https:", "file:", "data:", "about:"]);

function shouldUseHttpByDefault(value) {
  return /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[[0-9a-f:]+\])(?::|[/?#]|$)/i.test(value);
}

function withDefaultScheme(value) {
  const protocol = value.match(/^[a-z][a-z0-9+.-]*:/i)?.[0].toLowerCase();
  // 主机名后的数字端口不算协议；显式协议留给唯一的协议白名单校验。
  if (protocol && (SUPPORTED_PROTOCOLS.has(protocol) || !/^[a-z0-9.-]+:\d+(?:[/?#]|$)/i.test(value))) {
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
  if (!SUPPORTED_PROTOCOLS.has(parsed.protocol)) {
    throw new Error(`浏览器工具不支持的 URL 协议: ${parsed.protocol}`);
  }
  return parsed.toString();
}

export function nowIso() {
  return new Date().toISOString();
}
