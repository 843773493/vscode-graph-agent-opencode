const LOCAL_ATTACH_HOSTS = new Set([
  "127.0.0.1",
  "localhost",
  "::1",
  "0.0.0.0",
]);

export function toBrowserReachableAttachUrl(rawUrl: string): string {
  const url = new URL(rawUrl);
  const currentHost = window.location.hostname;
  if (
    LOCAL_ATTACH_HOSTS.has(url.hostname) &&
    currentHost &&
    !LOCAL_ATTACH_HOSTS.has(currentHost)
  ) {
    url.hostname = currentHost;
  }
  return url.toString();
}

export function toEmbeddedAttachUrl(rawUrl: string): string {
  const url = new URL(rawUrl);
  url.searchParams.set("embedded", "1");
  return url.toString();
}
