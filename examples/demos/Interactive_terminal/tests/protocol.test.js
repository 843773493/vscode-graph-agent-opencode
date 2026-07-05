import { describe, expect, test } from "bun:test";
import { FIXED_TERMINAL_ID } from "../src/server/terminalManager.js";
import { validateClientMessage } from "../src/server/protocol.js";

describe("WebSocket 协议校验", () => {
  test("接受 attach/detach/input/resize/agentInput 消息", () => {
    expect(validateClientMessage({ type: "attach", terminalId: FIXED_TERMINAL_ID }).type).toBe("attach");
    expect(validateClientMessage({ type: "detach", terminalId: FIXED_TERMINAL_ID }).type).toBe("detach");
    expect(validateClientMessage({ type: "input", terminalId: FIXED_TERMINAL_ID, data: "pwd\n" }).type).toBe("input");
    expect(validateClientMessage({ type: "agentInput", terminalId: FIXED_TERMINAL_ID, data: "echo ok\n" }).type).toBe("agentInput");
    expect(validateClientMessage({ type: "resize", terminalId: FIXED_TERMINAL_ID, cols: 80, rows: 24 }).type).toBe("resize");
  });

  test("未知消息类型快速失败", () => {
    expect(() => validateClientMessage({ type: "unknown", terminalId: FIXED_TERMINAL_ID })).toThrow("未知消息类型");
  });

  test("缺少 terminalId 或 data 快速失败", () => {
    expect(() => validateClientMessage({ type: "attach", terminalId: "" })).toThrow("terminalId 不能为空");
    expect(() => validateClientMessage({ type: "input", terminalId: FIXED_TERMINAL_ID })).toThrow("data 必须是字符串");
  });
});
