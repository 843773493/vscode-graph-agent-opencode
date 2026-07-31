import { describe, expect, test } from "bun:test";
import {
  goalCanResume,
  goalEditStatus,
  goalNeedsReplacementConfirmation,
  goalStreamMutation,
  parseGoalSlashAction,
} from "../sessionGoal";
import type { SessionGoal } from "../../types/backend";

describe("Goal Slash Command", () => {
  test("裸命令展示状态，其余保留 Codex 控制命令", () => {
    expect(parseGoalSlashAction(" ")).toEqual({ kind: "show" });
    expect(parseGoalSlashAction("EDIT")).toEqual({ kind: "edit" });
    expect(parseGoalSlashAction("pause")).toEqual({ kind: "pause" });
    expect(parseGoalSlashAction("resume")).toEqual({ kind: "resume" });
    expect(parseGoalSlashAction("clear")).toEqual({ kind: "clear" });
    expect(parseGoalSlashAction("  完成全部缓存测试  ")).toEqual({
      kind: "create",
      objective: "完成全部缓存测试",
    });
  });

  test("只有 complete 可无确认替换", () => {
    expect(goalNeedsReplacementConfirmation("active")).toBeTrue();
    expect(goalNeedsReplacementConfirmation("paused")).toBeTrue();
    expect(goalNeedsReplacementConfirmation("blocked")).toBeTrue();
    expect(goalNeedsReplacementConfirmation("usage_limited")).toBeTrue();
    expect(goalNeedsReplacementConfirmation("budget_limited")).toBeTrue();
    expect(goalNeedsReplacementConfirmation("complete")).toBeFalse();
  });

  test("编辑保持非终态，并重新激活终态", () => {
    expect(goalEditStatus("active")).toBe("active");
    expect(goalEditStatus("paused")).toBe("paused");
    expect(goalEditStatus("blocked")).toBe("blocked");
    expect(goalEditStatus("usage_limited")).toBe("usage_limited");
    expect(goalEditStatus("budget_limited")).toBe("active");
    expect(goalEditStatus("complete")).toBe("active");
  });

  test("仅允许非活跃状态恢复", () => {
    expect(goalCanResume("paused")).toBeTrue();
    expect(goalCanResume("blocked")).toBeTrue();
    expect(goalCanResume("usage_limited")).toBeTrue();
    expect(goalCanResume("budget_limited")).toBeFalse();
  });

  test("Goal SSE 更新和清除使用后端完整对象", () => {
    const goal: SessionGoal = {
      goal_id: "goal_1",
      session_id: "ses_1",
      objective: "完成目标",
      status: "active",
      token_budget: 100,
      tokens_used: 20,
      time_used_seconds: 5,
      created_at: "2026-07-27T00:00:00Z",
      updated_at: "2026-07-27T00:01:00Z",
    };
    expect(goalStreamMutation({
      type: "goal_updated",
      raw: { payload: { goal } },
    })).toEqual({ kind: "updated", goal });
    expect(goalStreamMutation({ type: "goal_cleared" })).toEqual({
      kind: "cleared",
    });
  });

  test("Goal SSE 缺少权威字段时快速失败", () => {
    expect(() => goalStreamMutation({
      type: "goal_updated",
      payload: { goal: { goal_id: "goal_broken" } },
    })).toThrow("goal_updated 事件中的 goal 字段无效");
  });
});
