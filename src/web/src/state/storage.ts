import type { WebUiSettings } from "../types/backend";
import {
  createDefaultWebUiSettings,
  normalizeWebUiSettings,
} from "./uiSettings/preferences";

const LAST_SESSION_STORAGE_KEY = "boxteam.web.currentSessionId";
const UI_SETTINGS_CACHE_KEY = "boxteam.web.uiSettings";
const UNREAD_SESSIONS_STORAGE_KEY = "boxteam.web.unreadSessionKeys";

function emptyUiSettings(): WebUiSettings {
  return createDefaultWebUiSettings();
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
  return normalizeWebUiSettings(parsed);
}

export function writeCachedUiSettings(settings: WebUiSettings): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(UI_SETTINGS_CACHE_KEY, JSON.stringify(settings));
}

export function readUnreadSessionKeys(): Set<string> {
  if (typeof window === "undefined") {
    return new Set();
  }
  const raw = window.localStorage.getItem(UNREAD_SESSIONS_STORAGE_KEY);
  if (!raw) {
    return new Set();
  }
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed) || parsed.some((value) => typeof value !== "string")) {
    throw new Error("本地未读会话状态格式无效");
  }
  return new Set(parsed);
}

export function writeUnreadSessionKeys(sessionKeys: Set<string>): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(
    UNREAD_SESSIONS_STORAGE_KEY,
    JSON.stringify([...sessionKeys]),
  );
}
