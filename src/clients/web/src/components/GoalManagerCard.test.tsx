import { expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { restartCompletedGoalPayload } from "../state/sessionGoal";
import GoalManagerCard from "./GoalManagerCard";
import WarmConfirmProvider from "./WarmConfirmProvider";

test("Goal 管理器在右侧区域提供完整操作且不重复展示斜杠命令", () => {
  const html = renderToStaticMarkup(
    <WarmConfirmProvider>
      <GoalManagerCard
        sessionId="ses_test"
        goal={{
          goal_id: "goal_test",
          session_id: "ses_test",
          objective: "第一行\n第二行",
          status: "paused",
          token_budget: null,
          tokens_used: 10,
          time_used_seconds: 2,
          created_at: "2026-07-27T00:00:00Z",
          updated_at: "2026-07-27T00:00:01Z",
        }}
        loading={false}
        error={null}
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

  expect(html).toContain("当前 Goal");
  expect(html).toContain("继续");
  expect(html).toContain("编辑");
  expect(html).toContain("清除");
  expect(html).toContain("Token 10");
  expect(html).not.toContain("/goal");
});

test("已完成 Goal 同时提供重新开始和修改后继续", () => {
  const html = renderToStaticMarkup(
    <WarmConfirmProvider>
      <GoalManagerCard
        sessionId="ses_complete"
        goal={{
          goal_id: "goal_complete",
          session_id: "ses_complete",
          objective: "已经完成的目标",
          status: "complete",
          token_budget: 80000,
          tokens_used: 12500,
          time_used_seconds: 90,
          created_at: "2026-07-27T00:00:00Z",
          updated_at: "2026-07-27T00:10:00Z",
        }}
        loading={false}
        error={null}
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

  expect(html).toContain("已完成");
  expect(html).toContain("重新开始");
  expect(html).toContain("修改后继续");
  expect(html).not.toContain(">继续</button>");
});

test("重新开始会创建新 Goal 并保留原 token 预算", () => {
  const payload = restartCompletedGoalPayload({
    goal_id: "goal_complete",
    session_id: "ses_complete",
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
