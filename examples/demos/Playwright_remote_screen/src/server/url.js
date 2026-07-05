export function normalizeHttpUrl(rawUrl) {
  if (typeof rawUrl !== "string") {
    throw new Error("URL 必须是字符串");
  }

  const trimmed = rawUrl.trim();
  if (!trimmed) {
    throw new Error("URL 不能为空");
  }

  const withProtocol = /^[a-zA-Z][a-zA-Z\d+.-]*:/.test(trimmed) ? trimmed : `https://${trimmed}`;
  const url = new URL(withProtocol);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`只支持 http/https URL: ${rawUrl}`);
  }
  return url.toString();
}
