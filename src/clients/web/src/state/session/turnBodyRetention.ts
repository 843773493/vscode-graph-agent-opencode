import type { SessionTurnTimeline, TurnRecord } from "./turnTimeline";

const TERMINAL_STATUSES = new Set(["completed", "succeeded", "failed", "cancelled", "timed_out"]);

/** 这是前端正文占位记录，不是后端摘要；身份、版本和排序坐标仍来自实际读取。 */
function withoutBody(turn: TurnRecord): TurnRecord {
  return {
    turn_id: turn.turn_id, job_id: turn.job_id, session_id: turn.session_id,
    ordinal: turn.ordinal, revision: turn.revision, status: turn.status,
    created_at: turn.created_at, updated_at: turn.updated_at, completed_at: turn.completed_at,
    source_view_id: turn.source_view_id, source_view_revision: turn.source_view_revision,
    root_logical_item_ordinal: turn.root_logical_item_ordinal,
    merged_job_ids: turn.merged_job_ids,
    bodyEvicted: true,
  };
}

/** 可视窗口与最近两轮之外只保留定位信息；活动回合不能淘汰。 */
export function compactTurnBodies(timeline: SessionTurnTimeline): SessionTurnTimeline {
  if (!timeline.bodyWindowTurnIds) return timeline;
  const retained = new Set([...timeline.bodyWindowTurnIds, ...timeline.orderedTurnIds.slice(-2)]);
  let turnsById = timeline.turnsById;
  let summaryTurnsById = timeline.summaryTurnsById;
  const evicted = new Set<string>();
  for (const turnId of timeline.orderedTurnIds) {
    const turn = turnsById[turnId];
    if (retained.has(turnId) || !TERMINAL_STATUSES.has(turn.status)) continue;
    if (turn.bodyEvicted && !summaryTurnsById[turnId]) continue;
    if (turnsById === timeline.turnsById) turnsById = { ...turnsById };
    if (summaryTurnsById === timeline.summaryTurnsById) summaryTurnsById = { ...summaryTurnsById };
    turnsById[turnId] = withoutBody(turn);
    delete summaryTurnsById[turnId];
    evicted.add(turnId);
  }
  return evicted.size ? { ...timeline, turnsById, summaryTurnsById,
    detailAccessOrder: timeline.detailAccessOrder.filter(id => !evicted.has(id)) } : timeline;
}
