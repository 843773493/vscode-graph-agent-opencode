import { describe, expect, test } from "bun:test";
import { validateMessageStreamSnapshot } from "./messageStreamSnapshot";

function minimalSnapshot(): Record<string, unknown> {
  return {
    session_id: "ses_123",
    turn_id: "turn_123",
    turn_stream_id: "stream_123",
    snapshot_seq: 4,
    stream_status: "open",
    agent_loop_status: "running",
    current_attempt: 1,
    blocks: [],
    tool_executions: [],
    tool_calls: [],
    model_calls: [],
    activities: [],
    resource_refs: [],
    resumable: true,
  };
}

describe("消息流快照 DTO", () => {
  test("接受最小公共快照", () => {
    expect(validateMessageStreamSnapshot(minimalSnapshot()).snapshot_seq).toBe(4);
  });

  test("拒绝非法 stream_status", () => {
    expect(() => validateMessageStreamSnapshot({
      ...minimalSnapshot(),
      stream_status: "done",
    })).toThrow("stream_status 非法");
  });

  test("拒绝缺少公共数组", () => {
    const snapshot = minimalSnapshot();
    delete snapshot.activities;
    expect(() => validateMessageStreamSnapshot(snapshot)).toThrow("activities 必须是数组");
  });

  test("拒绝协议外顶层字段", () => {
    expect(() => validateMessageStreamSnapshot({
      ...minimalSnapshot(),
      unknown: true,
    })).toThrow("包含未知字段");
  });

  test("拒绝数组载荷：复用 utils.isRecord 的“非数组对象”语义", () => {
    expect(() => validateMessageStreamSnapshot([])).toThrow("消息流快照必须是对象");
    expect(() => validateMessageStreamSnapshot(null)).toThrow("消息流快照必须是对象");
    expect(() => validateMessageStreamSnapshot("snapshot")).toThrow("消息流快照必须是对象");
  });
});
