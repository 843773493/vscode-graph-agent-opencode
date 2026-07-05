const LAST_SESSION_STORAGE_KEY = "boxteam.web.currentSessionId";

export function readLastSessionId(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.localStorage.getItem(LAST_SESSION_STORAGE_KEY);
}

export function writeLastSessionId(sessionId: string): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(LAST_SESSION_STORAGE_KEY, sessionId);
}
