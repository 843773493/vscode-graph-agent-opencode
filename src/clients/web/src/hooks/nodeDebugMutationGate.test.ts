import { describe, expect, test } from "bun:test";
import { NodeDebugMutationGate } from "./nodeDebugMutationGate";

describe("Node Debug owner/mutation gate", () => {
  test("A→B→A 会保留旧 owner 锁，旧请求结束后才允许新 mutation", () => {
    const gate = new NodeDebugMutationGate("A");
    const oldMutation = gate.beginMutation("A", "action");
    if (!oldMutation) throw new Error("应成功获取 A owner 的首个 mutation");

    expect(gate.switchOwner("B")).toBe(true);
    expect(gate.switchOwner("A")).toBe(true);
    expect(gate.ownerGeneration).toBe(2);
    expect(gate.beginMutation("A", "loading")).toBeNull();
    expect(gate.busyFlags(true)).toEqual({ actionBusy: true, loading: false });
    expect(gate.isCurrentMutation(oldMutation)).toBe(false);

    expect(gate.releaseMutation(oldMutation)).toEqual({
      released: true,
      wasCurrent: false,
      isCurrentOwner: true,
    });
    const newMutation = gate.beginMutation("A", "loading");
    if (!newMutation) throw new Error("旧 mutation settle 后应允许新 mutation");
    expect(newMutation.ownerGeneration).toBe(2);
  });

  test("同一 owner 只允许一个并发 mutation，释放后才解锁", () => {
    const gate = new NodeDebugMutationGate("A");
    const firstMutation = gate.beginMutation("A", "loading");
    if (!firstMutation) throw new Error("应成功获取首个 mutation");

    expect(gate.beginMutation("A", "action")).toBeNull();
    expect(gate.releaseMutation(firstMutation)).toEqual({
      released: true,
      wasCurrent: true,
      isCurrentOwner: true,
    });
    expect(gate.beginMutation("A", "action")).not.toBeNull();
  });

  test("旧 owner mutation 不能覆盖新的 owner generation", () => {
    const gate = new NodeDebugMutationGate("A");
    const oldMutation = gate.beginMutation("A", "action");
    if (!oldMutation) throw new Error("应成功获取旧 owner mutation");

    gate.switchOwner("B");
    const currentMutation = gate.beginMutation("B", "loading");
    if (!currentMutation) throw new Error("应成功获取新 owner mutation");

    expect(currentMutation.ownerGeneration).toBe(1);
    expect(gate.isCurrentMutation(oldMutation)).toBe(false);
    expect(gate.isCurrentMutation(currentMutation)).toBe(true);
    expect(gate.releaseMutation(oldMutation)).toEqual({
      released: true,
      wasCurrent: false,
      isCurrentOwner: false,
    });
    expect(gate.isCurrentMutation(currentMutation)).toBe(true);
  });

  test("busy flags 在隐藏 owner 时清零，恢复可见后按当前锁恢复", () => {
    const gate = new NodeDebugMutationGate("A");
    const mutation = gate.beginMutation("A", "loading");
    if (!mutation) throw new Error("应成功获取 loading mutation");

    expect(gate.busyFlags(false)).toEqual({ actionBusy: false, loading: false });
    expect(gate.busyFlags(true)).toEqual({ actionBusy: false, loading: true });

    gate.switchOwner("B");
    expect(gate.busyFlags(true)).toEqual({ actionBusy: false, loading: false });
    gate.switchOwner("A");
    expect(gate.busyFlags(true)).toEqual({ actionBusy: false, loading: true });
  });
});
