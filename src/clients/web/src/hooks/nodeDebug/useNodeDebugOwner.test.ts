import { describe, expect, test } from "bun:test";

import {
  resolveNodeDebugThreadId,
  synchronizeNodeDebugOwner,
  type NodeDebugOwnerSelection,
} from "./useNodeDebugOwner";

const ownerA = { workspaceId: "workspace-a", sessionId: "session-a" };
const ownerB = { workspaceId: "workspace-b", sessionId: "session-b" };
const mainThreadA = "thr_00000000700070008000000000000001";
const mainThreadB = "thr_00000000700070008000000000000002";
const childThread = "thr_00000000700070008000000000000003";

describe("Node Debug owner 选择", () => {
  test("owner 变化时立即用新 owner 的 catalog main thread", () => {
    const selection: NodeDebugOwnerSelection = { ...ownerA, threadId: childThread };

    expect(resolveNodeDebugThreadId(selection, ownerA, mainThreadA)).toBe(childThread);
    expect(resolveNodeDebugThreadId(selection, ownerB, mainThreadB)).toBe(mainThreadB);
  });

  test("A→B→A 每次切换都清除旧 thread 选择", () => {
    const selectedA: NodeDebugOwnerSelection = { ...ownerA, threadId: childThread };
    const selectedB = synchronizeNodeDebugOwner(selectedA, ownerB, mainThreadB);
    const returnedA = synchronizeNodeDebugOwner(selectedB, ownerA, mainThreadA);

    expect(selectedB).toEqual({ ...ownerB, threadId: mainThreadB });
    expect(returnedA).toEqual({ ...ownerA, threadId: mainThreadA });
  });

  test("同一 owner 同步保持现有对象和 thread", () => {
    const selection: NodeDebugOwnerSelection = { ...ownerA, threadId: childThread };

    expect(synchronizeNodeDebugOwner(selection, ownerA, mainThreadA)).toBe(selection);
  });

  test("目录 main 尚未解析时不伪造 thread identity", () => {
    const selection: NodeDebugOwnerSelection = { ...ownerA, threadId: null };

    expect(resolveNodeDebugThreadId(selection, ownerA, null)).toBeNull();
  });
});
