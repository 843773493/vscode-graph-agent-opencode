import {
  createSessionTurnTimeline,
  failTurnTimeline,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { SetAppState } from "../contentViewLoaderTypes";

/**
 * bootstrap、detail 与 page 三个 Turn 加载器共享的脚手架。
 *
 * 这三处此前各自复制了逐字相同的「按 scope 取时间线」「可取消重试等待」
 * 「最终失败投影」实现；收敛在这里后每种行为全仓只有一份。
 */

export function timelineForScope(
  timelines: Map<string, SessionTurnTimeline>,
  scopeKey: string,
): SessionTurnTimeline {
  return timelines.get(scopeKey) ?? createSessionTurnTimeline(scopeKey);
}

/** 可被 signal 立即打断的延迟等待；返回等待结束时是否仍未中止。 */
export async function waitForDelayAborted(
  delayMs: number,
  signal: AbortSignal,
): Promise<boolean> {
  if (signal.aborted) return false;
  await new Promise<void>((resolve) => {
    const timer = globalThis.setTimeout(resolve, delayMs);
    signal.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
  return !signal.aborted;
}

/** 把无法自愈的加载失败投影到当前 scope 的时间线，并保留其他状态字段。 */
export function writeTurnLoadFailure(
  setState: SetAppState,
  sessionCacheKey: string,
  targetGeneration: number,
  message: string,
): void {
  setState((previous) => {
    const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
    if (timeline.generation !== targetGeneration) return previous;
    return {
      ...previous,
      turnTimelinesBySession: writeTurnTimelineCache(
        previous.turnTimelinesBySession,
        sessionCacheKey,
        failTurnTimeline(timeline, targetGeneration, message),
      ),
    };
  });
}

