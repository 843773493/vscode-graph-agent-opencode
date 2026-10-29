import { inspect } from "node:util";
import { describe, expect, spyOn, test } from "bun:test";
import { createRuntimeLogger } from "../../../shared/serverRuntimeLogging.mjs";
import { parseTerminalClientMessage, rejectTerminalMessageType } from "./terminalInput.js";

const logger = createRuntimeLogger("terminal", "terminal.input.test").withContext({ workspace_id: "workspace_input_test" });

const invalidFields = [
  ["resize", "cols"], ["resize", "rows"], ["attach", "cols"], ["attach", "rows"],
  ["attach", "afterSequence"], ["ack", "sequence"],
];

describe.each(["/private/terminal-ws", String.raw`C:\private\terminal-ws`, String.raw`\\host\share\terminal-ws`])(
  "Terminal输入错误只留诊断: %s", nativePath => {
    test.each(invalidFields)("%s.%s保留schema原错和安全摘要", (type, field) => {
      const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
      try {
        let rejected;
        try { parseTerminalClientMessage(JSON.stringify({ type, [field]: nativePath }), logger); }
        catch (error) { rejected = error; }
        expect(rejected.constructor).toBe(Error);
        expect(rejected.message).toBe(`Terminal WebSocket 消息不符合协议: type=${type}, field=payload, stage=websocket_schema, error=Error`);
        expect(rejected.cause).toBeInstanceOf(Error);
        expect(rejected.cause.message).toContain(field === "afterSequence" ? "after_sequence" : field);
        expect(rejected.cause.message).toContain(`"${nativePath}"`);
        expect(logged.mock.calls.map(args => args[0]).join("\n")).toContain(inspect(rejected.cause));
      } finally { logged.mockRestore(); }
    });

    test("非法JSON与未知消息保留同一原因链", () => {
      const logged = spyOn(process.stderr, "write").mockImplementation(() => undefined);
      try {
        let rejected;
        try { parseTerminalClientMessage(nativePath, logger); } catch (error) { rejected = error; }
        expect(rejected.constructor).toBe(SyntaxError);
        expect(rejected.message).toBe("WebSocket JSON 请求解析失败: stage=websocket_json, error=SyntaxError");
        expect(rejected.cause).toBeInstanceOf(SyntaxError);
        let original;
        try { JSON.parse(nativePath); } catch (error) { original = error; }
        expect(rejected.cause.message).toBe(original.message);
        try { rejectTerminalMessageType(nativePath, logger); } catch (error) { rejected = error; }
        expect(rejected.message).toBe("未知 WebSocket 消息类型: field=type");
        expect(rejected.cause.message).toBe(`未知 WebSocket 消息类型: ${nativePath}`);
        expect(logged.mock.calls.map(args => args[0]).join("\n")).toContain(inspect(rejected.cause));
      } finally { logged.mockRestore(); }
    });
  },
);

test.each(["string", "bytes", "buffer"])("%s输入保持正文和一次解析", representation => {
  const text = String.raw`用户输入 /private/terminal-ws C:\private\terminal-ws \\host\share\terminal-ws`;
  const raw = JSON.stringify({ type: "input", data: text });
  const encoded = representation === "string" ? raw
    : representation === "bytes" ? new TextEncoder().encode(raw) : Buffer.from(raw);
  const parsed = spyOn(JSON, "parse");
  try {
    expect(parseTerminalClientMessage(encoded, logger)).toEqual({ type: "input", data: text, source: "user" });
    expect(parsed).toHaveBeenCalledTimes(1);
  } finally { parsed.mockRestore(); }
});

test("非有限数和负零仍按原JSON语义规范化，扩展消息不被提前拒绝", () => {
  expect(parseTerminalClientMessage('{"type":"extension","nested":{"overflow":1e400,"negative":-0,"rows":[-0,1e400,2]}}', logger))
    .toEqual({ type: "extension", nested: { overflow: null, negative: 0, rows: [0, null, 2] } });
  expect(parseTerminalClientMessage('{"type":"attach","terminalId":"term_test","grantToken":"grant","cols":80,"rows":24,"afterSequence":7}', logger))
    .toEqual({ type: "attach", terminalId: "term_test", grantToken: "grant", cols: 80, rows: 24, afterSequence: 7 });
});

test.each(["json", "schema", "type"])("%s拒绝的日志次错不替换原失败", stage => {
  const sinkError = new Error("/private/failed-log");
  const logged = spyOn(process.stderr, "write").mockImplementation(() => { throw sinkError; });
  try {
    let rejected;
    try {
      if (stage === "type") rejectTerminalMessageType("/private/terminal-ws", logger);
      else parseTerminalClientMessage(stage === "json" ? "/private/terminal-ws" : '{"type":"resize","cols":"/private/terminal-ws"}', logger);
    } catch (error) { rejected = error; }
    expect(rejected.message).not.toContain("/private/");
    expect(rejected.cause).toBeInstanceOf(AggregateError);
    expect(rejected.cause.errors[0]).toBe(rejected.cause.cause);
    expect(rejected.cause.errors[0]).toBeInstanceOf(Error);
    expect(rejected.cause.errors[1]).toBe(sinkError);
  } finally { logged.mockRestore(); }
});
