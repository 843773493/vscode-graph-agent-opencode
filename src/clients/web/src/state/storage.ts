import type { WebUiSettings } from "../types/backend";
import {
  createDefaultWebUiSettings,
} from "./uiSettings/preferences";

// 用户会话和视图位置由 Gateway 用户状态保存；这些函数保留空实现只为让会话动作不再写共享浏览器状态。
export function readLastSessionId(): string | null {
  return null;
}

export function writeLastSessionId(_sessionId: string): void {}

export function clearLastSessionId(): void {}

export function readCachedUiSettings(): WebUiSettings {
  // 用户 UI 设置由 Gateway 当前用户 profile 返回，不能从未分用户的浏览器缓存读取。
  return createDefaultWebUiSettings();
}

export function writeCachedUiSettings(_settings: WebUiSettings): void {
  // 保留调用边界，实际不写浏览器缓存，避免不同用户共享视图设置。
}

export function readUnreadSessionKeys(): Set<string> {
  // 未读状态属于当前页面访问，不进入跨用户的 localStorage。
  return new Set();
}

export function writeUnreadSessionKeys(_sessionKeys: Set<string>): void {
  // 未读提示由当前页面内存维护，用户切换后由后端摘要流重新驱动。
}
