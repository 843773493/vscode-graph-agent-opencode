import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as jobs from "../../api/jobs";
import { useToolApprovals } from "./useToolApprovals";

let renderer: ReactTestRenderer | undefined;
let state: ReturnType<typeof useToolApprovals>;
let read: ReturnType<typeof spyOn<typeof jobs, "getThreadToolApprovals">>;
let submit: ReturnType<typeof spyOn<typeof jobs, "submitToolApprovals">>;

beforeEach(() => {
  read = spyOn(jobs, "getThreadToolApprovals");
  submit = spyOn(jobs, "submitToolApprovals");
});

function Probe() {
  state = useToolApprovals(8014, "workspace-one", "session-one", "thread-one");
  return null;
}

afterEach(() => {
  if (renderer) act(() => renderer!.unmount());
  renderer = undefined;
  read.mockRestore();
  submit.mockRestore();
});

test("读取失败明确报错，手动重试成功清除旧错误", async () => {
  read.mockRejectedValueOnce(new Error("读取确认超时")).mockResolvedValue([]);
  await act(async () => { renderer = create(<Probe />); });
  expect(state.error).toBe("读取确认超时");
  await act(async () => { await state.retry(); });
  expect(state.error).toBeNull();
  expect(state.approvals).toEqual([]);
  expect(read).toHaveBeenCalledTimes(2);
});

test("提交失败后仍回读权威状态，成功回读不能掩盖提交错误", async () => {
  read.mockResolvedValue([]);
  submit.mockRejectedValue(new Error("检查点已拒绝"));
  await act(async () => { renderer = create(<Probe />); });
  const payload = { command_id: "command-original", checkpoint_id: "checkpoint-original", responses: [] };
  await act(async () => { await state.submit("job-original", payload); });
  expect(submit.mock.calls[0].slice(0, 4)).toEqual([8014, "workspace-one", "job-original", payload]);
  expect(read).toHaveBeenCalledTimes(2);
  expect(state.error).toBe("检查点已拒绝");
  expect(state.busy).toBe(false);
  expect(state.receipt).toBeNull();
});

test("卸载中止请求，迟到失败不会恢复旧错误", async () => {
  let rejectRead!: (reason: Error) => void;
  read.mockImplementation(() => new Promise((_, reject) => { rejectRead = reject; }));
  await act(async () => { renderer = create(<Probe />); });
  const signal = read.mock.calls[0][4];
  act(() => renderer!.unmount());
  renderer = undefined;
  expect(signal?.aborted).toBe(true);
  await act(async () => { rejectRead(new Error("旧目标读取失败")); });
  expect(state.error).toBeNull();
});
