import React from "react";
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installSessionCatalogFetchMock, unwrapSessionCatalogFetch } from "../../api/session/sessionApiFetchMock";
import { untrackSessionSkill } from "../../api/session/sessionSkillTracking";
import type { ComposerStateSnapshot } from "../../state/composerState";
import type { SessionSkillUntrackResult } from "../../types/backend";
import { useSkillUntrackCommand } from "./useSkillUntrackCommand";

const sessionId = "ses_0199b54c12a17a919abc123456789012";
const threadId = "thr_0199b54c12a17a919abc123456789013";
const target = { workspace_id: "gateway-workspace", backend_workspace_id: "backend-workspace", connection_kind: "local" as const };
const endpoint = `/api/v1/sessions/${sessionId}/threads/${threadId}/skills/untrack`;
const renderers: ReactTestRenderer[] = [];

afterEach(() => {
  act(() => { for (const renderer of renderers.splice(0)) renderer.unmount(); });
  unwrapSessionCatalogFetch();
});

function receipt(overrides: Partial<SessionSkillUntrackResult> = {}): SessionSkillUntrackResult {
  return {
    session_id: sessionId, thread_id: threadId, name: "review skill", mode: "untrack",
    status: "loaded", display_uri: null, revision: "revision", content_hash: "hash",
    append_status: "none", tracked: false, queued: false, error: null, ...overrides,
  };
}

function envelope(data: unknown): Response {
  return Response.json({ code: 0, message: "ok", request_id: "req-untrack", data });
}

function harness() {
  const notices: string[] = [];
  const errors: string[] = [];
  const workspace = { ...target };
  const state = { apiPort: 49352, gatewayWorkspaces: [workspace] } as ComposerStateSnapshot;
  let run!: (args: string) => Promise<void>;
  function Harness({ scopeKey }: { scopeKey: string }) {
    run = useSkillUntrackCommand({
      state, sessionId, workspaceId: workspace.workspace_id, scopeKey,
      setNotice: (message) => notices.push(message), setError: (message) => errors.push(message),
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<Harness scopeKey="original" />); });
  renderers.push(renderer);
  return { notices, errors, workspace, run: (args: string) => run(args), switchScope: () => {
    act(() => { renderer.update(<Harness scopeKey="other" />); });
  } };
}

test("默认主线程从后端取得，等待期间路由冻结，切换会话后不污染反馈", async () => {
  let release!: () => void;
  const barrier = new Promise<void>((resolve) => { release = resolve; });
  let mainRequested!: () => void;
  const mainSeen = new Promise<void>((resolve) => { mainRequested = resolve; });
  const calls: string[] = [];
  installSessionCatalogFetchMock(async ({ path, method, init }) => {
    calls.push(path);
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(target.workspace_id);
    expect(headers.get("X-BoxTeam-Backend-Workspace-Id")).toBe(target.backend_workspace_id);
    if (path === `/api/v1/sessions/${sessionId}/main-thread`) {
      mainRequested();
      await barrier;
      return envelope({ session_id: sessionId, thread_id: threadId });
    }
    expect(path).toBe(endpoint);
    expect(method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ name: "review skill" });
    return envelope(receipt());
  });
  const ui = harness();
  const pending = ui.run('"review skill"');
  await mainSeen;
  ui.workspace.backend_workspace_id = "changed-backend";
  ui.switchScope();
  release();
  await pending;
  expect(calls).toEqual([`/api/v1/sessions/${sessionId}/main-thread`, endpoint]);
  expect(ui.notices).toEqual([]);
  expect(ui.errors).toEqual([]);
});

test.each(["loaded", "not_tracked"] as const)("显式线程直接控制，%s 完整结果决定提示", async (status) => {
  let count = 0;
  installSessionCatalogFetchMock(({ path, init }) => {
    expect(path).toBe(endpoint);
    expect(JSON.parse(String(init?.body))).toEqual({ name: "review skill" });
    count += 1;
    return envelope(receipt({ status }));
  });
  const ui = harness();
  await ui.run(`"review skill" ${threadId}`);
  expect(count).toBe(1);
  expect(ui.errors).toEqual([]);
  expect(ui.notices).toEqual([status === "not_tracked"
    ? "review skill 在该线程未被跟踪，已有上下文保留"
    : "已停止跟踪 review skill，已有上下文保留"]);
});

test.each([
  receipt({ status: "error", error: { code: "tracking-state-conflict", message: "存在多个 active registration" } }),
  receipt({ tracked: true }),
  receipt({ status: "rebound" }),
  receipt({ append_status: "appended" }),
])("后端失败或仍在跟踪时不显示成功", async (result) => {
  installSessionCatalogFetchMock(() => envelope(result));
  const ui = harness();
  await ui.run(`"review skill" ${threadId}`);
  expect(ui.notices).toEqual([]);
  expect(ui.errors).toHaveLength(1);
  expect(ui.errors[0]).toContain(result.error?.message ?? "停止跟踪尚未完成");
});

test.each(["session_id", "thread_id", "name", "mode"] as const)("API 拒绝归属或操作不一致的 %s", async (field) => {
  installSessionCatalogFetchMock(() => envelope({ ...receipt(), [field]: "wrong" }));
  await expect(untrackSessionSkill(49352, target, sessionId, threadId, "review skill"))
    .rejects.toThrow("停止跟踪 Skill 的响应与请求归属不一致");
});

test("空名称和多余参数不发起请求", async () => {
  let count = 0;
  installSessionCatalogFetchMock(() => { count += 1; return envelope(receipt()); });
  const ui = harness();
  await ui.run('"  "');
  await ui.run("skill thread extra");
  expect(count).toBe(0);
  expect(ui.errors).toHaveLength(2);
});
