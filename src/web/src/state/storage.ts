import type { WebUiSettings } from "../types/backend";

const LAST_SESSION_STORAGE_KEY = "boxteam.web.currentSessionId";
const UI_SETTINGS_CACHE_KEY = "boxteam.web.uiSettings";

function emptyUiSettings(): WebUiSettings {
  return {
    layout: {},
    recent_local_workspace_paths: [],
  };
}

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

export function clearLastSessionId(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(LAST_SESSION_STORAGE_KEY);
}

export function readCachedUiSettings(): WebUiSettings {
  if (typeof window === "undefined") {
    return emptyUiSettings();
  }
  const raw = window.localStorage.getItem(UI_SETTINGS_CACHE_KEY);
  if (!raw) {
    return emptyUiSettings();
  }
  const parsed = JSON.parse(raw) as Partial<WebUiSettings>;
  return {
    layout: parsed.layout ?? {},
    recent_local_workspace_paths: Array.isArray(parsed.recent_local_workspace_paths)
      ? parsed.recent_local_workspace_paths
      : [],
  };
}

export function writeCachedUiSettings(settings: WebUiSettings): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(UI_SETTINGS_CACHE_KEY, JSON.stringify(settings));
}
