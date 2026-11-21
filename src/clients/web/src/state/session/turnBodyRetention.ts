import type { SessionTurnTimeline, TurnRecord } from "./turnTimeline";
import type { InternalExecutionRecord } from "./internalExecutionHistory";

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
  const internalExecutions = compactInternalExecutionBodies(timeline.internalExecutions,
    timeline.bodyWindowExecutionItemIds ?? [], new Set(timeline.activeJobs.map(job => job.job_id)));
  const changedExecutions = internalExecutions.some((execution, index) => execution !== timeline.internalExecutions[index]);
  return evicted.size || changedExecutions ? { ...timeline, turnsById, summaryTurnsById,
    internalExecutions: changedExecutions ? internalExecutions : timeline.internalExecutions,
    detailAccessOrder: timeline.detailAccessOrder.filter(id => !evicted.has(id)) } : timeline;
}

/** item 是无 Turn 执行的正文释放单位；同 execution 的其它 item 不因可见一项而驻留。 */
export function compactInternalExecutionBodies(executions: readonly InternalExecutionRecord[],
  retainedItemIds: readonly string[], activeJobs: ReadonlySet<string> = new Set()): InternalExecutionRecord[] {
  const retainedItems = new Set(retainedItemIds);
  return executions.map(execution => {
    let retained = retainedItems;
    if (execution.status === "unknown" || activeJobs.has(execution.job_id)) {
      // 这里全部是已提交且不可变的 canonical item；活动执行也只保留最近两项，
      // 不让一个仍在执行的 job 把数千个已完成 item 的正文全部钉在内存里。
      const recent: Array<{ id: string; ordinal: number }> = [];
      const visit = (id: string | null | undefined, ordinal: number | null | undefined) => {
        if (!id || ordinal === undefined || ordinal === null || recent.some(item => item.id === id)) return;
        recent.push({ id, ordinal });
        recent.sort((left, right) => right.ordinal - left.ordinal);
        recent.length = Math.min(recent.length, 2);
      };
      for (const part of execution.display_parts) visit(part.item_id, part.logical_item_ordinal);
      for (const part of execution.response_parts) if (part.kind !== "context_notice") {
        visit(part.source.item_id, part.source.logical_item_ordinal);
      }
      retained = new Set([...retainedItems, ...recent.map(item => item.id)]);
    }
    let changed = false;
    const display_parts = execution.display_parts.map(part => {
      if (part.bodyEvicted || retained.has(part.item_id)) return part;
      changed = true;
      return { ...part, text: "", bodyEvicted: true as const };
    });
    const response_parts = execution.response_parts.map(part => {
      // context notice 本来就只有来源 metadata；正文始终由 inspect 按需读取。
      if (part.kind === "context_notice" || part.bodyEvicted || retained.has(part.source.item_id ?? "")) return part;
      changed = true;
      return { part_id: part.part_id, kind: part.kind, projection: part.projection,
        status: part.status, source: part.source, bodyEvicted: true as const };
    });
    return changed ? { ...execution, display_parts, response_parts } : execution;
  });
}
