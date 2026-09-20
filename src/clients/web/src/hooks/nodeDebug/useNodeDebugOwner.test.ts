import { describe, expect, test } from "bun:test";

import {
  resolveNodeDebugThreadId,
  synchronizeNodeDebugOwner,
  type NodeDebugOwnerSelection,
} from "./useNodeDebugOwner";

const ownerA = { workspaceId: "workspace-a", sessionId: "session-a" };
const ownerB = { workspaceId: "workspace-b", sessionId: "session-b" };

describe("Node Debug owner 选择", () => {
  test("owner 变化时在 effect 落地前也只暴露 main", () => {
    const selection: NodeDebugOwnerSelection = { ...ownerA, threadId: "thread-a" };

    expect(resolveNodeDebugThreadId(selection, ownerA)).toBe("thread-a");
    expect(resolveNodeDebugThreadId(selection, ownerB)).toBe("main");
  });

  test("A→B→A 每次切换都清除旧 thread 选择", () => {
    const selectedA: NodeDebugOwnerSelection = { ...ownerA, threadId: "thread-a" };
    const selectedB = synchronizeNodeDebugOwner(selectedA, ownerB);
    const returnedA = synchronizeNodeDebugOwner(selectedB, ownerA);

    expect(selectedB).toEqual({ ...ownerB, threadId: "main" });
    expect(returnedA).toEqual({ ...ownerA, threadId: "main" });
  });

  test("同一 owner 同步保持现有对象和 thread", () => {
    const selection: NodeDebugOwnerSelection = { ...ownerA, threadId: "thread-a" };

    expect(synchronizeNodeDebugOwner(selection, ownerA)).toBe(selection);
  });
});
