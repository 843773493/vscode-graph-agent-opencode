import { useEffect } from "react";
import { cloneMaps } from "../../state/appStateMaps";
import { writeUnreadSessionKeys } from "../../state/storage";
import type { SetAppState } from "../contentViewLoaderTypes";

/** 会话未读追踪链路：把内存中的未读集合写回存储边界，并在当前会话真正
 * 可见且有焦点时把它标记为已读。未读状态只属于当前页面访问，不跨用户共享。 */
export function useUnreadSessionTracking({
  unreadSessionKeys,
  currentSessionCacheKey,
  setState,
}: {
  unreadSessionKeys: Set<string>;
  currentSessionCacheKey: string | null;
  setState: SetAppState;
}): void {
  useEffect(() => {
    writeUnreadSessionKeys(unreadSessionKeys);
  }, [unreadSessionKeys]);

  useEffect(() => {
    const markCurrentSessionRead = () => {
      if (
        !currentSessionCacheKey
        || document.visibilityState !== "visible"
        || !document.hasFocus()
      ) {
        return;
      }
      setState((previous) => {
        if (!previous.unreadSessionKeys.has(currentSessionCacheKey)) {
          return previous;
        }
        const next = cloneMaps(previous);
        next.unreadSessionKeys.delete(currentSessionCacheKey);
        return next;
      });
    };
    markCurrentSessionRead();
    document.addEventListener("visibilitychange", markCurrentSessionRead);
    window.addEventListener("focus", markCurrentSessionRead);
    return () => {
      document.removeEventListener("visibilitychange", markCurrentSessionRead);
      window.removeEventListener("focus", markCurrentSessionRead);
    };
  }, [currentSessionCacheKey, setState]);
}
