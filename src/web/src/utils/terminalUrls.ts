const LOCAL_TERMINAL_HOSTS = new Set([
  "127.0.0.1",
  "localhost",
  "::1",
  "0.0.0.0",
]);

export function toBrowserReachableTerminalUrl(rawUrl: string): string {
  const url = new URL(rawUrl);
  const currentHost = window.location.hostname;
  if (
    LOCAL_TERMINAL_HOSTS.has(url.hostname) &&
    currentHost &&
    !LOCAL_TERMINAL_HOSTS.has(currentHost)
  ) {
    url.hostname = currentHost;
  }
  return url.toString();
}
