import { afterEach, expect, spyOn, test } from "bun:test";
import * as ReactDOM from "react-dom";
import type { ReactPortal } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { restartCompletedGoalPayload } from "../../state/sessionGoal";
import type { SessionGoal, SessionGoalUpdateRequest } from "../../types/backend";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import GoalManagerCard from "./GoalManagerCard";
import WarmConfirmProvider from "../shell/WarmConfirmProvider";

const goal: SessionGoal = {
  goal_id: "goal_lifecycle", session_id: "ses_test", thread_id: "thr_main", objective: "检查服务上线",
  status: "active", token_budget: 1000, tokens_used: 100, time_used_seconds: 12,
  created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z",
};
let renderer: ReactTestRenderer | undefined;
const restores: Array<() => void> = [];
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  restores.splice(0).reverse().forEach((restore) => restore());
});

function mountGoal(initial: SessionGoal | null, loading = false) {
  const writes: SessionGoalUpdateRequest[] = [];
  let deletes = 0;
  const render = (current: SessionGoal | null) => <WarmConfirmProvider>
    <GoalManagerCard sessionId="ses_test" goal={current} loading={loading} error={null}
      onRefresh={async () => current}
      onUpdate={async (payload) => { writes.push(payload); return { ...goal, ...payload }; }}
      onClear={async () => { deletes++; }} />
  </WarmConfirmProvider>;
  act(() => { renderer = create(render(initial)); });
  return {
    writes, deletes: () => deletes,
    show: (current: SessionGoal | null) => act(() => renderer!.update(render(current))),
    button: (text: string) => renderer!.root.findAllByType("button").find((button) => button.children.join("") === text)!,
  };
}

function installDialogContainer() {
  for (const [name, value] of Object.entries({
    window: { addEventListener() {}, removeEventListener() {} }, document: { body: {} },
  })) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value });
    restores.push(() => restoreGlobalDescriptor(name, descriptor));
  }
  // 只替换 Portal 容器，表单校验、确认和错误反馈使用真实组件。
  const portal = spyOn(ReactDOM, "createPortal").mockImplementation((children) => children as ReactPortal);
  restores.push(() => portal.mockRestore());
}

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
  expect(html).toContain("删除 Goal");
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

test("新建表单通过开始 Goal 提交目标，不沿用旧用量", async () => {
  installDialogContainer();
  const card = mountGoal(null);
  act(() => { card.button("新建 Goal").props.onClick(); });
  expect(card.button("开始 Goal").props.disabled).toBe(true);
  act(() => { renderer!.root.findByType("textarea").props.onChange({ target: { value: "  检查服务上线  " } }); });
  await act(async () => { renderer!.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
  expect(card.writes).toEqual([{ objective: "检查服务上线", token_budget: null, status: "active", replace: true }]);
});

test("编辑预算保留暂停状态，非法预算不写入，留空明确清除限制", async () => {
  installDialogContainer();
  const card = mountGoal({ ...goal, status: "paused" });
  act(() => { card.button("编辑").props.onClick(); });
  const budget = () => renderer!.root.findByProps({ "aria-label": "Goal Token 预算" });
  expect(budget().props.value).toBe("1000");
  act(() => budget().props.onChange({ target: { value: "1.5" } }));
  await act(async () => { renderer!.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
  expect(card.writes).toEqual([]);
  expect(renderer!.root.findByProps({ role: "alert" }).children.join("")).toContain("正整数");
  act(() => budget().props.onChange({ target: { value: "2000" } }));
  await act(async () => { renderer!.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
  expect(card.writes).toEqual([{ objective: goal.objective, status: "paused", token_budget: 2000 }]);
  act(() => { card.button("编辑").props.onClick(); });
  act(() => budget().props.onChange({ target: { value: "" } }));
  await act(async () => { renderer!.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
  expect(card.writes[1]).toEqual({ objective: goal.objective, status: "paused", token_budget: null });
});

test("暂停和各可恢复状态只更新状态，重新开始保留预算并显式替换", async () => {
  const card = mountGoal(goal);
  await act(async () => { card.button("暂停").props.onClick(); });
  expect(card.writes).toEqual([{ status: "paused" }]);
  for (const status of ["paused", "blocked", "usage_limited"] as const) {
    card.show({ ...goal, status });
    await act(async () => { card.button("继续").props.onClick(); });
    expect(card.writes[card.writes.length - 1]).toEqual({ status: "active" });
  }
  card.show({ ...goal, status: "complete" });
  await act(async () => { card.button("重新开始").props.onClick(); });
  expect(card.writes[card.writes.length - 1]).toEqual({ objective: goal.objective, token_budget: 1000, status: "active", replace: true });
  card.show({ ...goal, status: "budget_limited" });
  expect(card.button("继续")).toBeUndefined();
  expect(card.button("删除 Goal").props.disabled).toBe(false);
});

test("删除必须确认，取消不发出删除，确认后才删除", async () => {
  installDialogContainer();
  const card = mountGoal({ ...goal, status: "paused" });
  act(() => { card.button("删除 Goal").props.onClick(); });
  expect(card.deletes()).toBe(0);
  act(() => { card.button("取消").props.onClick(); });
  expect(card.deletes()).toBe(0);
  act(() => { card.button("删除 Goal").props.onClick(); });
  await act(async () => { renderer!.root.findByType("form").props.onSubmit({ preventDefault() {} }); });
  expect(card.deletes()).toBe(1);
});

test("等待后端响应时禁用暂停、编辑和删除，避免重复操作", () => {
  const card = mountGoal(goal, true);
  for (const text of ["暂停", "编辑", "删除 Goal"]) expect(card.button(text).props.disabled).toBe(true);
});
