import type { AppState } from "../../types/frontend";
import { sessionScopeKey } from "./sessionScope";

/** 判断某个会话此刻是否正在被用户查看：它必须是当前会话，且页面可见、窗口聚焦。
 *
 * 这是「会话正在被查看」守卫的唯一实现，会话活动流与后台 Job 对账共用同一判据。
 * 无 document 的运行环境（SSR / 测试）拿不到可见性信息，按「正在查看」处理，
 * 与对账链路原有的 SSR 兜底保持一致。 */
export function isSessionActivelyViewed(
  state: AppState,
  sessionCacheKey: string,
): boolean {
  const sessionId = state.currentSession?.session_id;
  const workspaceId = state.currentSessionWorkspaceId;
  if (
    !sessionId
    || !workspaceId
    || sessionScopeKey(workspaceId, sessionId) !== sessionCacheKey
  ) {
    return false;
  }
  return typeof document === "undefined"
    || (document.visibilityState === "visible" && document.hasFocus());
}
