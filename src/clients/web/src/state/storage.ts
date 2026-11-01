export function readUnreadSessionKeys(): Set<string> {
  // 未读状态属于当前页面访问，不进入跨用户的 localStorage。
  return new Set();
}

export function writeUnreadSessionKeys(_sessionKeys: Set<string>): void {
  // 未读提示由当前页面内存维护，用户切换后由后端摘要流重新驱动。
}
