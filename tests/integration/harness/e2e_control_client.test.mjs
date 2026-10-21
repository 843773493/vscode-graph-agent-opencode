import { describe, expect, test } from "bun:test";
import { createServer } from "node:net";
import { setImmediate } from "node:timers/promises";

import { testControl } from "../../support/e2e_control/browser_client.mjs";

async function withControlServer(respond, exercise) {
  const previousAddress = process.env.BOXTEAM_E2E_TEST_CONTROL;
  const requests = [];
  const sockets = new Set();
  const server = createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.setEncoding("utf8");
    let requestText = "";
    socket.on("data", (chunk) => {
      requestText += chunk;
      const newline = requestText.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(requestText.slice(0, newline));
      requests.push(request);
      void respond(socket, request);
    });
  });
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    process.env.BOXTEAM_E2E_TEST_CONTROL = JSON.stringify({
      host: "127.0.0.1", port: server.address().port, token: "isolated-control-token",
    });
    await exercise(requests);
  } finally {
    if (previousAddress === undefined) delete process.env.BOXTEAM_E2E_TEST_CONTROL;
    else process.env.BOXTEAM_E2E_TEST_CONTROL = previousAddress;
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
  }
}

describe("测试控制客户端单行回执", () => {
  test.each([
    ["not-json", "SyntaxError"],
    ['{"value":', "SyntaxError"],
    ["", "SyntaxError"],
    ["null", "TypeError"],
  ])("无效回执 %j 由原Promise拒绝而不产生未捕获异常", (body, expectedErrorName) => {
    // 在独立进程验证异常归属，旧回调的未捕获异常不能终止本测试运行器。
    const source = `
      import assert from "node:assert/strict";
      import { createServer } from "node:net";
      import { testControl } from "./tests/support/e2e_control/browser_client.mjs";
      const [body, expectedErrorName] = JSON.parse(process.argv[1]);
      const sockets = new Set();
      const server = createServer({ allowHalfOpen: true }, socket => {
        sockets.add(socket);
        socket.once("data", () => socket.write(body + "\\n"));
      });
      try {
        await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
        process.env.BOXTEAM_E2E_TEST_CONTROL = JSON.stringify({
          host: "127.0.0.1", port: server.address().port, token: "parse-error-test",
        });
        let caught;
        try { await testControl({ action: "events" }); }
        catch (error) { caught = error; }
        assert.equal(caught?.name, expectedErrorName);
        assert(caught instanceof Error);
        assert(caught.stack.includes("JSON.parse") || caught instanceof TypeError);
        console.log(JSON.stringify({ rejected: true, error_name: caught.name }));
      } finally {
        for (const socket of sockets) socket.destroy();
        await new Promise(resolve => server.close(resolve));
      }
    `;
    const result = Bun.spawnSync([
      "node", "--input-type=module", "--eval", source, JSON.stringify([body, expectedErrorName]),
    ], { cwd: process.cwd(), stdout: "pipe", stderr: "pipe" });
    expect(result.exitCode, result.stderr.toString()).toBe(0);
    expect(result.stderr.toString()).toBe("");
    expect(JSON.parse(result.stdout.toString())).toEqual({ rejected: true, error_name: expectedErrorName });
  });

  test("跨UTF-8字节分块仍返回完整正文并保留请求凭据", async () => {
    const value = { label: "中文🙂", lines: "第一行\n第二行", nested: [null, 0, false] };
    const bytes = Buffer.from(JSON.stringify({ value }) + "\n");
    await withControlServer(async (socket) => {
      socket.setNoDelay(true);
      for (const byte of bytes) {
        socket.write(Buffer.from([byte]));
        await setImmediate();
      }
      socket.end();
    }, async (requests) => {
      expect(await testControl({ action: "events", marker: "保持正文" })).toEqual(value);
      expect(requests).toEqual([{
        action: "events", marker: "保持正文", token: "isolated-control-token", pid: process.pid,
      }]);
    });
  });

  test.each([null, false, 0, ""])("合法假值正文 %j 不变成缺失结果", async (value) => {
    await withControlServer((socket) => socket.end(JSON.stringify({ value }) + "\n"), async () => {
      expect(await testControl({ action: "value" })).toBe(value);
    });
  });

  test("错误回执通过Promise拒绝且保留原错误信息", async () => {
    await withControlServer((socket) => socket.end('{"error":"ValueError: 路径无效"}\n'), async () => {
      await expect(testControl({ action: "read_directory" })).rejects.toThrow("ValueError: 路径无效");
    });
  });

  test("流结束但没有换行时明确拒绝不完整回执", async () => {
    await withControlServer((socket) => socket.end('{"value":123}'), async () => {
      await expect(testControl({ action: "events" })).rejects.toThrow("外置 test control 未返回完整回执");
    });
  });

  test("同包尾随内容不改变第一条完整回执", async () => {
    await withControlServer((socket) => socket.end('{"value":"first"}\nignored trailing data'), async () => {
      expect(await testControl({ action: "events" })).toBe("first");
    });
  });

  test("并发请求各自收集分块且不会串用正文", async () => {
    await withControlServer(async (socket, request) => {
      socket.write('{"value":');
      await setImmediate();
      socket.end(JSON.stringify(request.ordinal) + "}\n");
    }, async (requests) => {
      expect(await Promise.all(Array.from({ length: 8 }, (_, ordinal) =>
        testControl({ action: "events", ordinal }),
      ))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
      expect(requests).toHaveLength(8);
    });
  });
});
