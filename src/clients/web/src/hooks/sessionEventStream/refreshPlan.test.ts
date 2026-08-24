import { describe, expect, test } from "bun:test";
import type { SessionStreamEvent } from "../../types/backend";
import { planTurnRefreshes } from "./refreshPlan";

function event(
  type: SessionStreamEvent["type"],
  jobId: string,
): SessionStreamEvent {
  return {
    event_id: `event-${type}-${jobId}`,
    session_id: "session-refresh-plan",
    job_id: jobId,
    type,
    phase: "job",
    title: type,
    content: "",
    timestamp: "2026-07-29T00:00:00Z",
  };
}

describe("planTurnRefreshes", () => {
  test("终态 Turn 只交给终态对账，不进入通用详情刷新", () => {
    const plan = planTurnRefreshes([
      event("tool_call_end", "job-other"),
      event("text_end", "job-terminal"),
      event("job_completed", "job-terminal"),
    ]);

    expect(plan.terminalEventIndex).toBe(2);
    expect(plan.genericTurnIds).toEqual(["job-other"]);
  });

  test("没有终态时只刷新已经产生可查询内容的失效 Turn", () => {
    const plan = planTurnRefreshes([
      event("tool_call_end", "job-a"),
      event("text_end", "job-a"),
      event("message_created", "job-b"),
    ]);

    expect(plan.terminalEventIndex).toBe(-1);
    expect(plan.genericTurnIds).toEqual(["job-a"]);
  });

  test("Job 创建和用户消息事件只更新运行中占位，不提前查询尚未提交的 Turn", () => {
    const plan = planTurnRefreshes([
      event("job_created", "job-new"),
      event("job_started", "job-new"),
      event("message_created", "job-new"),
    ]);

    expect(plan.genericTurnIds).toEqual([]);
    expect(plan.terminalEventIndex).toBe(-1);
  });

  test("多个终态事件以最后一个为终态对账目标", () => {
    const plan = planTurnRefreshes([
      event("job_failed", "job-first"),
      event("tool_call_end", "job-middle"),
      event("job_completed", "job-last"),
    ]);

    expect(plan.terminalEventIndex).toBe(2);
    expect(plan.genericTurnIds).toEqual(["job-first", "job-middle"]);
  });
});
