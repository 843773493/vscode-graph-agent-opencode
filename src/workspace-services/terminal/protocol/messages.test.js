import { describe, expect, test } from "bun:test";

import {
  encodeTerminalClientMessage,
  encodeTerminalServerMessage,
  parseTerminalClientMessage,
} from "./messages.js";

describe("Terminal Protobuf JSON adapter", () => {
  test("保留 attach oneof 的旧 JSON 外形", () => {
    const message = parseTerminalClientMessage(JSON.stringify({
      type: "attach",
      terminalId: "terminal_123",
      afterSequence: 4,
    }));

    expect(message).toEqual({
      type: "attach",
      terminalId: "terminal_123",
      afterSequence: 4,
    });
  });

  test("客户端发送也经过同一个 Protobuf adapter", () => {
    expect(JSON.parse(encodeTerminalClientMessage({
      type: "detach",
      terminalId: "terminal_123",
    }))).toEqual({
      type: "detach",
      terminalId: "terminal_123",
    });
  });

  test("通过 Struct 保留未迁移的服务端扩展字段", () => {
    const message = {
      type: "resync",
      terminalId: "terminal_123",
      snapshot: {
        buffer: "hello",
        last_command_status: "running",
      },
    };

    expect(JSON.parse(encodeTerminalServerMessage(message))).toEqual(message);
  });
});
