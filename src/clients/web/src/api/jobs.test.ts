import { afterEach, expect, test } from "bun:test";
import { controlThreadExecution, getThreadExecutionControl } from "./jobs";
import { gatewayWorkspaceKey } from "./workspaceIdentity";
import { apiSuccessResponse, installGatewayFetch, restoreGatewayTestEnvironment } from "../tests/fixtures/session/gatewayEnvironment";

afterEach(restoreGatewayTestEnvironment);

test("无 Turn 的执行控制按完整 owner 读取，并精确提交原 execution 或空身份", async () => {
  const owner = { gatewayId: "gateway-control", workspaceId: "00000000-0000-4000-8000-000000000039" };
  const workspace = gatewayWorkspaceKey(owner);
  const snapshot = { session_id: "session/控制", thread_id: "thread/控制", job_id: null,
    execution_id: "execution-original", state: "paused" as const, advancement_paused: true, can_resume: true };
  const bodies: unknown[] = [];
  let wrongOwner = false;
  installGatewayFetch(({ path, url, method, init }) => {
    expect(path).toBe("/api/v1/jobs/control");
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(owner.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(owner.workspaceId);
    const query = new URL(url, "http://localhost").searchParams;
    expect(query.get("session_id")).toBe(snapshot.session_id);
    expect(query.get("thread_id")).toBe(snapshot.thread_id);
    if (method === "POST") bodies.push(JSON.parse(String(init?.body)));
    return apiSuccessResponse(wrongOwner ? { ...snapshot, thread_id: "another-thread" } : snapshot);
  });
  const current = await getThreadExecutionControl(0, workspace, snapshot.session_id, snapshot.thread_id);
  expect(current).toEqual(snapshot);
  await controlThreadExecution(0, workspace, current, "resume");
  await controlThreadExecution(0, workspace, { ...current, execution_id: null }, "resume");
  expect(bodies).toEqual([
    { action: "resume", expected_execution_id: "execution-original" },
    { action: "resume", expected_execution_id: null },
  ]);
  wrongOwner = true;
  await expect(getThreadExecutionControl(0, workspace, snapshot.session_id, snapshot.thread_id)).rejects.toThrow("执行控制响应归属不匹配");
});
