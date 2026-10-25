import { expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { restartCompletedGoalPayload } from "../../state/sessionGoal";
import type { SessionGoal } from "../../types/backend";
import GoalManagerCard from "./GoalManagerCard";
import WarmConfirmProvider from "../shell/WarmConfirmProvider";

function renderGoalCard(
  goal: SessionGoal | null,
  options: { sessionId?: string; loading?: boolean; error?: string } = {},
): string {
  return renderToStaticMarkup(
    <WarmConfirmProvider>
      <GoalManagerCard
        sessionId={options.sessionId ?? goal?.session_id ?? "ses_test"}
        goal={goal}
        loading={options.loading ?? false}
        error={options.error ?? null}
        onRefresh={async () => null}
        onUpdate={async () => {
          throw new Error("静态渲染不调用 onUpdate");
        }}
        onClear={async () => {
          throw new Error("静态渲染不调用 onClear");
        }}
      />
    </WarmConfirmProvider>,
  );
}

test("没有 Goal 的会话提供创建入口，未选会话时明确提示", () => {
  const empty = renderGoalCard(null);
  expect(empty).toContain("当前会话没有 Goal");
  expect(empty).toContain("新建 Goal");
  const unselected = renderGoalCard(null, { sessionId: "" });
  expect(unselected).toContain("请先选择会话");
  expect(unselected).not.toContain("新建 Goal");
});

test("读取中和读取失败均不得误报没有 Goal 或提供创建入口", () => {
  const loading = renderGoalCard(null, { loading: true });
  expect(loading).toContain("正在读取 Goal");
  expect(loading).not.toContain("新建 Goal");
  const failed = renderGoalCard(null, { error: "工作区连接已断开" });
  expect(failed).toContain("Goal 同步失败：工作区连接已断开");
  expect(failed).not.toContain("当前会话没有 Goal");
  expect(failed).not.toContain("新建 Goal");
});

test("Goal 管理器在右侧区域提供完整操作且不重复展示斜杠命令", () => {
  const html = renderGoalCard({
    goal_id: "goal_test",
    session_id: "ses_test",
    thread_id: "thr_goal_main",
    objective: "第一行\n第二行",
    status: "paused",
    token_budget: null,
    tokens_used: 10,
    time_used_seconds: 2,
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:00:01Z",
  });

  expect(html).toContain("当前 Goal");
  expect(html).toContain("继续");
  expect(html).toContain("编辑");
  expect(html).toContain("清除");
  expect(html).toContain("Token 10");
  expect(html).not.toContain("/goal");
});

test("已完成 Goal 同时提供重新开始和修改后继续", () => {
  const html = renderGoalCard({
    goal_id: "goal_complete",
    session_id: "ses_complete",
    thread_id: "thr_goal_main",
    objective: "已经完成的目标",
    status: "complete",
    token_budget: 80000,
    tokens_used: 12500,
    time_used_seconds: 90,
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:10:00Z",
  });

  expect(html).toContain("已完成");
  expect(html).toContain("重新开始");
  expect(html).toContain("修改后继续");
  expect(html).not.toContain(">继续</button>");
});

test("重新开始会创建新 Goal 并保留原 token 预算", () => {
  const payload = restartCompletedGoalPayload({
    goal_id: "goal_complete",
    session_id: "ses_complete",
    thread_id: "thr_goal_main",
    objective: "再次检查目标",
    status: "complete",
    token_budget: 80000,
    tokens_used: 12500,
    time_used_seconds: 90,
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:10:00Z",
  });

  expect(payload).toEqual({
    objective: "再次检查目标",
    status: "active",
    token_budget: 80000,
    replace: true,
  });
});
