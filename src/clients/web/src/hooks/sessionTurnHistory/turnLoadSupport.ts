import {
  createSessionTurnTimeline,
  failTurnTimeline,
  writeTurnTimelineCache,
  type SessionTurnTimeline,
} from "../../state/session/turnTimeline";
import type { MutableRefObject } from "react";
import type { SetAppState } from "../contentViewLoaderTypes";

/**
 * bootstrap、detail 与 page 三个 Turn 加载器共享的脚手架。
 *
 * 这三处此前各自复制了逐字相同的「按 scope 取时间线」「可取消重试等待」
 * 「最终失败投影」实现；收敛在这里后每种行为全仓只有一份。
 */

/**
 * 单个会话 scope 的 Turn 加载器入参。detail 与 page 两个 loader 此前各自
 * 内联了一份逐字相同的 8 字段类型；收窄副本在新增字段时会漂移，因此统一
 * 到本模块（与 contentViewLoaderTypes 的 FinishWorkspaceRefresh 同因）。
 */
export interface TurnScopeLoaderProps {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  generationRef: MutableRefObject<number>;
  requestSignal: AbortSignal;
  setState: SetAppState;
  onMissingTurn: (turnIds: string[]) => void;
}

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
/** 瞬态网络失败：保留当前可见内容，只在没有内容时落错误态。 */
export function preserveTimelineAfterTransientNetworkFailure(
  timeline: SessionTurnTimeline,
  targetGeneration: number,
): SessionTurnTimeline {
  const hasVisibleContent = timeline.orderedTurnIds.length > 0;
  return {
    ...timeline,
    phase: hasVisibleContent ? timeline.phase : "error",
    loadingBefore: false,
    loadingAfter: false,
    error: hasVisibleContent
      ? null
      : "历史服务暂时断开，当前没有可显示的历史；请稍后重试",
    generation: targetGeneration,
  };
}

/** 瞬态网络失败的唯一投影：保留当前内容并写入可重试状态文案。 */
export function writeTransientNetworkPreserved(
  setState: SetAppState,
  sessionCacheKey: string,
  targetGeneration: number,
): void {
  setState((previous) => {
    const timeline = timelineForScope(previous.turnTimelinesBySession, sessionCacheKey);
    if (timeline.generation !== targetGeneration) return previous;
    return {
      ...previous,
      turnTimelinesBySession: writeTurnTimelineCache(
        previous.turnTimelinesBySession,
        sessionCacheKey,
        preserveTimelineAfterTransientNetworkFailure(timeline, targetGeneration),
      ),
      status: "历史连接暂时变化，已保留当前内容，可继续重试",
    };
  });
}
