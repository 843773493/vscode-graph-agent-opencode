import React from "react";
import { afterEach, expect, jest, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { apiErrorResponse, apiSuccessResponse, installGatewayFetch, restoreGatewayTestEnvironment,
  TEST_GATEWAY_WORKSPACE_KEY } from "../../tests/fixtures/session/gatewayEnvironment";
import { useExecutionControls } from "./useExecutionControls";

let renderer: ReactTestRenderer | undefined;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  jest.useRealTimers();
  restoreGatewayTestEnvironment();
});

test.each([false, true])("控制读取连续失败后自动恢复，曾取得快照=%s", async (loaded) => {
  jest.useFakeTimers();
  const snapshot = { session_id: "ses_control", thread_id: "thr_control", job_id: null,
    execution_id: "exec_lost", state: "paused" as const, advancement_paused: true, can_resume: true };
  let healthy = loaded;
  const requests: string[] = [];
  installGatewayFetch(({ path, method }) => {
    expect(path).toBe("/api/v1/jobs/control");
    requests.push(method);
    return healthy ? apiSuccessResponse(snapshot) : apiErrorResponse(503, "后端重启中");
  });
  let controls!: ReturnType<typeof useExecutionControls>;
  function Probe({ active }: { active: boolean }) {
    controls = useExecutionControls({ apiPort: 0, workspaceId: TEST_GATEWAY_WORKSPACE_KEY,
      sessionId: snapshot.session_id, threadId: snapshot.thread_id, activityKey: "unchanged", active });
    return null;
  }
  await act(async () => { renderer = create(<Probe active />); });
  if (loaded) {
    expect(controls.snapshot?.can_resume).toBe(true);
    healthy = false;
    await act(async () => { jest.advanceTimersByTime(2000); });
  }
  expect(controls.snapshot).toBeNull();
  expect(controls.error).toContain("后端重启中");
  await act(async () => { await controls.control("resume"); });
  expect(requests.every(method => method === "GET")).toBe(true);
  // 同一错误连续发生也须继续恢复；不能依赖错误文案或 activityKey 变化。
  await act(async () => { jest.advanceTimersByTime(2000); });
  healthy = true;
  await act(async () => { jest.advanceTimersByTime(2000); });
  expect(controls.snapshot).toEqual(snapshot);
  expect(controls.error).toBe("");
  await act(async () => { await controls.control("resume"); });
  expect(requests.at(-1)).toBe("POST");
  await act(async () => { renderer!.update(<Probe active={false} />); });
  const count = requests.length;
  await act(async () => { jest.advanceTimersByTime(10_000); });
  expect(requests).toHaveLength(count);
});
