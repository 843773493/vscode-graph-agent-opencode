import { describe, expect, test } from "bun:test";

import { remainingOccupiedPorts } from "./dev-environment.mjs";

describe("开发端口清理后的占用核实", () => {
  test("仍被占用的端口连同监听 PID 一起返回", () => {
    const occupied = remainingOccupiedPorts([9501, 9502, 9503], (port) =>
      port === 9502 ? ["1234"] : port === 9503 ? ["5678", "9012"] : [],
    );

    expect(occupied).toEqual([
      { port: 9502, pids: ["1234"] },
      { port: 9503, pids: ["5678", "9012"] },
    ]);
  });

  test("全部释放时返回空列表", () => {
    expect(remainingOccupiedPorts([9501, 9502], () => [])).toEqual([]);
  });
});
