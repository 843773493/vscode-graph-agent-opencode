const DEFAULT_INITIAL_URL = "about:blank";
const SUPPORTED_PROTOCOLS = new Set(["http:", "https:", "file:", "data:", "about:"]);
const EXPLICIT_PROTOCOL_PATTERN = /^[a-z][a-z0-9+.-]*:/i;
const LOCAL_HTTP_TARGET_PATTERN = /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[[0-9a-f:]+\])(?::|[/?#]|$)/i;
const HOST_WITH_NUMERIC_PORT_PATTERN = /^[a-z0-9.-]+:\d+(?:[/?#]|$)/i;

function shouldUseHttpByDefault(urlText) {
  return LOCAL_HTTP_TARGET_PATTERN.test(urlText);
}

function addDefaultSchemeIfMissing(urlText) {
  const explicitProtocol = urlText.match(EXPLICIT_PROTOCOL_PATTERN)?.[0].toLowerCase();
  const hasExplicitScheme = explicitProtocol && (
    SUPPORTED_PROTOCOLS.has(explicitProtocol)
    || !HOST_WITH_NUMERIC_PORT_PATTERN.test(urlText)
  );
  // 主机名后的数字端口不算协议；显式协议留给唯一的协议白名单校验。
  if (hasExplicitScheme) return urlText;
  return `${shouldUseHttpByDefault(urlText) ? "http" : "https"}://${urlText}`;
}

export function normalizeBrowserUrl(urlInput = DEFAULT_INITIAL_URL) {
  const urlText = String(urlInput || "").trim() || DEFAULT_INITIAL_URL;
  if (urlText === "about:blank") {
    return urlText;
  }
  const parsedUrl = new URL(addDefaultSchemeIfMissing(urlText));
  if (!SUPPORTED_PROTOCOLS.has(parsedUrl.protocol)) {
    throw new Error(`浏览器工具不支持的 URL 协议: ${parsedUrl.protocol}`);
  }
  return parsedUrl.toString();
}

export function nowIso() {
  return new Date().toISOString();
}
